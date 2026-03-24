"""Helpers for source-aware retrieval across related code/docs/wiki sources."""

from __future__ import annotations

from typing import Callable, Optional

from .database import Database


def resolve_source_scope(db: Database, source_id: Optional[str]) -> dict:
    """Resolve a requested source into its related source family."""
    empty_scope = {
        "requested_id": source_id,
        "anchor_id": source_id,
        "resolved": source_id is None,
        "scope_status": "all_sources" if source_id is None else "degraded",
        "fallback_reason": "" if source_id is None else "source_not_provided",
        "all_ids": None,
        "code_ids": None,
        "docs_ids": None,
        "wiki_ids": None,
        "knowledge_ids": None,
    }
    if not source_id:
        return empty_scope

    source = db.get_source(source_id)
    if not source:
        return {
            **empty_scope,
            "resolved": False,
            "scope_status": "degraded",
            "fallback_reason": "source_not_found",
            "all_ids": [source_id],
            "code_ids": [source_id],
        }

    enabled_sources = db.list_sources(enabled_only=True)
    if not enabled_sources:
        enabled_sources = [source]

    anchor_id = str(source.get("linked_source_id") or source["id"])
    family: list[dict] = [source]
    for item in enabled_sources:
        if item["id"] == source["id"]:
            continue
        if item["id"] == anchor_id or item.get("linked_source_id", "") == anchor_id:
            family.append(item)
        elif source.get("linked_source_id") and item["id"] == source.get("linked_source_id"):
            family.append(item)

    deduped: list[dict] = []
    seen = set()
    for item in family:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        deduped.append(item)

    def _ids_for(kind: str) -> list[str]:
        return [item["id"] for item in deduped if item.get("source_type", "code") == kind]

    return {
        "requested_id": source_id,
        "anchor_id": anchor_id,
        "resolved": True,
        "scope_status": "resolved",
        "fallback_reason": "",
        "all_ids": [item["id"] for item in deduped],
        "code_ids": _ids_for("code"),
        "docs_ids": _ids_for("docs"),
        "wiki_ids": _ids_for("wiki"),
        "knowledge_ids": _ids_for("knowledge"),
    }


def source_ids_for(scope: dict, source_type: Optional[str] = None) -> Optional[list[str]]:
    """Return the source ids for a specific source type within a resolved scope."""
    if not scope:
        return None
    all_ids = scope.get("all_ids")
    if all_ids is None:
        return None
    if source_type is None:
        return list(all_ids or [])
    ids = scope.get(f"{source_type}_ids")
    return list(ids or [])


def row_in_scope(row: dict, scope: dict, keys: tuple[str, ...] = ("source_id", "repo_id")) -> bool:
    """Check whether a result row belongs to the requested source scope."""
    if not scope or scope.get("all_ids") is None:
        return True
    allowed = {item for item in (scope.get("all_ids") or []) if item}
    if not allowed:
        return False
    for key in keys:
        value = row.get(key)
        if value and value in allowed:
            return True
    return False


def merge_source_results(
    source_ids: Optional[list[str]],
    fetcher: Callable[[Optional[str], int], list[dict]],
    key_fn: Callable[[dict], tuple],
    limit: int,
) -> list[dict]:
    """Collect and deduplicate ranked rows across multiple source ids."""
    if source_ids is None:
        return fetcher(None, limit)

    merged: list[dict] = []
    seen = set()
    per_source_limit = max(limit, 8)
    for source_id in source_ids:
        for item in fetcher(source_id, per_source_limit):
            key = key_fn(item)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    merged.sort(key=_default_sort_key, reverse=True)
    return merged[:limit]


def _default_sort_key(item: dict) -> tuple:
    breakdown = item.get("score_breakdown") or {}
    return (
        float(breakdown.get("final_score", 0.0) or 0.0),
        float(item.get("priority", item.get("confidence", 0.0)) or 0.0),
        -float(item.get("stale_score", 0.0) or 0.0),
        str(
            item.get("title")
            or item.get("name")
            or item.get("path")
            or item.get("rel_path")
            or item.get("slug")
            or ""
        ),
    )
