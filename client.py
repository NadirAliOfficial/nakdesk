"""
NakDesk Client — runs on the controlling PC.
"""

import asyncio
import json
import os
import queue
import ssl
import struct
import sys
import threading
import time
import tkinter as tk
from tkinter import simpledialog

_LAST_ADDR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.last_host')

import cv2
import numpy as np
import pyperclip
import websockets
from PIL import Image, ImageTk


class NakDesk:
    BAR_H   = 42
    MM_RATE = 1 / 30

    # Dark theme palette
    _C = dict(
        bg      = '#0d0d0d',
        bar     = '#171717',
        sep     = '#2a2a2a',
        text    = '#e2e2e2',
        dim     = '#666',
        btn     = '#222',
        btn_h   = '#2e2e2e',
        btn_a   = '#383838',
        green   = '#2ecc71',
        red     = '#e74c3c',
        yellow  = '#f39c12',
        accent  = '#3d8ef8',
        acc_h   = '#2a72d9',
    )

    def __init__(self):
        self.frame_q     = queue.Queue(maxsize=1)
        self.screen_w    = 1920
        self.screen_h    = 1080
        self.fullscreen  = False
        self.connected   = False
        self._photo      = None
        self._img_item   = None
        self._last_mm    = 0.0
        self._loop       = None
        self._aq         = None
        self._cmd_held   = False
        self._last_rclick = 0.0   # dedup pynput + tkinter right-click
        self._fps_frames = 0
        self._fps_last   = time.perf_counter()
        self._pynput_ok  = False

        self.root = tk.Tk()
        self.root.title('NakDesk')
        self.root.configure(bg=self._C['bg'])
        self.root.geometry('1280x760')
        self.root.minsize(800, 500)

        self._build_ui()
        self._bind()
        self._start_rclick_listener()

    # ── UI ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        C = self._C

        # ── top bar
        bar = tk.Frame(self.root, bg=C['bar'], height=self.BAR_H)
        bar.pack(side=tk.TOP, fill=tk.X)
        bar.pack_propagate(False)
        self._bar = bar

        # 1-px separator
        self._sep = tk.Frame(self.root, bg=C['sep'], height=1)
        self._sep.pack(side=tk.TOP, fill=tk.X)

        # brand
        tk.Label(bar, text='NakDesk', fg='#fff', bg=C['bar'],
                 font=('Helvetica', 13, 'bold')).pack(side=tk.LEFT, padx=(16, 0))
        tk.Label(bar, text='Remote Desktop', fg=C['dim'], bg=C['bar'],
                 font=('Helvetica', 9)).pack(side=tk.LEFT, padx=(6, 24))

        # status + fps
        self.status_lbl = tk.Label(bar, text='● Disconnected',
                                   fg=C['red'], bg=C['bar'],
                                   font=('Helvetica', 10))
        self.status_lbl.pack(side=tk.LEFT, padx=(0, 6))

        self._fps_lbl = tk.Label(bar, text='', fg=C['dim'], bg=C['bar'],
                                 font=('Helvetica', 9))
        self._fps_lbl.pack(side=tk.LEFT)

        # helper: flat clickable label button
        def _btn(text, cmd, primary=False):
            fg  = '#fff'       if primary else C['text']
            bg  = C['accent']  if primary else C['btn']
            hbg = C['acc_h']   if primary else C['btn_h']
            abg = C['acc_h']   if primary else C['btn_a']
            b   = tk.Label(bar, text=text, fg=fg, bg=bg,
                           font=('Helvetica', 10), padx=11, pady=7,
                           cursor='hand2')
            b.bind('<Button-1>',        lambda e: (cmd(), 'break'))
            b.bind('<Enter>',           lambda e: b.config(bg=hbg))
            b.bind('<Leave>',           lambda e: b.config(bg=bg))
            b.bind('<ButtonPress-1>',   lambda e: b.config(bg=abg))
            b.bind('<ButtonRelease-1>', lambda e: b.config(bg=hbg))
            return b

        _btn('⛶', self.toggle_fs             ).pack(side=tk.RIGHT, padx=(0, 12))
        _btn('Copy ← Remote', self._copy_in  ).pack(side=tk.RIGHT, padx=2)
        _btn('Paste → Remote', self._paste_out).pack(side=tk.RIGHT, padx=2)
        _btn('Connect', self._ask_connect, primary=True).pack(side=tk.RIGHT, padx=(2, 10))

        # ── canvas
        self.canvas = tk.Canvas(self.root, bg='#000', highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self._draw_hint()

    def _draw_hint(self):
        self.canvas.update_idletasks()
        cw = self.canvas.winfo_width()  or 640
        ch = self.canvas.winfo_height() or 380
        self.canvas.create_text(
            cw // 2, ch // 2,
            text='NakDesk\n\nClick  Connect  to start',
            fill='#2a2a2a', font=('Helvetica', 22, 'bold'),
            justify='center', tags='hint')

    # ── fullscreen ────────────────────────────────────────────────────────

    def toggle_fs(self):
        self.fullscreen = not self.fullscreen
        self.root.attributes('-fullscreen', self.fullscreen)
        if self.fullscreen:
            self._bar.pack_forget()
            self._sep.pack_forget()
        else:
            self._bar.pack(side=tk.TOP, fill=tk.X, before=self.canvas)
            self._sep.pack(side=tk.TOP, fill=tk.X, before=self.canvas)

    # ── clipboard ─────────────────────────────────────────────────────────

    def _paste_out(self):
        text = pyperclip.paste()
        if text and self.connected:
            self._send({'t': 'cb_set', 'text': text})
            self._send({'t': 'type',   'text': text})

    def _copy_in(self):
        if self.connected:
            self._send({'t': 'cb_get'})

    # ── right-click via pynput (bypasses macOS event interception) ────────

    def _start_rclick_listener(self):
        try:
            from pynput.mouse import Button, Listener as ML
            def _on_click(x, y, button, pressed):
                if button != Button.right or not self.connected:
                    return
                now = time.perf_counter()
                if now - self._last_rclick < 0.05:
                    return  # tkinter already sent it
                self._last_rclick = now
                try:
                    rx = self.canvas.winfo_rootx()
                    ry = self.canvas.winfo_rooty()
                    cw = self.canvas.winfo_width()
                    ch = self.canvas.winfo_height()
                    lx, ly = x - rx, y - ry
                    if 0 <= lx < cw and 0 <= ly < ch:
                        self._send({'t': 'mc',
                                    'x': lx / max(cw, 1),
                                    'y': ly / max(ch, 1),
                                    'b': 'r', 'd': pressed})
                except Exception:
                    pass
            self._ml = ML(on_click=_on_click)
            self._ml.daemon = True
            self._ml.start()
            self._pynput_ok = True
        except Exception as e:
            print(f'[rclick] pynput unavailable ({e}) — using tkinter fallback')

    # ── events ────────────────────────────────────────────────────────────

    def _bind(self):
        c = self.canvas
        c.bind('<Motion>',          self._mm)
        c.bind('<ButtonPress-1>',   self._lp)
        c.bind('<ButtonRelease-1>', self._lr)
        c.bind('<MouseWheel>',      self._scroll)
        c.bind('<Button-4>',  lambda e: self._send({'t':'ms','x':self._nx(e),'y':self._ny(e),'dy': 3}))
        c.bind('<Button-5>',  lambda e: self._send({'t':'ms','x':self._nx(e),'y':self._ny(e),'dy':-3}))
        self.root.bind('<KeyPress>',   self._kp)
        self.root.bind('<KeyRelease>', self._kr)
        # right-click at root level — tkinter fallback (pynput is primary)
        self.root.bind_all('<ButtonPress-3>',           self._rp_root)
        self.root.bind_all('<ButtonRelease-3>',         self._rr_root)
        self.root.bind_all('<Control-ButtonPress-1>',   self._rp_root)
        self.root.bind_all('<Control-ButtonRelease-1>', self._rr_root)
        self.root.bind('<F11>',    lambda e: self.toggle_fs())
        self.root.bind('<Escape>', lambda e: (
            self.root.attributes('-fullscreen', False),
            setattr(self, 'fullscreen', False)) if self.fullscreen else None)
        import sys as _sys
        if _sys.platform == 'darwin':
            for _ch in 'abcdefghijklmnopqrstuvwxyz0123456789[]\\;\',./`':
                self.root.bind(f'<Command-{_ch}>',
                               lambda e: self._cmd_key(e))
            self.root.bind('<Command-Return>',   lambda e: self._cmd_key(e))
            self.root.bind('<Command-BackSpace>', lambda e: self._cmd_key(e))
            self.root.bind('<Command-Tab>',      lambda e: self._cmd_key(e))

    def _nx(self, e): return e.x / max(self.canvas.winfo_width(),  1)
    def _ny(self, e): return e.y / max(self.canvas.winfo_height(), 1)

    def _lp(self, e):
        self.canvas.focus_set()
        self._mc(e, 'l', True)

    def _lr(self, e): self._mc(e, 'l', False)

    def _rp_root(self, e):
        now = time.perf_counter()
        if self._pynput_ok and now - self._last_rclick < 0.05:
            return 'break'  # pynput already sent it
        cx = e.x_root - self.canvas.winfo_rootx()
        cy = e.y_root - self.canvas.winfo_rooty()
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if 0 <= cx < cw and 0 <= cy < ch:
            self.canvas.focus_set()
            self._last_rclick = now
            self._send({'t': 'mc', 'x': cx / max(cw, 1),
                        'y': cy / max(ch, 1), 'b': 'r', 'd': True})
        return 'break'

    def _rr_root(self, e):
        cx = e.x_root - self.canvas.winfo_rootx()
        cy = e.y_root - self.canvas.winfo_rooty()
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        self._send({'t': 'mc', 'x': cx / max(cw, 1),
                    'y': cy / max(ch, 1), 'b': 'r', 'd': False})
        return 'break'

    def _mm(self, e):
        now = time.perf_counter()
        if now - self._last_mm < self.MM_RATE:
            return
        self._last_mm = now
        self._send({'t': 'mm', 'x': self._nx(e), 'y': self._ny(e)})

    def _mc(self, e, btn, down):
        self._send({'t': 'mc', 'x': self._nx(e), 'y': self._ny(e),
                    'b': btn, 'd': down})

    def _scroll(self, e):
        self._send({'t': 'ms', 'x': self._nx(e), 'y': self._ny(e),
                    'dy': 3 if e.delta > 0 else -3})

    KEY_MAP = {
        'Return':'enter','BackSpace':'backspace','Tab':'tab',
        'Escape':'escape','Delete':'delete','space':'space',
        'Control_L':'ctrl','Control_R':'ctrl',
        'Alt_L':'alt','Alt_R':'alt',
        'Shift_L':'shift','Shift_R':'shift',
        'Up':'up','Down':'down','Left':'left','Right':'right',
        'Home':'home','End':'end','Prior':'page_up','Next':'page_down',
        **{f'F{i}':f'f{i}' for i in range(1,13)},
    }

    _CMD = {'Super_L', 'Super_R'}

    _CURSOR_MAP = {0:'arrow', 1:'xterm', 2:'hand2', 3:'watch', 4:'fleur', 5:'sizing'}

    def _map(self, e):
        return self.KEY_MAP.get(e.keysym) or (e.char if len(e.char) == 1 else None)

    def _cmd_key(self, e):
        k = self.KEY_MAP.get(e.keysym) or (e.keysym.lower() if len(e.keysym) == 1 else None)
        if k:
            self._send({'t': 'kp', 'k': 'ctrl'})
            self._send({'t': 'kp', 'k': k})
            self._send({'t': 'kr', 'k': k})
            self._send({'t': 'kr', 'k': 'ctrl'})
        return 'break'

    def _kp(self, e):
        if e.keysym in self._CMD:
            self._cmd_held = True
            return
        if self._cmd_held:
            return
        k = self._map(e)
        if k: self._send({'t': 'kp', 'k': k})

    def _kr(self, e):
        if e.keysym in self._CMD:
            self._cmd_held = False
            return
        if self._cmd_held:
            return
        k = self._map(e)
        if k: self._send({'t': 'kr', 'k': k})

    # ── send — zero-delay via asyncio.Queue ───────────────────────────────

    def _send(self, msg):
        if self.connected and self._loop and self._aq:
            self._loop.call_soon_threadsafe(self._aq.put_nowait, msg)

    # ── WebSocket ─────────────────────────────────────────────────────────

    def _ask_connect(self):
        try:
            last = open(_LAST_ADDR_FILE).read().strip()
        except Exception:
            last = ''
        addr = simpledialog.askstring(
            'Connect',
            'Host address\n'
            'LAN:    192.168.1.10:9000\n'
            'ngrok:  wss://xxxx.ngrok-free.app',
            initialvalue=last,
            parent=self.root)
        if not addr:
            return
        addr = addr.strip()
        try:
            open(_LAST_ADDR_FILE, 'w').write(addr)
        except Exception:
            pass
        if addr.startswith('ws://') or addr.startswith('wss://'):
            uri = addr
        else:
            if ':' not in addr:
                addr += ':9000'
            h, p = addr.rsplit(':', 1)
            uri = f'ws://{h}:{p}'
        threading.Thread(target=lambda: asyncio.run(self._ws_uri(uri)),
                         daemon=True).start()

    async def _ws(self, host, port):
        await self._ws_uri(f'ws://{host}:{port}')

    async def _ws_uri(self, uri):
        self._setstatus('Connecting…', self._C['yellow'])
        self._loop = asyncio.get_running_loop()
        self._aq   = asyncio.Queue()

        headers = {'ngrok-skip-browser-warning': 'true'}
        ssl_ctx = None
        if uri.startswith('wss://'):
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode    = ssl.CERT_NONE

        try:
            async with websockets.connect(
                    uri, max_size=None,
                    ping_interval=20,
                    compression=None,
                    additional_headers=headers,
                    ssl=ssl_ctx) as ws:
                self.connected = True
                self._setstatus('● Connected', self._C['green'])
                self.root.after(0, self.canvas.focus_set)

                async def _sender():
                    while True:
                        msg = await self._aq.get()
                        try:
                            await ws.send(json.dumps(msg))
                        except Exception:
                            pass

                sender = asyncio.create_task(_sender())
                try:
                    async for msg in ws:
                        if isinstance(msg, bytes) and msg[:1] == b'F':
                            sz = struct.unpack_from('!I', msg, 1)[0]
                            sw, sh = struct.unpack_from('!HH', msg, 5)
                            self.screen_w, self.screen_h = sw, sh
                            jpg   = msg[9: 9 + sz]
                            frame = cv2.imdecode(
                                np.frombuffer(jpg, np.uint8),
                                cv2.IMREAD_COLOR)
                            if frame is not None:
                                try: self.frame_q.get_nowait()
                                except queue.Empty: pass
                                self.frame_q.put_nowait(frame)
                        elif isinstance(msg, str):
                            d = json.loads(msg)
                            if d.get('t') == 'cb':
                                pyperclip.copy(d['text'])
                                self._setstatus('● Connected  📋 copied',
                                                self._C['green'])
                            elif d.get('t') == 'cursor':
                                cur = self._CURSOR_MAP.get(d.get('c', 0), 'arrow')
                                self.root.after(0, lambda c=cur:
                                    self.canvas.config(cursor=c))
                finally:
                    sender.cancel()
        except Exception as ex:
            print(f'[WS] {ex}')
        finally:
            self.connected = False
            self._loop     = None
            self._aq       = None
            self._setstatus('● Disconnected', self._C['red'])
            self.root.after(0, lambda: self._fps_lbl.config(text=''))
            self.root.after(0, lambda: self.canvas.config(cursor='arrow'))

    def _setstatus(self, txt, col):
        self.root.after(0, lambda: self.status_lbl.config(text=txt, fg=col))

    # ── render ────────────────────────────────────────────────────────────

    def _render(self):
        try:
            frame = self.frame_q.get_nowait()
            cw = self.canvas.winfo_width()
            ch = self.canvas.winfo_height()
            if cw > 4 and ch > 4:
                frame = cv2.resize(frame, (cw, ch),
                                   interpolation=cv2.INTER_LINEAR)
                img = Image.frombytes('RGB', (cw, ch),
                                      frame[:, :, ::-1].tobytes())
                self._photo = ImageTk.PhotoImage(image=img)
                if self._img_item is None:
                    self.canvas.delete('hint')
                    self._img_item = self.canvas.create_image(
                        0, 0, anchor=tk.NW, image=self._photo)
                else:
                    self.canvas.itemconfig(self._img_item, image=self._photo)
            # FPS counter
            self._fps_frames += 1
            now = time.perf_counter()
            if now - self._fps_last >= 1.0:
                fps = round(self._fps_frames / (now - self._fps_last))
                self._fps_frames = 0
                self._fps_last   = now
                if self.connected:
                    self.root.after(0, lambda f=fps:
                        self._fps_lbl.config(text=f'· {f} FPS'))
        except queue.Empty:
            pass
        self.root.after(10, self._render)

    def run(self):
        self.root.after(100, self._render)
        self.root.mainloop()


if __name__ == '__main__':
    if len(sys.argv) == 3:
        app = NakDesk()
        threading.Thread(
            target=lambda: asyncio.run(app._ws(sys.argv[1], int(sys.argv[2]))),
            daemon=True).start()
    else:
        app = NakDesk()
    app.run()
