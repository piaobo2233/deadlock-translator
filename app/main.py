"""Stable source and frozen application entry."""


def run():
    from app.bootstrap import run as run_application
    return run_application()
