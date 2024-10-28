# NakDesk

Remote desktop tool — stream and control any screen over LAN or the internet. No accounts, no cloud, no BS.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

## How it works

- **Host** — runs on the machine you want to control. Streams the screen and executes incoming mouse/keyboard commands.
- **Client** — runs on your controlling machine. Shows the remote screen and forwards your inputs.

Communication is over WebSockets. Cross-network access is handled via [ngrok](https://ngrok.com) TCP tunnels (optional).

## Requirements

- Python 3.10+
- pip packages (see below)
- **macOS only:** Screen Recording + Accessibility permissions for Terminal/Python

## Installation

```bash
git clone https://github.com/NadirAliOfficial/nakdesk
cd nakdesk
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Usage

### On the host machine (to be controlled)

```bash
python host.py
```

Output:
```
────────────────────────────────────────────
  NakDesk Host  —  ready
  Local  →  192.168.1.10:9000
  Public →  4.tcp.ngrok.io:12345  (share this)
────────────────────────────────────────────
```

Share the **Local** address for LAN, or the **Public** address for internet access.

### On the client machine (controller)

```bash
python client.py
```

Click **Connect** and enter the host address (e.g. `192.168.1.10:9000` or `4.tcp.ngrok.io:12345`).

You can also connect directly from the command line:

```bash
python client.py 192.168.1.10 9000
```

## Cross-network (internet) setup

1. Sign up at [ngrok.com](https://ngrok.com) and get an auth token
2. `ngrok authtoken YOUR_TOKEN`
3. Run `host.py` — it auto-starts an ngrok TCP tunnel and prints the public address

If ngrok is not installed, local LAN still works normally.

## macOS permissions

On macOS, the host needs two permissions. It will warn you at startup if either is missing.

| Permission | Where to grant |
|---|---|
| Screen Recording | System Settings → Privacy & Security → Screen Recording → add Terminal |
| Accessibility | System Settings → Privacy & Security → Accessibility → add Terminal |

Restart Terminal after granting, then rerun `host.py`.

## Features

- Full HD screen streaming (1920px wide, 75% JPEG quality)
- 30 FPS capture, ~70 FPS client render
- Mouse move, click (left/right), scroll
- Keyboard input (all standard keys + F1–F8, modifiers)
- Clipboard sync — copy to/from remote
- Fullscreen mode (F11 or button)
- LAN + internet (ngrok TCP) support

## Known limitations

- **Same machine testing:** running host and client on the same machine creates a cursor feedback loop and an infinite mirror effect. This is expected — use two separate machines for real usage.
- Clipboard sync requires `xclip` or `xsel` on Linux.

## Windows

Works out of the box — no extra permissions needed. Run `host.py` in any terminal.

## License

MIT
