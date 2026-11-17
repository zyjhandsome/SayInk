"""Windows native platform boundary and headless offscreen fallbacks."""

from __future__ import annotations

# dwExtraInfo on every key SayInk synthesizes (paste, menu mask, replayed tap)
# so its own hotkey listener can tell them apart from keys the user pressed.
# Other tools' injected keys (AutoHotkey remaps) must still drive the hotkey.
SYNTHETIC_KEY_TAG = 0x5341594B

__all__ = ["SYNTHETIC_KEY_TAG"]
