"""Shared pagination helpers for MCP tool responses."""

from __future__ import annotations

from typing import Iterable

MAX_LIMIT = 50
DEFAULT_LIMIT = 20


def clamp_limit(limit: int | None, default: int = DEFAULT_LIMIT, max_limit: int = MAX_LIMIT) -> int:
    if limit is None:
        return default
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return min(value, max_limit)


def clamp_offset(offset: int | None) -> int:
    try:
        value = int(offset or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def paginate_list(items: Iterable[dict], limit: int | None, offset: int | None, *, default_limit: int = DEFAULT_LIMIT, max_limit: int = MAX_LIMIT) -> tuple[list[dict], dict]:
    rows = list(items)
    safe_limit = clamp_limit(limit, default=default_limit, max_limit=max_limit)
    safe_offset = clamp_offset(offset)
    sliced = rows[safe_offset:safe_offset + safe_limit]
    meta = {
        "total": len(rows),
        "limit": safe_limit,
        "offset": safe_offset,
        "returned": len(sliced),
        "has_more": safe_offset + len(sliced) < len(rows),
    }
    return sliced, meta
