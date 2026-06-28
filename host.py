"""
NakDesk Host — runs on the PC to be controlled.
"""

import asyncio
import json
import platform
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

_IS_WIN = platform.system() == 'Windows'

if _IS_WIN:
    import ctypes
    import ctypes.wintypes as _wt

    class _BMIH(ctypes.Structure):
        _fields_ = [
            ('biSize',          _wt.DWORD), ('biWidth',         _wt.LONG),
            ('biHeight',        _wt.LONG),  ('biPlanes',        _wt.WORD),
            ('biBitCount',      _wt.WORD),  ('biCompression',   _wt.DWORD),
            ('biSizeImage',     _wt.DWORD), ('biXPelsPerMeter', _wt.LONG),
            ('biYPelsPerMeter', _wt.LONG),  ('biClrUsed',       _wt.DWORD),
            ('biClrImportant',  _wt.DWORD),
        ]
    class _BMI(ctypes.Structure):
        _fields_ = [('bmiHeader', _BMIH), ('bmiColors', _wt.DWORD * 3)]
    class _CI(ctypes.Structure):
        _fields_ = [('cbSize', _wt.DWORD), ('flags', _wt.DWORD),
                    ('hCursor', _wt.HANDLE), ('ptScreenPos', _wt.POINT)]
    _u32 = ctypes.windll.user32
    _gdi = ctypes.windll.gdi32

    def _grab_with_cursor(left, top, w, h):
        hdc_src = _u32.GetDC(0)
        hdc_dst = _gdi.CreateCompatibleDC(hdc_src)
        hbm     = _gdi.CreateCompatibleBitmap(hdc_src, w, h)
        _gdi.SelectObject(hdc_dst, hbm)
        _gdi.BitBlt(hdc_dst, 0, 0, w, h, hdc_src, left, top, 0x00CC0020)

        bmi = _BMI()
        bmi.bmiHeader.biSize     = ctypes.sizeof(_BMIH)
        bmi.bmiHeader.biWidth    = w
        bmi.bmiHeader.biHeight   = -h   # top-down
        bmi.bmiHeader.biPlanes   = 1
        bmi.bmiHeader.biBitCount = 32
        buf = (ctypes.c_char * (w * h * 4))()
        _gdi.GetDIBits(hdc_dst, hbm, 0, h, buf, ctypes.byref(bmi), 0)

        _gdi.DeleteObject(hbm)
        _gdi.DeleteDC(hdc_dst)
        _u32.ReleaseDC(0, hdc_src)

        return np.frombuffer(buf, dtype=np.uint8).reshape((h, w, 4))[:, :, :3]

    _IDC = {32512:0, 32513:1, 32649:2, 32514:3, 32646:4,
            32645:5, 32644:5, 32643:5, 32642:5}

    def _cursor_type():
        try:
            ci = _CI()
            ci.cbSize = ctypes.sizeof(_CI)
            if not _u32.GetCursorInfo(ctypes.byref(ci)):
                return 0
            for idc, t in _IDC.items():
                h = _u32.LoadCursorW(0, idc)
                if h == ci.hCursor:
                    return t
        except Exception:
            pass
        return 0

else:
    def _cursor_type(): return 0

PORT    = 9000
FPS     = 30
QUALITY = 25
WIDTH   = 800

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


async def capture_loop(ws, stop, sw, sh):
    dw         = min(WIDTH, sw)
    dh         = int(sh * dw / sw)
    enc_params = [cv2.IMWRITE_JPEG_QUALITY, QUALITY]
    header_pre = struct.pack('!HH', sw, sh)
    interval   = 1.0 / FPS
    fq         = asyncio.Queue(maxsize=1)

    async def _capture():
        with mss.MSS() as sct:
            mon    = sct.monitors[1]
            wl, wt = mon['left'], mon['top']
            while not stop.is_set():
                t0 = time.perf_counter()
                try:
                    if _IS_WIN:
                        try:
                            frame = _grab_with_cursor(wl, wt, sw, sh)
                        except Exception:
                            shot  = sct.grab(mon)
                            frame = np.frombuffer(shot.raw, dtype=np.uint8).reshape(
                                (shot.height, shot.width, 4))[:, :, :3]
                    else:
                        shot  = sct.grab(mon)
                        frame = np.frombuffer(shot.raw, dtype=np.uint8).reshape(
                            (shot.height, shot.width, 4))[:, :, :3]
                    if frame.shape[1] != dw:
                        frame = cv2.resize(frame, (dw, dh),
                                           interpolation=cv2.INTER_LINEAR)
                    _, buf  = cv2.imencode('.jpg', frame, enc_params)
                    payload = (b'F' + struct.pack('!I', len(buf))
                               + header_pre + buf.tobytes())
                    try: fq.get_nowait()
                    except asyncio.QueueEmpty: pass
                    fq.put_nowait(payload)
                except Exception:
                    break
                dt = time.perf_counter() - t0
                await asyncio.sleep(max(0, interval - dt))

    async def _send():
        last_ct = -1
        while not stop.is_set():
            try:
                payload = await asyncio.wait_for(fq.get(), timeout=1.0)
                await ws.send(payload)
                ct = _cursor_type()
                if ct != last_ct:
                    last_ct = ct
                    await ws.send(json.dumps({'t': 'cursor', 'c': ct}))
            except asyncio.TimeoutError:
                continue
            except Exception:
                break

    await asyncio.gather(_capture(), _send())


async def handler(ws):
    sw, sh = screen_size()
    stop   = asyncio.Event()
    task   = asyncio.create_task(capture_loop(ws, stop, sw, sh))
    print(f'[+] {ws.remote_address[0]} connected')
    try:
        async for msg in ws:
            if isinstance(msg, str):
                try:
                    cmd = json.loads(msg)
                    t   = cmd.get('t')
                    if t == 'cb_get':
                        await ws.send(json.dumps(
                            {'t': 'cb', 'text': pyperclip.paste()}))
                    elif t != 'ack':
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
        import configparser, os
        cfg = configparser.ConfigParser()
        cfg.read(os.path.join(os.path.dirname(__file__), 'config.ini'))
        domain = cfg.get('ngrok', 'domain', fallback=None)
        kwargs = {'domain': domain} if domain else {}
        t   = ngrok.connect(PORT, 'http', **kwargs)
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
