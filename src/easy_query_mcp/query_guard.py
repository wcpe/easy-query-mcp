"""Shared output truncation and query guard helpers."""

from __future__ import annotations

MAX_CALL_CHAIN_DEPTH = 4
MAX_CALLERS_PER_LAYER = 8
MAX_RELATED_FILES = 12
MAX_ENTRYPOINTS = 10
MAX_MODULES = 12
MAX_DOCS = 8
MAX_DOMAIN_TERMS = 12
MAX_MATCH_LINES = 3


def clamp_depth(depth: int | None, default: int = 3, max_depth: int = MAX_CALL_CHAIN_DEPTH) -> int:
    try:
        value = int(depth or default)
    except (TypeError, ValueError):
        return default
    if value < 0:
        return default
    return min(value, max_depth)


def trim_list(items: list, max_items: int) -> tuple[list, bool]:
    if len(items) <= max_items:
        return items, False
    return items[:max_items], True


def truncation_meta(*, truncated: bool, reason: str = "", limits: dict | None = None) -> dict:
    return {
        "truncated": truncated,
        "reason": reason if truncated else "",
        "limits": limits or {},
    }
