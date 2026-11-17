"""Contract-verified in-memory test doubles for SayInk unit and integration tests."""

from __future__ import annotations

from dataclasses import dataclass, field

from sayink.contracts import SecretStoreProtocol
from sayink.platform.window_input import WindowInputBackend, is_terminal_process


@dataclass
class FakeSecretStore(SecretStoreProtocol):
    """Deterministic in-memory ``SecretStoreProtocol`` double."""

    value: str = ""
    fail_reads: bool = False
    fail_writes: bool = False
    writes: list[str] = field(default_factory=list)

    def read(self) -> str | None:
        if self.fail_reads:
            return None
        return self.value

    def write(self, value: str) -> bool:
        if self.fail_writes:
            return False
        self.value = value
        self.writes.append(value)
        return True


@dataclass
class FakeWindowInputBackend(WindowInputBackend):
    """Deterministic ``WindowInputBackend`` double without Win32 or clipboard side effects."""

    foreground: tuple[int, str, int] = (1001, "Editor", 4242)
    process_name: str = "Editor"
    selected_text: str | None = None
    sent_chords: list[str] = field(default_factory=list)
    fail_send: bool = False

    def get_foreground_window_info(self) -> tuple[int, str, int]:
        return self.foreground

    def foreground_target(self) -> tuple[int, str]:
        return int(self.foreground[0]), self.process_name

    def send_paste_shortcut(self, mode: str = "ctrl_v") -> None:
        if self.fail_send:
            raise OSError("simulated paste failure")
        self.sent_chords.append(mode)

    def read_selected_text(self, process_name: str = "") -> str | None:
        if is_terminal_process(process_name) and self.selected_text is None:
            return None
        return self.selected_text


@dataclass
class ControllableClock:
    """Monotonic test clock advancing deterministically without real sleeps."""

    now: float = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


__all__ = [
    "ControllableClock",
    "FakeSecretStore",
    "FakeWindowInputBackend",
]
