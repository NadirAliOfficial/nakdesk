"""
NakDesk Client — runs on the controlling PC.
"""

import asyncio
import json
import queue
import struct
import sys
import threading
import tkinter as tk
from tkinter import simpledialog

import cv2
import numpy as np
import pyperclip
import websockets
from PIL import Image, ImageTk


class NakDesk:
    BAR_H = 36

    def __init__(self):
        self.ws          = None
        self.send_q      = queue.Queue()
        self.frame_q     = queue.Queue(maxsize=2)
        self.screen_w    = 1920
        self.screen_h    = 1080
        self.fullscreen  = False
        self.connected   = False
        self._photo      = None

        self.root = tk.Tk()
        self.root.title('NakDesk')
        self.root.configure(bg='#111')
        self.root.geometry('1280x752')   # 720 + bar

        self._build_ui()
        self._bind()

    # ── UI ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Top bar
        bar = tk.Frame(self.root, bg='#1c1c1e', height=self.BAR_H)
        bar.pack(side=tk.TOP, fill=tk.X)
        bar.pack_propagate(False)

        tk.Label(bar, text='NakDesk', fg='white', bg='#1c1c1e',
                 font=('Arial', 13, 'bold')).pack(side=tk.LEFT, padx=12)

        self.status_lbl = tk.Label(bar, text='● Disconnected',
                                   fg='#ff453a', bg='#1c1c1e',
                                   font=('Arial', 10))
        self.status_lbl.pack(side=tk.LEFT, padx=8)

        btn_cfg = dict(fg='white', bg='#2c2c2e', activebackground='#3a3a3c',
                       activeforeground='white', relief='flat',
                       font=('Arial', 10), padx=10, pady=4, cursor='hand2')

        tk.Button(bar, text='Connect', command=self._prompt_connect,
                  **btn_cfg).pack(side=tk.LEFT, padx=4)

        tk.Button(bar, text='⛶  Fullscreen', command=self.toggle_fullscreen,
                  **btn_cfg).pack(side=tk.RIGHT, padx=4)
        tk.Button(bar, text='📋 Paste → Remote', command=self._paste_to_remote,
                  **btn_cfg).pack(side=tk.RIGHT, padx=2)
        tk.Button(bar, text='📋 Copy ← Remote', command=self._copy_from_remote,
                  **btn_cfg).pack(side=tk.RIGHT, padx=2)

        # Canvas
        self.canvas = tk.Canvas(self.root, bg='#000',
                                highlightthickness=0, cursor='none')
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # Placeholder
        self.canvas.create_text(640, 360, text='Enter host address to connect',
                                fill='#555', font=('Arial', 18),
                                tags='placeholder')

    def _prompt_connect(self):
        addr = simpledialog.askstring(
            'Connect', 'Host address  (e.g.  192.168.1.10:9000)',
            parent=self.root)
        if not addr:
            return
        addr = addr.strip()
        if ':' not in addr:
            addr += ':9000'
        host, port = addr.rsplit(':', 1)
        threading.Thread(target=self._start_ws, args=(host, int(port)),
                         daemon=True).start()

    # ── fullscreen ────────────────────────────────────────────────────────

    def toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        self.root.attributes('-fullscreen', self.fullscreen)
        # hide / show bar
        if self.fullscreen:
            for w in self.root.pack_slaves():
                if isinstance(w, tk.Frame):
                    w.pack_forget()
        else:
            # re-pack bar above canvas
            self.root.pack_slaves()[0].pack(side=tk.TOP, fill=tk.X, before=self.canvas)

    # ── clipboard ─────────────────────────────────────────────────────────

    def _paste_to_remote(self):
        text = pyperclip.paste()
        if text and self.connected:
            self._send({'t': 'cb_set', 'text': text})
            self._send({'t': 'type',   'text': text})

    def _copy_from_remote(self):
        if self.connected:
            self._send({'t': 'cb_get'})

    # ── input events ──────────────────────────────────────────────────────

    def _bind(self):
        c = self.canvas
        c.bind('<Motion>',          self._mm)
        c.bind('<ButtonPress-1>',   lambda e: self._mc(e, 'l', True))
        c.bind('<ButtonRelease-1>', lambda e: self._mc(e, 'l', False))
        c.bind('<ButtonPress-3>',   lambda e: self._mc(e, 'r', True))
        c.bind('<ButtonRelease-3>', lambda e: self._mc(e, 'r', False))
        c.bind('<MouseWheel>',      self._scroll)
        c.bind('<Button-4>',        lambda e: self._send({'t':'ms','x':self._nx(e),'y':self._ny(e),'dy': 3}))
        c.bind('<Button-5>',        lambda e: self._send({'t':'ms','x':self._nx(e),'y':self._ny(e),'dy':-3}))
        self.root.bind('<KeyPress>',   self._kp)
        self.root.bind('<KeyRelease>', self._kr)
        self.root.bind('<F11>',        lambda e: self.toggle_fullscreen())
        self.root.bind('<Escape>',
                       lambda e: self.root.attributes('-fullscreen', False) or setattr(self, 'fullscreen', False))

    def _nx(self, e): return e.x / max(self.canvas.winfo_width(),  1)
    def _ny(self, e): return e.y / max(self.canvas.winfo_height(), 1)

    def _mm(self, e):
        self._send({'t': 'mm', 'x': self._nx(e), 'y': self._ny(e)})

    def _mc(self, e, btn, down):
        self._send({'t': 'mc', 'x': self._nx(e), 'y': self._ny(e),
                    'b': btn, 'd': down})

    def _scroll(self, e):
        dy = 3 if e.delta > 0 else -3
        self._send({'t': 'ms', 'x': self._nx(e), 'y': self._ny(e), 'dy': dy})

    KEY_MAP = {
        'Return': 'enter', 'BackSpace': 'backspace', 'Tab': 'tab',
        'Escape': 'escape', 'Delete': 'delete', 'space': 'space',
        'Control_L': 'ctrl', 'Control_R': 'ctrl',
        'Alt_L': 'alt', 'Alt_R': 'alt',
        'Shift_L': 'shift', 'Shift_R': 'shift',
        'Super_L': 'super', 'Super_R': 'super',
        'Up': 'up', 'Down': 'down', 'Left': 'left', 'Right': 'right',
        'Home': 'home', 'End': 'end', 'Prior': 'page_up', 'Next': 'page_down',
        **{f'F{i}': f'f{i}' for i in range(1, 9)},
    }

    def _map_key(self, e):
        return self.KEY_MAP.get(e.keysym) or (e.char if len(e.char) == 1 else None)

    def _kp(self, e):
        k = self._map_key(e)
        if k:
            self._send({'t': 'kp', 'k': k})

    def _kr(self, e):
        k = self._map_key(e)
        if k:
            self._send({'t': 'kr', 'k': k})

    # ── send ──────────────────────────────────────────────────────────────

    def _send(self, msg):
        if self.connected:
            self.send_q.put_nowait(msg)

    # ── WebSocket ─────────────────────────────────────────────────────────

    def _start_ws(self, host, port):
        asyncio.run(self._ws_loop(host, port))

    async def _ws_loop(self, host, port):
        uri = f'ws://{host}:{port}'
        self._set_status('Connecting…', '#ffd60a')
        try:
            async with websockets.connect(uri, max_size=None,
                                          ping_interval=20) as ws:
                self.ws        = ws
                self.connected = True
                self._set_status('● Connected', '#30d158')

                async def sender():
                    while True:
                        try:
                            msg = self.send_q.get_nowait()
                            await ws.send(json.dumps(msg))
                        except queue.Empty:
                            await asyncio.sleep(0.004)

                send_task = asyncio.create_task(sender())
                try:
                    async for message in ws:
                        if isinstance(message, bytes) and message[:1] == b'F':
                            size, sw, sh = struct.unpack('!IHH', message[1:9])
                            self.screen_w, self.screen_h = sw, sh
                            jpg   = message[9: 9 + size]
                            frame = cv2.imdecode(
                                np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                            if frame is not None:
                                try:
                                    self.frame_q.get_nowait()
                                except queue.Empty:
                                    pass
                                self.frame_q.put_nowait(frame)
                        elif isinstance(message, str):
                            data = json.loads(message)
                            if data.get('t') == 'cb':
                                pyperclip.copy(data['text'])
                                self._set_status('● Connected  📋 copied', '#30d158')
                finally:
                    send_task.cancel()
        except Exception as ex:
            print(f'[WS] {ex}')
        finally:
            self.connected = False
            self.ws        = None
            self._set_status('● Disconnected', '#ff453a')

    def _set_status(self, text, color):
        self.root.after(0, lambda: self.status_lbl.config(text=text, fg=color))

    # ── render loop ───────────────────────────────────────────────────────

    def _render(self):
        try:
            frame = self.frame_q.get_nowait()
            cw = self.canvas.winfo_width()
            ch = self.canvas.winfo_height()
            if cw > 1 and ch > 1:
                frame = cv2.resize(frame, (cw, ch))
                img   = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                self._photo = ImageTk.PhotoImage(image=img)
                self.canvas.delete('placeholder')
                self.canvas.create_image(0, 0, anchor=tk.NW, image=self._photo)
        except queue.Empty:
            pass
        self.root.after(16, self._render)   # ~60 fps

    def run(self):
        self.root.after(100, self._render)
        self.root.mainloop()


def main():
    if len(sys.argv) == 3:
        host, port = sys.argv[1], int(sys.argv[2])
        app = NakDesk()
        threading.Thread(target=app._start_ws, args=(host, port), daemon=True).start()
    else:
        app = NakDesk()
    app.run()


if __name__ == '__main__':
    main()
