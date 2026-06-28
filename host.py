"""
NakDesk Host — runs on the PC to be controlled.
"""

import asyncio
import json
import socket
import struct
import time

import cv2
import mss
import numpy as np
import pyperclip
import websockets
from pynput.keyboard import Controller as KbCtrl, Key, KeyCode
from pynput.mouse import Button, Controller as MouseCtrl

PORT    = 9000
FPS     = 20
QUALITY = 55   # JPEG quality — lower = faster

mouse_ctrl = MouseCtrl()
kb_ctrl    = KbCtrl()

SPECIAL = {
    'enter': Key.enter, 'backspace': Key.backspace, 'tab': Key.tab,
    'space': Key.space, 'escape': Key.esc, 'delete': Key.delete,
    'ctrl': Key.ctrl_l, 'alt': Key.alt_l, 'shift': Key.shift_l,
    'super': Key.cmd,
    'up': Key.up, 'down': Key.down, 'left': Key.left, 'right': Key.right,
    'home': Key.home, 'end': Key.end, 'page_up': Key.page_up, 'page_down': Key.page_down,
    'f1': Key.f1, 'f2': Key.f2, 'f3': Key.f3, 'f4': Key.f4,
    'f5': Key.f5, 'f6': Key.f6, 'f7': Key.f7, 'f8': Key.f8,
}


def screen_size():
    with mss.mss() as s:
        m = s.monitors[1]
        return m['width'], m['height']


def resolve_key(k):
    if k in SPECIAL:
        return SPECIAL[k]
    if len(k) == 1:
        return KeyCode.from_char(k)
    return None


def handle(cmd, sw, sh):
    t = cmd.get('t')

    if t == 'mm':
        mouse_ctrl.position = (int(cmd['x'] * sw), int(cmd['y'] * sh))

    elif t == 'mc':
        mouse_ctrl.position = (int(cmd['x'] * sw), int(cmd['y'] * sh))
        btn = Button.left if cmd['b'] == 'l' else Button.right
        (mouse_ctrl.press if cmd['d'] else mouse_ctrl.release)(btn)

    elif t == 'ms':
        mouse_ctrl.position = (int(cmd['x'] * sw), int(cmd['y'] * sh))
        mouse_ctrl.scroll(0, int(cmd['dy']))

    elif t == 'kp':
        k = resolve_key(cmd['k'])
        if k:
            try: kb_ctrl.press(k)
            except Exception: pass

    elif t == 'kr':
        k = resolve_key(cmd['k'])
        if k:
            try: kb_ctrl.release(k)
            except Exception: pass

    elif t == 'type':
        kb_ctrl.type(cmd.get('text', ''))

    elif t == 'cb_set':
        pyperclip.copy(cmd.get('text', ''))


async def capture_loop(ws, stop):
    sw, sh = screen_size()
    interval = 1.0 / FPS
    with mss.mss() as sct:
        mon = sct.monitors[1]
        while not stop.is_set():
            t0 = time.perf_counter()
            try:
                shot  = sct.grab(mon)
                frame = np.array(shot)[:, :, :3]
                dw    = min(1280, sw)
                dh    = int(sh * dw / sw)
                if frame.shape[1] != dw:
                    frame = cv2.resize(frame, (dw, dh))
                _, buf = cv2.imencode('.jpg', frame,
                                      [cv2.IMWRITE_JPEG_QUALITY, QUALITY])
                data   = buf.tobytes()
                header = b'F' + struct.pack('!IHH', len(data), sw, sh)
                await ws.send(header + data)
            except Exception:
                break
            await asyncio.sleep(max(0, interval - (time.perf_counter() - t0)))


async def handler(ws):
    sw, sh = screen_size()
    stop   = asyncio.Event()
    task   = asyncio.create_task(capture_loop(ws, stop))
    print(f"[+] Client connected: {ws.remote_address}")
    try:
        async for msg in ws:
            if isinstance(msg, str):
                try:
                    cmd = json.loads(msg)
                    if cmd.get('t') == 'cb_get':
                        await ws.send(json.dumps({'t': 'cb', 'text': pyperclip.paste()}))
                    else:
                        handle(cmd, sw, sh)
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        stop.set()
        task.cancel()
        print("[-] Client disconnected")


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


async def main():
    ip = local_ip()
    print(f"\n{'─'*44}")
    print(f"  NakDesk Host  —  ready")
    print(f"  Local  →  {ip}:{PORT}")

    # Optional ngrok tunnel for cross-network
    try:
        from pyngrok import ngrok
        t = ngrok.connect(PORT, 'tcp')
        pub = t.public_url.replace('tcp://', '')
        h, p = pub.split(':')
        print(f"  Public →  {h}:{p}  (share this)")
    except Exception:
        print(f"  (install pyngrok + set auth token for internet access)")

    print(f"{'─'*44}\n")
    print("  Waiting for connections…  Ctrl+C to stop\n")

    async with websockets.serve(handler, '0.0.0.0', PORT,
                                max_size=None, ping_interval=20):
        await asyncio.Future()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\nHost stopped.')
