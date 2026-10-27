"""
VCFight V2 - Core Module
Advanced Voice Chat Bot for Telegram
"""

from .assistant import AssistantManager
from .bridge import AudioBridge
from .database import DatabaseManager
from .effects import AudioEffects
from .screenshare import ScreenshareManager

__version__ = "2.0.0"
__author__ = "VCFight Team"

__all__ = [
    'AssistantManager',
    'AudioBridge', 
    'DatabaseManager',
    'AudioEffects',
    'ScreenshareManager'
]
