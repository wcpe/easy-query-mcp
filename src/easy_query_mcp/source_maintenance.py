"""Source maintenance helpers for purge and reindex operations."""

from __future__ import annotations

import json
from typing import Optional

from .config import load_source_registry
from .database import Database
from .index_pipeline import IndexPipeline


def purge_source(db: Database, source_id: str, config_path: Optional[str] = None) -> str:
    registry = load_source_registry(config_path)
    source = registry.get(source_id)
    if source is None:
        return json.dumps({
            "status": "error",
            "message": f"Source '{source_id}' not found in registry.",
        }, ensure_ascii=False, indent=2)

    db.initialize()
    existing = db.get_source(source_id)
    if not existing:
        return json.dumps({
            "status": "ok",
            "source_id": source_id,
            "source_type": source.type,
            "purged": {"files": 0, "symbols": 0, "examples": 0, "docs": 0, "cards": 0},
            "message": "Source had no persisted index state.",
        }, ensure_ascii=False, indent=2)

    coverage = _count_source_rows(db, source_id)
    if source.type == "code":
        db.clear_source_files(source_id)
    elif source.type in {"docs", "wiki"}:
        db.clear_source_docs(source_id)
    elif source.type == "knowledge":
        db.clear_source_cards(source_id)
    db.update_source_index_state(source_id, indexed_at="", indexed_rev="")

    return json.dumps({
        "status": "ok",
        "source_id": source_id,
        "source_type": source.type,
        "purged": coverage,
        "message": f"Purged indexed data for source '{source_id}'.",
    }, ensure_ascii=False, indent=2)


def reindex_source(
    db: Database,
    source_id: str,
    config_path: Optional[str] = None,
    changed_only: bool = False,
    sync_first: bool = False,
) -> str:
    registry = load_source_registry(config_path)
    source = registry.get(source_id)
    if source is None:
        return json.dumps({
            "status": "error",
            "message": f"Source '{source_id}' not found in registry.",
        }, ensure_ascii=False, indent=2)

    from .source_manager import SourceManager

    db.initialize()
    sync_result = None
    if sync_first:
        sync_result = SourceManager(config_path, db=db).sync_source(source_id)

    pipeline = IndexPipeline(registry, db)
    stats = pipeline.index(source_id=source_id, changed_only=changed_only)
    coverage = _count_source_rows(db, source_id)
    source_row = db.get_source(source_id) or {}

    return json.dumps({
        "status": "ok",
        "source_id": source_id,
        "source_type": source.type,
        "changed_only": changed_only,
        "sync_first": sync_first,
        "sync_result": sync_result,
        "stats": stats,
        "coverage": coverage,
        "last_indexed_at": source_row.get("last_indexed_at", ""),
        "last_indexed_rev": source_row.get("last_indexed_rev", ""),
    }, ensure_ascii=False, indent=2)


def _count_source_rows(db: Database, source_id: str) -> dict:
    conn = db.connect()
    file_count = conn.execute("SELECT COUNT(*) AS c FROM files WHERE repo_id = ?", (source_id,)).fetchone()["c"]
    symbol_count = conn.execute("SELECT COUNT(*) AS c FROM symbols WHERE repo_id = ?", (source_id,)).fetchone()["c"]
    example_count = conn.execute("SELECT COUNT(*) AS c FROM examples WHERE repo_id = ?", (source_id,)).fetchone()["c"]
    doc_count = conn.execute("SELECT COUNT(*) AS c FROM docs WHERE repo_id = ?", (source_id,)).fetchone()["c"]
    card_count = conn.execute("SELECT COUNT(*) AS c FROM knowledge_cards WHERE source_id = ?", (source_id,)).fetchone()["c"]
    return {
        "files": file_count,
        "symbols": symbol_count,
        "examples": example_count,
        "docs": doc_count,
        "cards": card_count,
    }
