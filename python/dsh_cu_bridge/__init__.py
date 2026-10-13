"""dsh-cu bridge: KDE Plasma computer use for DeepSeek Harness.

The bridge is a resident JSON-RPC process over stdio. Every desktop capability
lives here — KWin window control, screenshots, EIS input injection, the AT-SPI
accessibility tree, flatpak identity and launch, and the clipboard — while the
TypeScript plugin owns the model-facing tool surface.

Nothing here trusts a guessed coordinate: an operation either proves where it
acts (a KWin rectangle, a matched accessibility element, a captured frame with a
stated origin) or it reports why it cannot.

Part of dsh-cu, derived from kwin-mcp (MIT, Copyright (c) 2026 Byeonghoon Yoo)
— see NOTICE.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
