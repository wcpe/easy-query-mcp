"""Source validation and index health audit helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .config import load_source_registry
from .database import Database


SUPPORTED_DOC_EXTS = {".md", ".mdx", ".adoc", ".txt"}
SUPPORTED_CODE_EXTS = {".kt", ".kts", ".java"}
SUPPORTED_KNOWLEDGE_EXTS = {".md", ".mdx", ".yml", ".yaml"}


def validate_sources(
    db: Database,
    config_path: Optional[str] = None,
    source_id: Optional[str] = None,
    include_disabled: bool = False,
    check_index_state: bool = True,
    check_linked_sources: bool = True,
    check_paths: bool = True,
) -> str:
    registry = load_source_registry(config_path)
    if db.db_path != registry.db_path:
        # registry may still be useful for config resolution; DB remains caller-controlled
        pass

    db.initialize()
    configured = registry.sources
    if source_id:
        configured = [item for item in configured if item.id == source_id]

    if not include_disabled:
        configured = [item for item in configured if item.enabled]

    results: list[dict] = []
    summary = {"checked": 0, "passed": 0, "warning": 0, "failed": 0}
    known_ids = {item.id for item in registry.sources}
    base_dir = Path(registry.config_path).resolve().parent if registry.config_path else None

    for source in configured:
        source_row = db.get_source(source.id) or {}
        checks: list[dict] = []
        suggestions: list[str] = []
        severity = "ok"
        resolved_path = source.resolved_path(base_dir)

        checks.append({
            "name": "valid_type",
            "passed": source.type in {"code", "docs", "wiki", "knowledge"},
            "message": "source type is supported" if source.type in {"code", "docs", "wiki", "knowledge"} else f"Unsupported source type: {source.type}",
        })

        if check_paths:
            exists = resolved_path.exists()
            is_dir = resolved_path.is_dir() if exists else False
            checks.append({
                "name": "path_exists",
                "passed": exists,
                "message": str(resolved_path) if exists else f"Source path does not exist: {resolved_path}",
            })
            checks.append({
                "name": "path_is_directory",
                "passed": is_dir,
                "message": "Path is a directory" if is_dir else f"Source path is not a directory: {resolved_path}",
            })
            if not exists:
                suggestions.append(f"Check config/sources.yaml path for '{source.id}'")
            elif not is_dir:
                suggestions.append(f"Update '{source.id}' to point to a directory path")

        if check_linked_sources and source.linked_source_id:
            linked_exists = source.linked_source_id in known_ids
            linked_type_ok = True
            if linked_exists:
                linked = registry.get(source.linked_source_id)
                linked_type_ok = bool(linked and linked.type == "code")
            checks.append({
                "name": "linked_source_exists",
                "passed": linked_exists,
                "message": "linked_source_id resolved" if linked_exists else f"linked_source_id '{source.linked_source_id}' not found",
            })
            checks.append({
                "name": "linked_source_type",
                "passed": linked_type_ok,
                "message": "linked source is code" if linked_type_ok else "linked source should point to a code source",
            })
            if not linked_exists:
                suggestions.append(f"Fix linked_source_id for '{source.id}' or create the missing source")
            elif not linked_type_ok:
                suggestions.append(f"Point '{source.id}' linked_source_id to a code source")

        if source.git_url:
            checks.append({
                "name": "git_sync_recorded",
                "passed": bool(source_row.get("last_synced_at") or source_row.get("last_synced_rev")),
                "message": "git source has sync metadata" if (source_row.get("last_synced_at") or source_row.get("last_synced_rev")) else "git-backed source has no sync history recorded",
            })
            if not (source_row.get("last_synced_at") or source_row.get("last_synced_rev")):
                suggestions.append(f"Run sync_source or sync_sources for '{source.id}'")

        if check_index_state:
            checks.append({
                "name": "indexed_once",
                "passed": bool(source_row.get("last_indexed_at") or source_row.get("last_indexed_rev")),
                "message": "source has index metadata" if (source_row.get("last_indexed_at") or source_row.get("last_indexed_rev")) else "source has not been indexed yet",
            })
            if not (source_row.get("last_indexed_at") or source_row.get("last_indexed_rev")):
                suggestions.append(f"Run easy-query-mcp-index --source-id {source.id}")

        coverage = _source_coverage(db, source.id, source.type)
        checks.extend(_coverage_checks(source.id, source.type, coverage, resolved_path))
        if coverage.get("warning"):
            suggestions.extend(coverage["warning"])

        failed = any(not item["passed"] and item["name"] in {"valid_type", "path_exists", "path_is_directory", "linked_source_exists", "linked_source_type"} for item in checks)
        warned = any(not item["passed"] for item in checks) and not failed
        if failed:
            severity = "failed"
            summary["failed"] += 1
        elif warned:
            severity = "warning"
            summary["warning"] += 1
        else:
            summary["passed"] += 1
        summary["checked"] += 1

        dedup_suggestions = []
        seen = set()
        for item in suggestions:
            if item not in seen:
                seen.add(item)
                dedup_suggestions.append(item)

        results.append({
            "source_id": source.id,
            "source_type": source.type,
            "enabled": source.enabled,
            "severity": severity,
            "path": str(resolved_path),
            "linked_source_id": source.linked_source_id,
            "last_synced_at": source_row.get("last_synced_at", ""),
            "last_indexed_at": source_row.get("last_indexed_at", ""),
            "coverage": coverage,
            "checks": checks,
            "suggestions": dedup_suggestions,
        })

    status = "ok"
    if summary["failed"]:
        status = "warning"
    elif summary["warning"]:
        status = "ok"

    return json.dumps({
        "status": status,
        "summary": summary,
        "results": results,
    }, ensure_ascii=False, indent=2)


def _source_coverage(db: Database, source_id: str, source_type: str) -> dict:
    conn = db.connect()
    warning: list[str] = []
    if source_type == "code":
        file_count = conn.execute("SELECT COUNT(*) AS c FROM files WHERE repo_id = ?", (source_id,)).fetchone()["c"]
        symbol_count = conn.execute("SELECT COUNT(*) AS c FROM symbols WHERE repo_id = ?", (source_id,)).fetchone()["c"]
        example_count = conn.execute("SELECT COUNT(*) AS c FROM examples WHERE repo_id = ?", (source_id,)).fetchone()["c"]
        if file_count and not symbol_count:
            warning.append(f"No symbols extracted for '{source_id}'")
        return {
            "files": file_count,
            "symbols": symbol_count,
            "examples": example_count,
            "warning": warning,
        }
    if source_type in {"docs", "wiki"}:
        doc_count = conn.execute("SELECT COUNT(*) AS c FROM docs WHERE repo_id = ?", (source_id,)).fetchone()["c"]
        if not doc_count:
            warning.append(f"No indexed documents found for '{source_id}'")
        return {
            "docs": doc_count,
            "warning": warning,
        }
    if source_type == "knowledge":
        card_count = conn.execute("SELECT COUNT(*) AS c FROM knowledge_cards WHERE source_id = ?", (source_id,)).fetchone()["c"]
        if not card_count:
            warning.append(f"No knowledge cards indexed for '{source_id}'")
        return {
            "cards": card_count,
            "warning": warning,
        }
    return {"warning": warning}


def get_index_overview(
    db: Database,
    config_path: Optional[str] = None,
    source_id: Optional[str] = None,
    include_disabled: bool = False,
) -> str:
    registry = load_source_registry(config_path)
    db.initialize()
    configured = registry.sources
    if source_id:
        configured = [item for item in configured if item.id == source_id]
    if not include_disabled:
        configured = [item for item in configured if item.enabled]

    results: list[dict] = []
    summary = {"checked": 0, "healthy": 0, "warning": 0, "unindexed": 0}

    for source in configured:
        source_row = db.get_source(source.id) or {}
        coverage = _source_coverage(db, source.id, source.type)
        indexed = bool(source_row.get("last_indexed_at") or source_row.get("last_indexed_rev"))
        issues: list[str] = []
        files = int(coverage.get("files", 0) or 0)
        symbols = int(coverage.get("symbols", 0) or 0)
        docs = int(coverage.get("docs", 0) or 0)
        examples = int(coverage.get("examples", 0) or 0)
        cards = int(coverage.get("cards", 0) or 0)

        if not indexed:
            issues.append("not_indexed")
            summary["unindexed"] += 1
        if source.type == "code" and indexed and files == 0:
            issues.append("no_files")
        if source.type == "code" and files > 0 and symbols == 0:
            issues.append("no_symbols")
        if source.type == "code" and files > 0 and examples == 0:
            issues.append("no_examples")
        if source.type in {"docs", "wiki"} and indexed and docs == 0:
            issues.append("no_docs")
        if source.type == "knowledge" and indexed and cards == 0:
            issues.append("no_cards")

        health = "healthy" if indexed and not issues else ("unindexed" if not indexed else "warning")
        if health == "healthy":
            summary["healthy"] += 1
        elif health == "warning":
            summary["warning"] += 1

        results.append({
            "source_id": source.id,
            "source_type": source.type,
            "enabled": source.enabled,
            "linked_source_id": source.linked_source_id,
            "files": files,
            "symbols": symbols,
            "docs": docs,
            "examples": examples,
            "cards": cards,
            "last_indexed_at": source_row.get("last_indexed_at", ""),
            "last_indexed_rev": source_row.get("last_indexed_rev", ""),
            "health": health,
            "issues": issues,
        })
        summary["checked"] += 1

    return json.dumps({
        "status": "ok",
        "summary": summary,
        "results": results,
    }, ensure_ascii=False, indent=2)


def check_index_health(
    db: Database,
    config_path: Optional[str] = None,
    source_id: Optional[str] = None,
    include_disabled: bool = False,
) -> str:
    registry = load_source_registry(config_path)
    db.initialize()
    configured = registry.sources
    if source_id:
        configured = [item for item in configured if item.id == source_id]
    if not include_disabled:
        configured = [item for item in configured if item.enabled]

    summary = {
        "checked": 0,
        "healthy": 0,
        "warning": 0,
        "unindexed": 0,
    }
    results: list[dict] = []
    conn = db.connect()
    base_dir = Path(registry.config_path).resolve().parent if registry.config_path else None

    for source in configured:
        resolved_path = source.resolved_path(base_dir)
        source_row = db.get_source(source.id) or {}
        coverage = _source_coverage(db, source.id, source.type)
        issues = []

        indexed = bool(source_row.get("last_indexed_at") or source_row.get("last_indexed_rev"))
        if not indexed:
            issues.append("Source has no index metadata")
            summary["unindexed"] += 1

        if source.type == "code":
            files = int(coverage.get("files", 0) or 0)
            symbols = int(coverage.get("symbols", 0) or 0)
            examples = int(coverage.get("examples", 0) or 0)
            if indexed and files == 0:
                issues.append("Indexed code source contains 0 files")
            if files > 0 and symbols == 0:
                issues.append("Code files exist but no symbols were extracted")
            if files > 0 and examples == 0:
                issues.append("No examples detected for this code source")
        elif source.type in {"docs", "wiki"}:
            docs = int(coverage.get("docs", 0) or 0)
            if indexed and docs == 0:
                issues.append("Indexed docs/wiki source contains 0 docs")
        elif source.type == "knowledge":
            cards = int(coverage.get("cards", 0) or 0)
            if indexed and cards == 0:
                issues.append("Indexed knowledge source contains 0 cards")

        orphaned_files = 0
        if source.type == "code":
            orphaned_files = conn.execute(
                "SELECT COUNT(*) AS c FROM files WHERE repo_id = ? AND content = ''",
                (source.id,),
            ).fetchone()["c"]
            if orphaned_files:
                issues.append(f"{orphaned_files} indexed files have empty content")

        supported_files_detected = _supported_file_count(resolved_path, source.type)
        if supported_files_detected > 0 and not indexed:
            issues.append(f"Detected {supported_files_detected} supported files but source has not been indexed")

        severity = "healthy"
        if not indexed:
            severity = "unindexed"
        elif issues:
            severity = "warning"

        if severity == "healthy":
            summary["healthy"] += 1
        elif severity == "warning":
            summary["warning"] += 1

        results.append({
            "source_id": source.id,
            "source_type": source.type,
            "severity": severity,
            "path": str(resolved_path),
            "last_indexed_at": source_row.get("last_indexed_at", ""),
            "last_indexed_rev": source_row.get("last_indexed_rev", ""),
            "coverage": coverage,
            "supported_files_detected": supported_files_detected,
            "issues": issues,
            "suggestions": _health_suggestions(source.id, source.type, severity, coverage, issues),
        })
        summary["checked"] += 1

    status = "ok" if summary["warning"] == 0 and summary["unindexed"] == 0 else "warning"
    return json.dumps({
        "status": status,
        "summary": summary,
        "results": results,
    }, ensure_ascii=False, indent=2)


def _supported_file_count(resolved_path: Path, source_type: str) -> int:
    if not resolved_path.exists() or not resolved_path.is_dir():
        return 0
    allowed = {
        "code": SUPPORTED_CODE_EXTS,
        "docs": SUPPORTED_DOC_EXTS,
        "wiki": SUPPORTED_DOC_EXTS,
        "knowledge": SUPPORTED_KNOWLEDGE_EXTS,
    }.get(source_type, set())
    return sum(1 for path in resolved_path.rglob("*") if path.is_file() and path.suffix.lower() in allowed)


def _health_suggestions(source_id: str, source_type: str, severity: str, coverage: dict, issues: list[str]) -> list[str]:
    suggestions = []
    if severity == "unindexed":
        suggestions.append(f"Run easy-query-mcp-index --source-id {source_id}")
    if any("0 files" in item or "0 docs" in item or "0 cards" in item for item in issues):
        suggestions.append(f"Check whether '{source_id}' path contains supported {source_type} files")
    if any("no symbols" in item.lower() for item in issues):
        suggestions.append(f"Inspect parser coverage for '{source_id}' source files")
    if any("empty content" in item.lower() for item in issues):
        suggestions.append(f"Reindex '{source_id}' and inspect unreadable files")
    if not suggestions and severity == "healthy":
        suggestions.append("No action required")
    deduped = []
    seen = set()
    for item in suggestions:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _coverage_checks(source_id: str, source_type: str, coverage: dict, resolved_path: Path) -> list[dict]:
    checks: list[dict] = []
    if source_type == "code":
        files = int(coverage.get("files", 0) or 0)
        symbols = int(coverage.get("symbols", 0) or 0)
        checks.append({
            "name": "indexed_code_files",
            "passed": files > 0,
            "message": f"Indexed {files} code files" if files > 0 else f"No indexed code files for '{source_id}'",
        })
        checks.append({
            "name": "indexed_symbols",
            "passed": symbols > 0 if files > 0 else False,
            "message": f"Indexed {symbols} symbols" if symbols > 0 else f"No symbols indexed for '{source_id}'",
        })
        if resolved_path.exists() and resolved_path.is_dir() and files == 0:
            supported = any(path.suffix.lower() in SUPPORTED_CODE_EXTS for path in resolved_path.rglob("*") if path.is_file())
            if supported:
                coverage.setdefault("warning", []).append(f"Supported code files exist but '{source_id}' is not indexed")
    elif source_type in {"docs", "wiki"}:
        docs = int(coverage.get("docs", 0) or 0)
        checks.append({
            "name": "indexed_docs",
            "passed": docs > 0,
            "message": f"Indexed {docs} documents" if docs > 0 else f"No indexed documents for '{source_id}'",
        })
        if resolved_path.exists() and resolved_path.is_dir() and docs == 0:
            supported = any(path.suffix.lower() in SUPPORTED_DOC_EXTS for path in resolved_path.rglob("*") if path.is_file())
            if supported:
                coverage.setdefault("warning", []).append(f"Supported docs files exist but '{source_id}' is not indexed")
    elif source_type == "knowledge":
        cards = int(coverage.get("cards", 0) or 0)
        checks.append({
            "name": "indexed_cards",
            "passed": cards > 0,
            "message": f"Indexed {cards} cards" if cards > 0 else f"No indexed cards for '{source_id}'",
        })
        if resolved_path.exists() and resolved_path.is_dir() and cards == 0:
            supported = any(path.suffix.lower() in SUPPORTED_KNOWLEDGE_EXTS for path in resolved_path.rglob("*") if path.is_file())
            if supported:
                coverage.setdefault("warning", []).append(f"Knowledge card files exist but '{source_id}' is not indexed")
    return checks
