import os
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()
load_dotenv("env")

# ==================== TELEGRAM CONFIGURATION ====================
API_ID = int(os.getenv("API_ID", "34979297") or "34979297")
API_HASH = os.getenv("API_HASH", "0e45dd9855674f34f91705734d7642b4") or "0e45dd9855674f34f91705734d7642b4"

_PLACEHOLDER_TOKENS = {
    "",
    "NEW_TOKEN",
    "NEW_TOKEN_HERE",
    "your-bot-token",
    "YOUR_BOT_TOKEN",
}
_DEFAULT_BOT_TOKEN = "8512231180:AAHwMIOnlQtW_qgpth-9jOH5KyjtL6bfWqk"
_raw_bot_token = (os.getenv("BOT_TOKEN", "") or "").strip().strip('"').strip("'")
if (
    (not _raw_bot_token)
    or (_raw_bot_token in _PLACEHOLDER_TOKENS)
    or ("NEW_TOKEN" in _raw_bot_token)
):
    BOT_TOKEN = _DEFAULT_BOT_TOKEN
else:
    BOT_TOKEN = _raw_bot_token

SUDO_USERS = [int(x.strip()) for x in os.getenv("SUDO_USERS", "8841848847,7450385463,6562455506,8608113180").split(",") if x.strip()]

ASSISTANT_SESSION = (os.getenv("ASSISTANT_SESSION", "") or "").strip()

LOG_CHAT_ID = int(os.getenv("LOG_CHAT_ID", "-1004318913888") or "-1004318913888")

# ==================== AUDIO CONFIGURATION ====================
DEFAULT_RADIO_URL = os.getenv("DEFAULT_RADIO_URL", "https://ice1.somafm.com/groovesalad-128-mp3")
AUDIO_BITRATE = int(os.getenv("AUDIO_BITRATE", "48000"))
AUDIO_CHANNELS = int(os.getenv("AUDIO_CHANNELS", "2"))

# ==================== SCREENSHARE CONFIGURATION ====================
SCREENSHARE_QUALITY = os.getenv("SCREENSHARE_QUALITY", "720p")
SCREENSHARE_FPS = int(os.getenv("SCREENSHARE_FPS", "30"))

# ==================== DATABASE CONFIGURATION ====================
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///vcfight.db")

# ==================== LOGGING CONFIGURATION ====================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
# 5400 sec = 1.5 hours (dedh ghanta)
HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", "5400"))

# ==================== EFFECTS CONFIGURATION ====================
DEFAULT_EFFECTS = {
    'level': int(os.getenv("DEFAULT_LEVEL", "42")),
    'bass': int(os.getenv("DEFAULT_BASS", "0")),
    'treble': int(os.getenv("DEFAULT_TREBLE", "0")),
    'clarity': int(os.getenv("DEFAULT_CLARITY", "0")),
    'gain': int(os.getenv("DEFAULT_GAIN", "3")),
    'muted': False,
}

# ==================== YOUTUBE DOWNLOAD API ====================
# Primary source for /yt (falls back to yt-dlp if this fails or is unset).
SHRUTI_API_URL = (os.getenv("SHRUTI_API_URL", "") or "").strip().rstrip("/")
SHRUTI_API_KEY = (os.getenv("SHRUTI_API_KEY", "") or "").strip()

# ==================== VOICE CHAT CONFIGURATION ====================
AUTO_LEAVE_ON_STOP = os.getenv("AUTO_LEAVE_ON_STOP", "true").lower() == "true"
MAX_BRIDGES = int(os.getenv("MAX_BRIDGES", "5"))

# ==================== ADVANCED CONFIGURATION ====================
TEMP_DIR = os.getenv("TEMP_DIR", "/tmp/vcfight")
SESSION_PREFIX = os.getenv("SESSION_PREFIX", "Assistant_")

Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)
Path("logs").mkdir(exist_ok=True)
Path("downloads").mkdir(exist_ok=True)
Path("screenshots").mkdir(exist_ok=True)

import logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/vcfight.log'),
        logging.StreamHandler()
    ]
)
