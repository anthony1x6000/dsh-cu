"""Evdev keycodes, US-QWERTY text mapping, and key-chord parsing.

KWin's EIS backend takes Linux evdev keycodes (``linux/input-event-codes.h``),
not keysyms, so typing needs an explicit character table. The table below is the
US-QWERTY layout — the layout the compositor applies is the *session's* layout,
so a non-US layout types different characters; callers that need exact text on a
non-US layout must use the clipboard path (see ``clipboard.py``).

Part of dsh-cu. The keycode tables and chord ordering are derived from kwin-mcp
(MIT, Copyright (c) 2026 Byeonghoon Yoo) — see NOTICE.
"""

from __future__ import annotations

from .rpc import BridgeError

# Named keys, Linux evdev KEY_* codes.
EVDEV_KEYS: dict[str, int] = {
    "return": 28,
    "enter": 28,
    "tab": 15,
    "escape": 1,
    "esc": 1,
    "backspace": 14,
    "delete": 111,
    "space": 57,
    "up": 103,
    "down": 108,
    "left": 105,
    "right": 106,
    "home": 102,
    "end": 107,
    "page_up": 104,
    "pageup": 104,
    "page_down": 109,
    "pagedown": 109,
    "insert": 110,
    "f1": 59,
    "f2": 60,
    "f3": 61,
    "f4": 62,
    "f5": 63,
    "f6": 64,
    "f7": 65,
    "f8": 66,
    "f9": 67,
    "f10": 68,
    "f11": 87,
    "f12": 88,
    "print": 99,
    "scroll_lock": 70,
    "pause": 119,
    "caps_lock": 58,
    "num_lock": 69,
    "menu": 127,
}

# Modifier names to their left-hand evdev codes.
MODIFIER_KEYS: dict[str, int] = {
    "shift": 42,
    "ctrl": 29,
    "control": 29,
    "alt": 56,
    "super": 125,
    "meta": 125,
    "meta_l": 125,
}

# (unshifted, shifted, evdev codes) per physical row, US QWERTY.
_QWERTY_ROWS: tuple[tuple[str, str, list[int]], ...] = (
    ("`1234567890-=", "~!@#$%^&*()_+", [41, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]),
    ("qwertyuiop[]\\", "QWERTYUIOP{}|", [16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 43]),
    ("asdfghjkl;'", 'ASDFGHJKL:"', [30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40]),
    ("zxcvbnm,./", "ZXCVBNM<>?", [44, 45, 46, 47, 48, 49, 50, 51, 52, 53]),
)

# character -> (evdev keycode, needs shift)
CHAR_KEYS: dict[str, tuple[int, bool]] = {}
for _normal, _shifted, _codes in _QWERTY_ROWS:
    for _char, _code in zip(_normal, _codes, strict=True):
        CHAR_KEYS[_char] = (_code, False)
    for _char, _code in zip(_shifted, _codes, strict=True):
        CHAR_KEYS[_char] = (_code, True)
CHAR_KEYS[" "] = (57, False)
CHAR_KEYS["\t"] = (15, False)
CHAR_KEYS["\n"] = (28, False)

# Chords that paste in the terminals whose paste binding is not plain Ctrl+V.
# A terminal that does not bind Ctrl+Shift+V forwards ^V (quoted-insert) into
# the pty, which is unrecoverable, so unknown terminal-ish classes get no chord.
CTRL_SHIFT_V_TERMINALS: frozenset[str] = frozenset({
    "alacritty",
    "com.gexperts.tilix",
    "com.mitchellh.ghostty",
    "foot",
    "footclient",
    "gnome-terminal-server",
    "kitty",
    "org.gnome.console",
    "org.gnome.terminal",
    "org.kde.konsole",
    "org.kde.yakuake",
    "org.wezfurlong.wezterm",
    "st-256color",
    "terminator",
    "xfce4-terminal",
})

# Substrings that mark a terminal-like app whose paste chord is unknown.
UNPASTEABLE_TERMINAL_MARKERS: tuple[str, ...] = ("term", "rxvt")


def key_code(name: str) -> int | None:
    """Resolve one key name (or single character) to an evdev keycode."""
    lowered = name.strip().lower()
    if lowered in EVDEV_KEYS:
        return EVDEV_KEYS[lowered]
    if len(name) == 1 and name in CHAR_KEYS:
        return CHAR_KEYS[name][0]
    return None


def parse_chord(chord: str) -> tuple[list[int], int]:
    """Parse ``"ctrl+shift+v"`` into (modifier codes, main keycode).

    The modifiers keep the order the caller wrote, because a chord's order is
    part of what the target app observes. A chord that names only modifiers
    (``"ctrl"``) presses that modifier alone, matching what the name says.
    """
    if chord.strip() == "":
        raise BridgeError("empty key chord", "BAD_ARGUMENT")
    modifiers: list[int] = []
    main: int | None = None
    for raw in chord.split("+"):
        part = raw.strip()
        if part == "":
            raise BridgeError(f"empty key in chord {chord!r}", "BAD_ARGUMENT")
        lowered = part.lower()
        if lowered in MODIFIER_KEYS:
            modifiers.append(MODIFIER_KEYS[lowered])
            continue
        code = key_code(part)
        if code is None:
            raise BridgeError(
                f"unknown key {part!r}; use a single character or one of "
                f"{', '.join(sorted(EVDEV_KEYS))}",
                "UNKNOWN_KEY",
            )
        main = code
    if main is None:
        if not modifiers:
            raise BridgeError(f"chord {chord!r} names no key", "UNKNOWN_KEY")
        main = modifiers.pop()
    return modifiers, main


def text_plan(text: str) -> list[tuple[int, bool]]:
    """Plan US-QWERTY keystrokes for ``text`` as (keycode, needs_shift) pairs.

    Raises before any input is sent when a character has no US-QWERTY key, so a
    half-typed string can never be the outcome of a rejected request.
    """
    plan: list[tuple[int, bool]] = []
    unmapped: list[str] = []
    for char in text:
        entry = CHAR_KEYS.get(char)
        if entry is None:
            unmapped.append(char)
        else:
            plan.append(entry)
    if unmapped:
        raise BridgeError(
            f"keyboard_type cannot type {''.join(dict.fromkeys(unmapped))!r} on US-QWERTY; "
            "use action=type with unicode=true (clipboard paste)",
            "NOT_TYPEABLE",
            {"unmapped": list(dict.fromkeys(unmapped))},
        )
    return plan


def paste_chord(resource_class: str | None) -> str | None:
    """The paste chord for one window's app class, or None when unsafe.

    None means "do not paste": for a terminal whose binding we do not know, a
    wrong chord injects ``^V`` into the shell, and no later key can undo it.
    """
    if resource_class is None or resource_class.strip() == "":
        return None
    lowered = resource_class.strip().lower()
    if lowered in CTRL_SHIFT_V_TERMINALS:
        return "ctrl+shift+v"
    if any(marker in lowered for marker in UNPASTEABLE_TERMINAL_MARKERS):
        return None
    return "ctrl+v"
