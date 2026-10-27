#!/usr/bin/env python3
"""
Validate all dependencies and configuration before startup
"""

import sys
import subprocess
import shutil
import os
from pathlib import Path

def check_python_version():
    version = sys.version_info
    if version.major >= 3 and version.minor >= 10:
        print(f"✅ Python {version.major}.{version.minor}.{version.micro}")
        return True
    print(f"❌ Python 3.10+ required (found {version.major}.{version.minor})")
    return False

def check_ffmpeg():
    if shutil.which('ffmpeg'):
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
        version_line = result.stdout.split('\n')[0]
        print(f"✅ {version_line[:60]}")
        return True
    print("❌ FFmpeg not found! Install with: sudo apt install ffmpeg")
    return False

def check_pulseaudio():
    if shutil.which('pulseaudio'):
        print("✅ PulseAudio found")
        return True
    print("⚠️ PulseAudio not found (optional, for screenshare)")
    return True

def check_packages():
    required = ['pyrogram', 'pytgcalls', 'sqlalchemy', 'pydub', 'yt_dlp']
    missing = []
    
    for pkg in required:
        try:
            __import__(pkg)
            print(f"✅ {pkg}")
        except ImportError:
            missing.append(pkg)
            print(f"❌ {pkg}")
    
    return len(missing) == 0

def check_env():
    """Check if .env file exists and has required variables"""
    from dotenv import load_dotenv
    load_dotenv()
    
    required_vars = ['API_ID', 'API_HASH', 'BOT_TOKEN', 'SUDO_USERS']
    missing = []
    
    for var in required_vars:
        value = os.getenv(var)
        if value:
            # Mask sensitive values
            if var in ['API_HASH', 'BOT_TOKEN']:
                print(f"✅ {var} = {value[:10]}...")
            else:
                print(f"✅ {var} = {value}")
        else:
            missing.append(var)
            print(f"❌ {var} - MISSING")
    
    return len(missing) == 0

def check_directories():
    """Create required directories"""
    dirs = ['logs', 'downloads', 'screenshots', 'temp']
    for d in dirs:
        Path(d).mkdir(exist_ok=True)
        print(f"✅ Directory: {d}")
    return True

def main():
    print("\n" + "="*60)
    print("🔍 VCFight V2 - Startup Validation")
    print("="*60 + "\n")
    
    checks = [
        ("Python Version", check_python_version()),
        ("FFmpeg", check_ffmpeg()),
        ("PulseAudio (optional)", check_pulseaudio()),
        ("Python Packages", check_packages()),
        ("Environment Variables", check_env()),
        ("Directories", check_directories())
    ]
    
    print("\n" + "="*60)
    all_passed = all(c[1] for c in checks)
    
    if all_passed:
        print("✅ ALL CHECKS PASSED! Ready to start.")
        print("="*60 + "\n")
        return 0
    else:
        print("❌ SOME CHECKS FAILED! Please fix the issues above.")
        print("\n💡 Quick Fix:")
        print("  • Install FFmpeg: sudo apt install ffmpeg")
        print("  • Install Python packages: pip install -r requirements.txt")
        print("  • Create .env file with required variables")
        print("="*60 + "\n")
        return 1

if __name__ == "__main__":
    sys.exit(main())
