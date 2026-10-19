"""Document margins for capped dictation and responsive meeting readers."""
from sayink.ui import design_tokens as tok


def fit_reading_width(view, *, fill_available: bool = False) -> None:
    frame = view.document().rootFrame()
    fmt = frame.frameFormat()
    extra = 0 if fill_available else max(0, view.viewport().width() - tok.READING_MAX_WIDTH)
    if fmt.rightMargin() != extra:
        fmt.setRightMargin(extra)
        frame.setFrameFormat(fmt)
