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
FPS     = 25
QUALITY = 55
WIDTH   = 1280
WINDOW  = 4    # frames in-flight before waiting for ACK

mouse_ctrl = MouseCtrl()
kb_ctrl    = KbCtrl()

SPECIAL = {
    'enter': Key.enter, 'backspace': Key.backspace, 'tab': Key.tab,
    'space': Key.space, 'escape': Key.esc, 'delete': Key.delete,
    'ctrl': Key.ctrl_l, 'alt': Key.alt_l, 'shift': Key.shift_l,
    'super': Key.cmd,
    'up': Key.up, 'down': Key.down, 'left': Key.left, 'right': Key.right,
    'home': Key.home, 'end': Key.end,
    'page_up': Key.page_up, 'page_down': Key.page_down,
    **{f'f{i}': getattr(Key, f'f{i}') for i in range(1, 9)},
}


def screen_size():
    with mss.MSS() as s:
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


async def capture_loop(ws, stop, sw, sh, sem):
    dw = min(WIDTH, sw)
    dh = int(sh * dw / sw)
    enc_params = [cv2.IMWRITE_JPEG_QUALITY, QUALITY]
    header_prefix = struct.pack('!HH', sw, sh)
    interval = 1.0 / FPS

    with mss.MSS() as sct:
        mon = sct.monitors[1]
        while not stop.is_set():
            await sem.acquire()   # sliding window — max WINDOW frames in flight
            t0 = time.perf_counter()
            try:
                shot  = sct.grab(mon)
                frame = np.frombuffer(shot.raw, dtype=np.uint8).reshape(
                    (shot.height, shot.width, 4))[:, :, :3]
                if frame.shape[1] != dw:
                    frame = cv2.resize(frame, (dw, dh),
                                       interpolation=cv2.INTER_LINEAR)
                _, buf  = cv2.imencode('.jpg', frame, enc_params)
                data    = buf.tobytes()
                payload = b'F' + struct.pack('!I', len(data)) + header_prefix + data
                await ws.send(payload)
            except Exception:
                break
            dt = time.perf_counter() - t0
            await asyncio.sleep(max(0, interval - dt))


async def handler(ws):
    sw, sh = screen_size()
    stop   = asyncio.Event()
    sem    = asyncio.Semaphore(WINDOW)
    task   = asyncio.create_task(capture_loop(ws, stop, sw, sh, sem))
    print(f'[+] {ws.remote_address[0]} connected')
    try:
        async for msg in ws:
            if isinstance(msg, str):
                try:
                    cmd = json.loads(msg)
                    if cmd.get('t') == 'ack':
                        sem.release()   # free one slot in the sliding window
                    elif cmd.get('t') == 'cb_get':
                        await ws.send(json.dumps(
                            {'t': 'cb', 'text': pyperclip.paste()}))
                    else:
                        handle(cmd, sw, sh)
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        stop.set()
        task.cancel()
        print(f'[-] {ws.remote_address[0]} disconnected')


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def _check_macos_perms():
    import ctypes
    ok = True

    # Screen Recording — CGPreflightScreenCaptureAccess
    try:
        CG = ctypes.cdll.LoadLibrary(
            '/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
        CG.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        if not CG.CGPreflightScreenCaptureAccess():
            print('\n⚠️  Screen Recording permission NOT granted!')
            print('   System Settings → Privacy & Security → Screen Recording')
            print('   Add Terminal (or Python) then RESTART this script.\n')
            ok = False
    except Exception:
        pass

    # Accessibility — AXIsProcessTrusted
    try:
        AS = ctypes.cdll.LoadLibrary(
            '/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices')
        AS.AXIsProcessTrusted.restype = ctypes.c_bool
        if not AS.AXIsProcessTrusted():
            print('\n⚠️  Accessibility permission NOT granted!')
            print('   System Settings → Privacy & Security → Accessibility')
            print('   Add Terminal (or Python) then RESTART this script.\n')
            ok = False
    except Exception:
        pass

    return ok


async def main():
    import platform
    if platform.system() == 'Darwin':
        _check_macos_perms()

    ip = local_ip()
    print(f'\n{"─"*44}')
    print(f'  NakDesk Host  —  ready')
    print(f'  Local  →  {ip}:{PORT}')
    try:
        from pyngrok import ngrok
        t   = ngrok.connect(PORT, 'http')
        pub = t.public_url.replace('http://', 'ws://').replace('https://', 'wss://')
        print(f'  Public →  {pub}  (share this)')
    except Exception:
        pass
    print(f'{"─"*44}\n')

    async with websockets.serve(handler, '0.0.0.0', PORT,
                                max_size=None,
                                ping_interval=20,
                                compression=None):   # no per-message compression
        await asyncio.Future()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\nHost stopped.')
