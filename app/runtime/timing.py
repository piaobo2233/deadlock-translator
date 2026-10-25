"""Receive work has a bounded recovery window, not a forced display delay."""

# Four seconds remains the responsiveness target. It must not cancel a paid
# response merely because OCR consumed part of that target. Live identity and
# foreground checks still cancel work immediately when its bubble disappears.
RECEIVE_TIMEOUT = 12.0
