"""Platform-specific helpers."""

# dwExtraInfo on every key SayInk synthesizes (paste, menu mask, replayed tap)
# so its own hotkey listener can tell them apart from keys the user pressed.
# Other tools' injected keys (AutoHotkey remaps) must still drive the hotkey.
SYNTHETIC_KEY_TAG = 0x5341594B
