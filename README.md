# SMR-THAKUR-VC-FYT-V2

> **Telegram Voice Chat Bridge Bot** — real-time audio forwarding, live DSP processing, screenshare, and multi-assistant session management for Telegram group voice chats.

[![Deploy](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy)

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Audio Engine](#audio-engine)
- [Installation](#installation)
- [Configuration](#configuration)
- [Commands Reference](#commands-reference)
- [DSP Chain — Technical Details](#dsp-chain--technical-details)
- [Presets](#presets)
- [Multi-Session Management](#multi-session-management)
- [Screenshare & Camera](#screenshare--camera)
- [Deployment](#deployment)
- [File Structure](#file-structure)

---

## Overview

SMR-THAKUR-VC-FYT-V2 is a Telegram userbot/bot hybrid that bridges audio between Telegram group voice chats with a full real-time DSP processing pipeline. It is designed for competitive voice chat use cases — the audio engine applies gain, EQ, compression, and limiting in-process, at 48 kHz, with no external DSP dependency.

Key capabilities:

- **Audio bridge** — forward live microphone audio between two groups simultaneously
- **Real-time DSP** — per-frame processing: EQ → compressor → lookahead limiter
- **Multi-assistant** — multiple Telegram user sessions, each with independent effects chains
- **Screenshare / camera** — stream desktop or video into a voice chat
- **YouTube & radio** — pull audio from YouTube URLs or internet radio into VC
- **Live recording** — capture and export ongoing VC audio

---

## Architecture

```
main.py
  └── Pyrogram Bot Client
  └── Assistant Manager (core/user_sessions.py)
        └── N × Pyrogram User Client  ←─ each assistant joins VC independently
              └── AudioBridge (core/bridge.py)
                    └── LiveEQ (core/effects.py)  ←─ DSP chain per assistant
                          └── PyTgCalls sink
```

The bot client handles commands. Each assistant client owns one `AudioBridge`, which owns one `LiveEQ` instance. Audio flows:

```
Microphone PCM (int16, 48kHz, stereo interleaved)
  → LiveEQ.process_int16()
      → DC / rumble block
      → Bass shelf
      → Treble shelf
      → Presence band
      → Chest / warmth band
      → Harmonic exciter (bass only)
      → Pre-gain multiply
      → Echo delay mix (optional)
      → Compressor (block-peak, linked stereo)
      → Tanh saturation (makeup)
      → Lookahead limiter (1-block, CEIL = 0.9995)
  → PyTgCalls → Telegram VC
```

---

## Audio Engine

The DSP core lives in `core/effects.py`. All processing is vectorised NumPy — no scipy, no external audio libraries.

### Signal chain

| Stage | Class / method | Purpose |
|---|---|---|
| DC block | `_OnePole(30 Hz)` | Remove DC offset and sub-20Hz rumble |
| Bass shelf | `_OnePole(55 Hz)` × 2 | Low-frequency boost (0–22 dB) |
| Treble shelf | `_OnePole(5500 Hz)` | High-frequency boost (0–16 dB) |
| Presence | `_OnePole(5500) - _OnePole(800)` | Bandpass around voice fundamentals |
| Warmth | `_OnePole(420) - _OnePole(75)` | Chest / body resonance (0–24 dB) |
| Harmonic exciter | `tanh(8×low) - LP(tanh(...))` | Adds upper harmonics to bass band |
| Pre-gain | multiply | `vol = 10^(pre_db/20)`, pre_db = gain×0.8 + loudness×0.4 |
| Echo | circular buffer, 50ms | Mix delayed signal (0–18% + 50% of echo param) |
| Compressor | block-peak (48-sample blocks) | Ratio up to 153×, linked stereo |
| Tanh saturation | `tanh(drive×x) / tanh(drive)` | Makeup + harmonic density |
| Lookahead limiter | 1-block lookahead, linear ramps | Hard ceiling at 0.9995 FS, no clipping |
| Pitch shifter | `_PitchShifter` (crossfading delay) | Optional voice deepening |

### Clarity patch (v8)

The default config was updated from all-maxed values to tuned values after analysis of a real recording showed 8.08% hard clipping and a 3.6 dB crest factor (severely over-compressed):

| Parameter | v7 (old) | v8 (current) | Reason |
|---|---|---|---|
| `gain` | 200 | 130 | Headroom for compressor |
| `loudness` | 100 | 82 | Avoid brick-wall saturation |
| `bass` | 100 | 65 | Reduce 80–300 Hz mud |
| `treble` | 85 | 85 | Unchanged |
| `clarity` | 100 | 92 | Presence shelf handles this |
| Compressor release | 18 ms | 80 ms | Consonants no longer squashed |
| Dynamics passes | 2× | 1× | Single pass — clarity over density |

Result on the same recording: clipping 8.08% → **0.00%**, mid-voice energy 33.7% → **38.2%**, low-mid mud 30.5% → **22.9%**.

---

## Installation

### Prerequisites

- Python 3.10+
- FFmpeg (system package)
- A Telegram API ID and hash — obtain from [my.telegram.org](https://my.telegram.org)
- A Telegram bot token — obtain from [@BotFather](https://t.me/BotFather)
- One or more Telegram user accounts for assistant sessions

### Steps

```bash
# 1. Clone
git clone https://github.com/yourfork/SMR-THAKUR-VC-FYT-V2.git
cd SMR-THAKUR-VC-FYT-V2

# 2. Install dependencies
pip install -r requirements.txt

# 3. System packages (Debian / Ubuntu)
apt-get install -y ffmpeg

# 4. Configure environment
cp .env.example .env
nano .env          # fill in credentials — see Configuration section

# 5. Validate startup
python validate_startup.py

# 6. Run
python main.py
```

---

## Configuration

Copy `.env.example` to `.env` and fill in the values below. **Never commit `.env` to version control.**

```env
# ── Telegram ──────────────────────────────────────────────────
API_ID=your_api_id
API_HASH=your_api_hash
BOT_TOKEN=your_bot_token

# Comma-separated Telegram user IDs who can control the bot
SUDO_USERS=123456789,987654321

# ── Assistant sessions ─────────────────────────────────────────
# Pyrogram string session for the userbot account that joins VC.
# Generate with: python gen_string.py
ASSISTANT_SESSION=your_pyrogram_string_session

# ── Audio ──────────────────────────────────────────────────────
AUDIO_BITRATE=48000
AUDIO_CHANNELS=2
DEFAULT_RADIO_URL=https://ice1.somafm.com/groovesalad-128-mp3

# ── YouTube download API ───────────────────────────────────────
# Optional: faster YouTube fetching via external API
ARU_YT_API=https://your-service.up.railway.app
ARU_API_KEY=your-api-key

# ── Screenshare ────────────────────────────────────────────────
SCREENSHARE_QUALITY=720p
SCREENSHARE_FPS=30

# ── Misc ───────────────────────────────────────────────────────
LOG_LEVEL=INFO
MAX_BRIDGES=5
AUTO_LEAVE_ON_STOP=true
TEMP_DIR=/tmp/vcfight
```

### Generating an assistant session string

```bash
python gen_string.py
```

Follow the prompts. Paste the output string into `ASSISTANT_SESSION` in `.env`.

---

## Commands Reference

All commands are group-only unless noted. SUDO access required for most audio controls.

### Voice Chat

| Command | Description |
|---|---|
| `/join` | Join the group's voice chat as the assistant |
| `/joinvc` / `/vc` | Alias for `/join` |
| `/joins` | Join with source bridge (audio forwarding mode) |
| `/vcstatus` | Show current VC state |

### Audio Bridge

| Command | Description |
|---|---|
| `/radio <url>` | Stream internet radio into VC |
| `/yt <url or query>` | Play YouTube audio in VC |
| `/youtube <url>` | Alias for `/yt` |
| `/mute` | Mute the bridge output |
| `/unmute` | Unmute |
| `/automute on\|off` | Auto-mute on next `/join` |

### DSP Effects (0–100 scale unless noted)

| Command | Description |
|---|---|
| `/gain <value>` | Pre-gain (0–200). Raises overall signal level before DSP chain |
| `/loudness <value>` | Post-compressor loudness (drives compressor ratio and makeup) |
| `/volume <value>` | Fine-grained loudness dial (0–400), maps to `loudness` parameter |
| `/bass <value>` | Low-frequency shelf boost (0–22 dB at 110 Hz) |
| `/treble <value>` | High-frequency shelf boost (0–16 dB at 6500 Hz) |
| `/clarity <value>` | Presence band boost (0–14 dB around 3500 Hz) |
| `/echo <value>` | Echo / reverb mix (delay ≈ 160ms, 0 = off) |
| `/pitch <value>` | Voice pitch deepening (0 = natural, 100 = deepest) |
| `/swap` / `/swap 1\|0` | Swap stereo L↔R channels |
| `/voice female\|male\|normal` | Quick tone preset (adjusts bass/treble only) |
| `/effects` | Show current effect values |
| `/eq` / `/equalizer` | Open interactive EQ panel (inline keyboard) |
| `/preset <name>` | Apply a named preset — see [Presets](#presets) |
| `/reset` | Reset all effects to defaults |

### Screenshare & Camera

| Command | Description |
|---|---|
| `/screenshare` | Start desktop screenshare into VC |
| `/screenshareoff` | Stop screenshare |
| `/camera` | Stream webcam into VC |
| `/screencamera` | Combined screen + camera overlay |
| `/livescreenshare` | Low-latency live screenshare mode |
| `/stoplivescreenshare` | Stop live screenshare |
| `/rcplay` | Play from remote camera/stream source |
| `/rcplayoff` | Stop remote play |

### Recording

| Command | Description |
|---|---|
| `/startrecord` | Begin recording VC audio |
| `/stoprecord` | Stop and export recording |

### Admin / Session Management

| Command | Description |
|---|---|
| `/addstring` / `/addsession` | Add a new assistant session string |
| `/delsession` / `/dlsession` | Remove an assistant session |
| `/assistants` | List active assistant sessions |
| `/removeassistant` | Force-remove an assistant from VC |
| `/sudoadd <user_id>` | Grant sudo access |
| `/sudodel <user_id>` | Revoke sudo access |
| `/sudolist` | List sudo users |
| `/broadcast <text>` | Broadcast message to all active chats |
| `/restart` | Restart the bot process |
| `/logs` | Fetch recent log file |
| `/status` | Bot health and uptime |
| `/help` / `/cmds` / `/commands` | Show help |

---

## DSP Chain — Technical Details

### `_OnePole` filter

A stateful one-pole IIR low-pass. State is carried between frames so there are no clicks at chunk boundaries. Computed with the cumsum trick for full vectorisation:

```
y[n] = a·y[n-1] + (1-a)·x[n]
a = exp(-2π·fc/fs)
```

### Compressor

Block-peak compressor, block size = 48 samples (1 ms @ 48 kHz). Gain is linked across stereo channels. Attack = 5 ms, release = 80 ms. Ratio and threshold are derived from the UI values:

```python
ratio     = 3.0 + 150.0 × t          # up to 153:1
comp_thr  = 0.20 × exp(-0.75 × t)    # exponential decay to near-zero
makeup_db = 120.0 × t                 # dB of makeup gain
t         = sqrt((gain + loudness) / 100.0)
```

`t` uses square-root growth so audible changes continue across the full knob range before the limiter pins.

### Lookahead limiter

One-block (48-sample) lookahead. Ceiling = 0.9995 FS (1 LSB from digital clip). Gain ramps are linear per-sample to avoid zipper noise. No sample ever exceeds the ceiling regardless of input level.

### Pitch shifter (`_PitchShifter`)

Two-tap crossfading delay line. Window = 1920 samples (≈ 40 ms). State is continuous across frames — no dropped audio, no chunk-size dependency. Ratio < 1 → deeper voice. Latency ≈ 20 ms.

---

## Presets

Apply with `/preset <name>`.

| Preset | Gain | Loudness | Bass | Treble | Clarity | Pitch | Character |
|---|---|---|---|---|---|---|---|
| `nuclear` | 200 | 100 | 100 | 80 | 90 | 0 | Maximum everything |
| `james` | 100 | 100 | 90 | 45 | 65 | 0 | Dense, saturated, loud |
| `phonemic` | 130 | 82 | 65 | 85 | 92 | 0 | Default — loud + clear |
| `bambam` | 95 | 90 | 100 | 25 | 50 | 35 | Booming, deep, punchy |
| `heavy` | 85 | 80 | 95 | 20 | 45 | 30 | Deep voice |
| `dj` | 90 | 85 | 90 | 40 | 60 | 0 | Full loud, natural pitch |
| `fight` | 75 | 60 | 80 | 25 | 50 | 0 | Strong and clear |
| `voice` | 55 | 70 | 20 | 35 | 70 | 0 | Clean natural voice |
| `default` | 130 | 82 | 65 | 85 | 92 | 0 | Same as phonemic |

---

## Multi-Session Management

The bot supports multiple Telegram user accounts (assistants). Each assistant joins a VC independently and has its own `AudioBridge` and `LiveEQ` instance with its own saved effect configuration.

Adding a second assistant:

```
/addsession
```

The bot will prompt for a Pyrogram string session. Generate one with `python gen_string.py` from a new account.

Each sudo user controls their own assistant's effects — `/gain`, `/bass`, `/preset`, etc. only affect the assistant tied to that sudo user's account. The owner's bridge is unaffected.

---

## Screenshare & Camera

Screenshare uses FFmpeg to capture the display and pipe frames into PyTgCalls. Camera uses a connected capture device.

Requirements (Linux):
```bash
# X11 display (for screenshare)
export DISPLAY=:0

# System packages
apt-get install -y ffmpeg v4l-utils
```

Quality is configurable via `.env`:
```env
SCREENSHARE_QUALITY=720p   # 480p / 720p / 1080p
SCREENSHARE_FPS=30
```

---

## Deployment

### Heroku

Click the deploy button at the top of this file. Set config vars in the Heroku dashboard — same keys as `.env`.

### Docker

```bash
docker build -t vcfight .
docker run -d --env-file .env vcfight
```

### Systemd (VPS)

```bash
cp vcfight.service /etc/systemd/system/
# Edit WorkingDirectory and ExecStart paths in the service file
systemctl daemon-reload
systemctl enable vcfight
systemctl start vcfight
```

### Railway / Render

Set the same environment variables from `.env.example` in the platform dashboard. The `Procfile` is already configured:

```
worker: python main.py
```

---

## File Structure

```
SMR-THAKUR-VC-FYT-V2/
├── main.py                     # Entry point — initialises bot, assistants, plugins
├── validate_startup.py         # Pre-flight checks before main.py
├── gen_string.py               # Generates Pyrogram session strings
├── config/
│   ├── settings.py             # All config loaded from env vars
│   └── __init__.py
├── core/
│   ├── effects.py              # DSP engine — LiveEQ, AudioEffects, presets
│   ├── audio_effects.py        # Command handlers for all /eq /gain /bass etc.
│   ├── bridge.py               # AudioBridge — connects LiveEQ to PyTgCalls
│   ├── bridge_patch.py         # Monkey-patches for PyTgCalls edge cases
│   ├── bridge_state.py         # Global live bridge registry
│   ├── user_bridges.py         # Per-user bridge registry
│   ├── user_effects.py         # Per-user effect config persistence
│   ├── user_sessions.py        # Assistant session lifecycle
│   ├── assistant.py            # Single assistant wrapper
│   ├── automute_guard.py       # Auto-mute / force-unmute logic
│   ├── database.py             # SQLAlchemy DB (session storage)
│   ├── media_state.py          # Current media play state
│   ├── media_toggle.py         # Play / pause / stop
│   ├── rcplay.py               # Remote camera play
│   ├── recorder.py             # VC recording
│   ├── screenshare.py          # Desktop screenshare via FFmpeg
│   ├── livescreenshare.py      # Low-latency screenshare variant
│   ├── video_orient.py         # Video orientation helpers
│   ├── vc_leave.py             # Leave VC helpers
│   ├── vc_mute.py              # Mute / unmute VC
│   ├── runtime_vars.py         # In-memory runtime state
│   ├── sudo.py                 # Sudo user check and registry
│   ├── effects.py              # (see above)
│   └── mtproto_patch.py        # MTProto-level patches
├── plugins/
│   ├── stream_commands.py      # /radio /yt
│   ├── vc_commands.py          # /join /screenshare /camera
│   ├── audio_effects.py        # /eq /gain /bass etc. (registers handlers)
│   ├── fx_ui.py                # Inline keyboard for EQ panel
│   ├── bridge_commands.py      # Bridge join/leave
│   ├── admin_commands.py       # /restart /logs /broadcast /sudo*
│   ├── extra_commands.py       # /start /help /status
│   ├── livescreenshare_commands.py
│   ├── media_toggle_commands.py
│   └── __init__.py
├── assets/                     # Static assets
├── .env.example                # Environment variable template
├── requirements.txt
├── Dockerfile
├── Procfile
├── vcfight.service             # Systemd unit file
└── runtime.txt                 # Python version pin (Heroku)
```

---

## Notes

- Secrets (API keys, bot token, session strings) are **never stored in this repository**. Use `.env` only.
- The `_v` field in saved effect configs is a version number. Bumping `CONFIG_VERSION` in `effects.py` triggers automatic migration of all saved user configs to the new defaults on next load.
- The compressor ratio and threshold are derived from `gain` and `loudness` together — raising either knob increases compression aggressiveness, not just signal level.
- The lookahead limiter is the only hard ceiling in the chain. All parameters before it are uncapped by design — pushing values past 100 keeps working, it just drives the limiter harder until output is indistinguishable from fully-pinned.
