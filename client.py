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
    BAR_H   = 36
    MM_RATE = 1 / 25   # 25 mouse-move msgs/sec — leaves bandwidth for clicks

    def __init__(self):
        self.frame_q    = queue.Queue(maxsize=1)
        self.screen_w   = 1920
        self.screen_h   = 1080
        self.fullscreen = False
        self.connected  = False
        self._photo     = None
        self._img_item  = None
        self._last_mm   = 0.0
        self._loop      = None       # asyncio loop running in WS thread
        self._aq        = None       # asyncio.Queue — zero-delay sends
        self._cmd_held  = False      # macOS Command key → translate to Ctrl

        self.root = tk.Tk()
        self.root.title('NakDesk')
        self.root.configure(bg='#111')
        self.root.geometry('1280x756')

        self._build_ui()
        self._bind()

    # ── UI ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        bar = tk.Frame(self.root, bg='#1c1c1e', height=self.BAR_H)
        bar.pack(side=tk.TOP, fill=tk.X)
        bar.pack_propagate(False)
        self._bar = bar

        tk.Label(bar, text='NakDesk', fg='white', bg='#1c1c1e',
                 font=('Arial', 13, 'bold')).pack(side=tk.LEFT, padx=12)

        self.status_lbl = tk.Label(bar, text='● Disconnected',
                                   fg='#ff453a', bg='#1c1c1e',
                                   font=('Arial', 10))
        self.status_lbl.pack(side=tk.LEFT, padx=8)

        b = dict(fg='white', bg='#2c2c2e', activebackground='#3a3a3c',
                 activeforeground='white', relief='flat',
                 font=('Arial', 10), padx=10, pady=4, cursor='hand2')

        tk.Button(bar, text='Connect',         command=self._ask_connect, **b).pack(side=tk.LEFT,  padx=4)
        tk.Button(bar, text='⛶ Fullscreen',    command=self.toggle_fs,    **b).pack(side=tk.RIGHT, padx=4)
        tk.Button(bar, text='📋 Paste→Remote', command=self._paste_out,   **b).pack(side=tk.RIGHT, padx=2)
        tk.Button(bar, text='📋 Copy←Remote',  command=self._copy_in,     **b).pack(side=tk.RIGHT, padx=2)

        self.canvas = tk.Canvas(self.root, bg='#000', highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.create_text(640, 360, text='Click Connect to start',
                                fill='#555', font=('Arial', 18), tags='hint')

    # ── fullscreen ────────────────────────────────────────────────────────

    def toggle_fs(self):
        self.fullscreen = not self.fullscreen
        self.root.attributes('-fullscreen', self.fullscreen)
        if self.fullscreen:
            self._bar.pack_forget()
        else:
            self._bar.pack(side=tk.TOP, fill=tk.X, before=self.canvas)

    # ── clipboard ─────────────────────────────────────────────────────────

    def _paste_out(self):
        text = pyperclip.paste()
        if text and self.connected:
            self._send({'t': 'cb_set', 'text': text})
            self._send({'t': 'type',   'text': text})

    def _copy_in(self):
        if self.connected:
            self._send({'t': 'cb_get'})

    # ── events ────────────────────────────────────────────────────────────

    def _bind(self):
        c = self.canvas
        c.bind('<Motion>',                  self._mm)
        c.bind('<ButtonPress-1>',           self._lp)
        c.bind('<ButtonRelease-1>',         self._lr)
        c.bind('<ButtonPress-3>',           self._rp)
        c.bind('<ButtonRelease-3>',         self._rr)
        c.bind('<Control-ButtonPress-1>',   self._rp)
        c.bind('<Control-ButtonRelease-1>', self._rr)
        c.bind('<MouseWheel>',              self._scroll)
        c.bind('<Button-4>',  lambda e: self._send({'t':'ms','x':self._nx(e),'y':self._ny(e),'dy': 3}))
        c.bind('<Button-5>',  lambda e: self._send({'t':'ms','x':self._nx(e),'y':self._ny(e),'dy':-3}))
        self.root.bind('<KeyPress>',   self._kp)
        self.root.bind('<KeyRelease>', self._kr)
        self.root.bind('<F11>',    lambda e: self.toggle_fs())
        self.root.bind('<Escape>', lambda e: (
            self.root.attributes('-fullscreen', False),
            setattr(self, 'fullscreen', False)) if self.fullscreen else None)

    def _nx(self, e): return e.x / max(self.canvas.winfo_width(),  1)
    def _ny(self, e): return e.y / max(self.canvas.winfo_height(), 1)

    def _lp(self, e):
        self.canvas.focus_set()   # reclaim focus on every click
        self._mc(e, 'l', True)

    def _lr(self, e): self._mc(e, 'l', False)

    def _rp(self, e):
        self.canvas.focus_set()
        self._mc(e, 'r', True)
        return 'break'

    def _rr(self, e):
        self._mc(e, 'r', False)
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

    _CMD = {'Super_L', 'Super_R'}   # macOS Command keys

    def _map(self, e):
        return self.KEY_MAP.get(e.keysym) or (e.char if len(e.char) == 1 else None)

    def _kp(self, e):
        if e.keysym in self._CMD:
            self._cmd_held = True
            return
        if self._cmd_held:
            # Cmd+key → Ctrl+key on Windows
            k = self.KEY_MAP.get(e.keysym) or (e.keysym.lower() if len(e.keysym) == 1 else None)
            if k:
                self._send({'t': 'kp', 'k': 'ctrl'})
                self._send({'t': 'kp', 'k': k})
            return
        k = self._map(e)
        if k: self._send({'t': 'kp', 'k': k})

    def _kr(self, e):
        if e.keysym in self._CMD:
            self._cmd_held = False
            return
        if self._cmd_held:
            k = self.KEY_MAP.get(e.keysym) or (e.keysym.lower() if len(e.keysym) == 1 else None)
            if k:
                self._send({'t': 'kr', 'k': k})
                self._send({'t': 'kr', 'k': 'ctrl'})
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
        addr = simpledialog.askstring('Connect',
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
        self._setstatus('Connecting…', '#ffd60a')
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
                self._setstatus('● Connected', '#30d158')
                self.root.after(0, self.canvas.focus_set)

                async def _sender():
                    while True:
                        msg = await self._aq.get()
                        try:
                            await ws.send(json.dumps(msg))
                        except Exception:
                            pass   # keep running — don't drop future messages

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
                                self._setstatus('● Connected  📋 copied', '#30d158')
                finally:
                    sender.cancel()
        except Exception as ex:
            print(f'[WS] {ex}')
        finally:
            self.connected = False
            self._loop     = None
            self._aq       = None
            self._setstatus('● Disconnected', '#ff453a')

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
            self._send({'t': 'ack'})
        except queue.Empty:
            pass
        self.root.after(14, self._render)

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
