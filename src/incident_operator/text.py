"""Small text helpers shared by evidence, operations and the executor."""
from __future__ import annotations


def cap(text: str, limit: int, tail: bool = False) -> tuple[str, bool]:
    """`text` cut to `limit` characters (keeping the end when `tail`: logs, newest lines last), and whether it was
    cut."""
    if len(text) <= limit:
        return text, False
    return (("…" + text[-limit:]) if tail else (text[:limit] + "…")), True


def error_text(e: BaseException, limit: int | None = None) -> str:
    """An exception as stored and shown: its type and message, cut to `limit` characters."""
    return f"{type(e).__name__}: {e}"[:limit]
