"""Shared line length for saved dictation and meeting documents."""
from sayink.ui import design_tokens as tok


def fit_reading_width(view) -> None:
    frame = view.document().rootFrame()
    fmt = frame.frameFormat()
    extra = max(0, view.viewport().width() - tok.READING_MAX_WIDTH)
    if fmt.rightMargin() != extra:
        fmt.setRightMargin(extra)
        frame.setFrameFormat(fmt)
