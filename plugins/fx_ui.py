from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

_SC = str.maketrans(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "ᴀʙᴄᴅᴇၾɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢᴀʙᴄᴅᴇၾɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢ",
)

FX_KEYS = ("gain", "loudness", "bass", "pitch", "treble", "clarity", "echo")
FX_VALUES = (10, 20, 30, 40, 50, 60, 70, 80, 90, 100)
# Extended button rows for gain/loudness (uncapped — goes beyond 100)
FX_VALUES_EXTENDED = (50, 100, 150, 200, 250, 300, 400, 500)
FX_UNCAPPED = {"gain", "loudness"}   # these use FX_VALUES_EXTENDED in their sub-panel
ALERT_NOSUDO = "ʏᴏᴜ ᴀʀᴇ ɴᴏᴛ ᴀ sᴜᴅᴏ ᴜ sᴇʀ"


def _sc(text: str) -> str:
    return (text or "").translate(_SC)


def current_fx(config: dict) -> dict:
    c = config or {}
    return {
        "gain": float(c.get("gain", 0)),
        "loudness": float(c.get("loudness", 0)),
        "bass": float(c.get("bass", 0)),
        "pitch": float(c.get("pitch", 0)),
        "treble": float(c.get("treble", 0)),
        "clarity": float(c.get("clarity", 0)),
        "echo": float(c.get("echo", 0)),
    }


def _fx_body(config: dict) -> str:
    fx = current_fx(config)
    return (
        f"{_sc('gain')} {int(fx['gain'])} • {_sc('loudness')} {int(fx['loudness'])}\n"
        f"{_sc('bass')} {int(fx['bass'])} • {_sc('echo')} {int(fx['echo'])}\n"
        f"{_sc('pitch')} {int(fx['pitch'])} • {_sc('treble')} {int(fx['treble'])}\n"
        f"{_sc('clarity')} {int(fx['clarity'])}\n"
        f"{_sc('gain/loudness')} 0–∞ • {_sc('rest')} 0–100"
    )


def fx_panel_caption(config: dict) -> str:
    return (
        f"🎛️ **{_sc('equaliser')}**\n\n"
        f"{_sc('button se value set karo')}\n"
        f"{_sc('ya command')}: `/gain 70` `/bass 80`\n"
        f"<blockquote>{_fx_body(config)}</blockquote>"
    )


def value_caption(kind: str, config: dict) -> str:
    fx = current_fx(config)
    cur = int(fx.get(kind, 0))
    if kind in FX_UNCAPPED:
        range_hint = f"{_sc('uncapped — type')} `/{kind} 300` {_sc('for higher')}"
    else:
        range_hint = f"{_sc('10 mild')} • {_sc('100 max')}"
    return (
        f"🎛️ {_sc(kind)}\n"
        f"<blockquote>"
        f"{_sc('current')} {cur}\n"
        f"{range_hint}"
        f"</blockquote>"
    )


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(_sc("gain"), callback_data="fxm:gain:0"),
            InlineKeyboardButton(_sc("loudness"), callback_data="fxm:loudness:0"),
        ],
        [
            InlineKeyboardButton(_sc("bass"), callback_data="fxm:bass:0"),
            InlineKeyboardButton(_sc("echo"), callback_data="fxm:echo:0"),
        ],
        [
            InlineKeyboardButton(_sc("pitch"), callback_data="fxm:pitch:0"),
            InlineKeyboardButton(_sc("treble"), callback_data="fxm:treble:0"),
        ],
        [
            InlineKeyboardButton(_sc("clarity"), callback_data="fxm:clarity:0"),
        ],
    ])


def values_keyboard(kind: str) -> InlineKeyboardMarkup:
    if kind not in FX_KEYS:
        kind = "gain"
    if kind in FX_UNCAPPED:
        # Extended rows for gain/loudness — goes 50, 100 ... 500
        vals = FX_VALUES_EXTENDED
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(str(v), callback_data=f"fxs:{kind}:{v}") for v in vals[0:4]],
            [InlineKeyboardButton(str(v), callback_data=f"fxs:{kind}:{v}") for v in vals[4:8]],
            [InlineKeyboardButton(_sc("back"), callback_data="fxb")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(str(v), callback_data=f"fxs:{kind}:{v}") for v in FX_VALUES[0:4]],
        [InlineKeyboardButton(str(v), callback_data=f"fxs:{kind}:{v}") for v in FX_VALUES[4:8]],
        [InlineKeyboardButton(str(v), callback_data=f"fxs:{kind}:{v}") for v in FX_VALUES[8:10]],
        [InlineKeyboardButton(_sc("back"), callback_data="fxb")],
    ])
