#!/usr/bin/env python3
"""
Generate Pyrogram session string for assistant account.
Uses SAME API_ID + API_HASH from .env when available.
If Telegram sends the code to the app and you never see it,
this script can resend via SMS.
"""

import asyncio
import os
import sys
from pathlib import Path

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from dotenv import load_dotenv
from pyrogram import Client
from pyrogram.errors import (
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberInvalid,
    SessionPasswordNeeded,
    FloodWait,
)

load_dotenv(Path(__file__).parent / ".env")
load_dotenv(Path(__file__).parent / "env")


def _code_type_name(sent) -> str:
    t = getattr(sent, "type", None)
    name = type(t).__name__ if t is not None else "Unknown"
    return name


async def main():
    print("\n" + "=" * 50)
    print("VCFight V2 - Session String Generator")
    print("=" * 50)

    env_id = os.getenv("API_ID", "").strip()
    env_hash = os.getenv("API_HASH", "").strip()

    if env_id and env_hash:
        print(f"\n.env se API mil gaya: API_ID={env_id}")
        use = input("Yahi API use karun? (Y/n): ").strip().lower()
        if use in ("", "y", "yes"):
            api_id = int(env_id)
            api_hash = env_hash
        else:
            api_id = int(input("\nAPI ID: ").strip())
            api_hash = input("API HASH: ").strip()
    else:
        print("\n.env me API_ID/API_HASH nahi mila. Bot wale .env se same values daalo.")
        api_id = int(input("\nAPI ID: ").strip())
        api_hash = input("API HASH: ").strip()

    phone = input("\nAssistant phone (+97798...): ").strip().replace(" ", "")
    if phone and not phone.startswith("+"):
        phone = "+" + phone

    print("\nConnecting...")
    app = Client("session_gen", api_id=api_id, api_hash=api_hash, in_memory=True)

    try:
        await app.connect()
        sent = await app.send_code(phone)
        print(f"\nCode type: {_code_type_name(sent)}")
        print("Agar 'App' / 'SentCodeTypeApp' likha hai to code Telegram APP ke 777000 chat me aata hai.")
        print("SMS me nahi.")

        choice = input(
            "\nKya karna hai?\n"
            "  1 = mere paas code aa gaya, ab enter karunga\n"
            "  2 = code nahi aaya, SMS se dubara bhejo\n"
            "Choice [1/2]: "
        ).strip() or "1"

        if choice == "2":
            try:
                sent = await app.resend_code(phone, sent.phone_code_hash)
                print(f"Resend type: {_code_type_name(sent)}")
                print("Ab SMS / missed-call check karo. 1-2 min wait.")
            except FloodWait as e:
                print(f"FloodWait: {e.value} seconds baad try karo.")
                await app.disconnect()
                return
            except Exception as e:
                print(f"SMS resend fail: {e}")
                print("Phir bhi jo code aaya ho wo try kar sakte ho.")

        code = input("\nConfirmation code (spaces hata ke): ").strip().replace(" ", "")
        if not code:
            print("Code empty. Band.")
            await app.disconnect()
            return

        try:
            await app.sign_in(phone, sent.phone_code_hash, code)
        except SessionPasswordNeeded:
            pwd = input("2FA / cloud password: ").strip()
            await app.check_password(pwd)
        except PhoneCodeInvalid:
            print("Galat code. Dobara python gen_string.py")
            await app.disconnect()
            return
        except PhoneCodeExpired:
            print("Code expire. Dobara python gen_string.py")
            await app.disconnect()
            return

        me = await app.get_me()
        session_string = await app.export_session_string()

        print("\n" + "=" * 50)
        print("SESSION STRING READY")
        print("=" * 50)
        print(session_string)
        print(f"\nAccount: {me.first_name} (@{me.username or 'N/A'}) id={me.id}")
        print("\nBot PM:\n/addstring <upar wali string>")
        print("=" * 50)

        await app.disconnect()
    except PhoneNumberInvalid:
        print("Phone number invalid. +countrycode ke sath likho, jaise +97798...")
    except FloodWait as e:
        print(f"FloodWait: {e.value} seconds baad try karo.")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
