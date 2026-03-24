from __future__ import annotations

import argparse
import json
from pathlib import Path

from easy_query_mcp.config import load_source_registry
from easy_query_mcp.database import Database
from easy_query_mcp.tools import get_examples, search_symbol
from easy_query_mcp.tools_advanced import search_by_concept
from easy_query_mcp.tools_docs import read_doc, search_docs
from easy_query_mcp.tools_knowledge import search_knowledge_cards


def _parse_payload(raw: str) -> dict:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise AssertionError(f"Expected JSON object, got: {type(payload).__name__}")
    return payload


def _assert_ok(name: str, payload: dict) -> dict:
    status = payload.get("status")
    if status != "ok":
        raise AssertionError(f"{name} failed with status={status}: {json.dumps(payload, ensure_ascii=False)}")
    return payload


def _query_count(db: Database, table: str, repo_id: str, column: str = "repo_id") -> int:
    row = db.conn.execute(
        f"SELECT COUNT(*) AS count FROM {table} WHERE {column} = ?",
        (repo_id,),
    ).fetchone()
    return int(row["count"]) if row else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="CI smoke checks for easy-query-mcp")
    parser.add_argument("--config", required=True, help="Path to config/sources.yaml")
    parser.add_argument("--db", required=True, help="Path to sqlite index database")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    db_path = Path(args.db).resolve()

    if not config_path.exists():
        raise SystemExit(f"Config file not found: {config_path}")
    if not db_path.exists():
        raise SystemExit(f"Database file not found: {db_path}")

    registry = load_source_registry(str(config_path))
    expected_sources = {"easyquery", "easyquery-docs", "knowledge-easyquery"}
    actual_sources = {source.id for source in registry.sources}
    missing_sources = sorted(expected_sources - actual_sources)
    if missing_sources:
        raise SystemExit(f"Missing expected sources in registry: {missing_sources}")

    db = Database(str(db_path))
    db.connect()
    try:
        db.initialize()

        for source_id in sorted(expected_sources):
            source_row = db.get_source(source_id)
            if source_row is None:
                raise AssertionError(f"Source not registered in database: {source_id}")
            if not source_row.get("path"):
                raise AssertionError(f"Source path missing in database row: {source_id}")

        if _query_count(db, "files", "easyquery") <= 0:
            raise AssertionError("No indexed source files found for easyquery")
        if _query_count(db, "docs", "easyquery-docs") <= 0:
            raise AssertionError("No indexed docs found for easyquery-docs")
        if _query_count(db, "knowledge_cards", "knowledge-easyquery", column="source_id") <= 0:
            raise AssertionError("No indexed knowledge cards found for knowledge-easyquery")

        symbol_payload = _assert_ok(
            "search_symbol",
            _parse_payload(search_symbol(db, query="EasyQuery", source_id="easyquery", limit=5)),
        )
        if symbol_payload.get("count", 0) <= 0:
            raise AssertionError("search_symbol returned no symbol hits")

        docs_payload = _assert_ok(
            "search_docs",
            _parse_payload(search_docs(db, query="transaction", source_id="easyquery-docs", limit=5)),
        )
        docs_results = docs_payload.get("results") or []
        if not docs_results:
            raise AssertionError("search_docs returned no documentation hits")

        first_doc_path = str(docs_results[0].get("path", "")).strip()
        if not first_doc_path:
            raise AssertionError("search_docs returned a result without a path")

        read_doc_payload = _assert_ok(
            "read_doc",
            _parse_payload(read_doc(db, repo_id="easyquery-docs", path=first_doc_path, source_id="easyquery-docs")),
        )
        if not str(read_doc_payload.get("content", "")).strip():
            raise AssertionError("read_doc returned empty content")

        examples_payload = _assert_ok(
            "get_examples",
            _parse_payload(get_examples(db, query="query", source_id="easyquery", limit=5)),
        )
        if examples_payload.get("count", 0) <= 0:
            raise AssertionError("get_examples returned no example hits")

        concept_payload = _assert_ok(
            "search_by_concept",
            _parse_payload(search_by_concept(db, concept="how to use transaction", source_id="easyquery", limit=5)),
        )
        concept_has_hits = any(
            concept_payload.get(bucket)
            for bucket in ("knowledge_cards", "docs", "examples", "symbols", "files")
        )
        if not concept_has_hits:
            raise AssertionError("search_by_concept returned no hits in any bucket")

        knowledge_payload = _assert_ok(
            "search_knowledge_cards",
            _parse_payload(search_knowledge_cards(db, query="transaction", source_id="knowledge-easyquery", limit=5)),
        )
        slugs = {item.get("slug", "") for item in knowledge_payload.get("results") or []}
        if "easyquery-transaction" not in slugs:
            raise AssertionError("Expected easyquery-transaction knowledge card to be returned")

        print(
            json.dumps(
                {
                    "status": "ok",
                    "checked_sources": sorted(expected_sources),
                    "doc_path": first_doc_path,
                    "symbol_hits": symbol_payload.get("count", 0),
                    "doc_hits": docs_payload.get("count", 0),
                    "example_hits": examples_payload.get("count", 0),
                    "knowledge_hits": knowledge_payload.get("count", 0),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
