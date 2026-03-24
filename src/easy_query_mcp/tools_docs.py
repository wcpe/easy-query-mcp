"""MCP tools for documentation and wiki search/read flows."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from .database import Database
from .source_scope import merge_source_results, resolve_source_scope
from .pagination import clamp_limit, clamp_offset, paginate_list


def search_docs(db: Database, query: str, repo_id: Optional[str] = None,
                source_id: Optional[str] = None,
                limit: int = 20,
                offset: int = 0) -> str:
    """Search documentation sources only."""
    resolved_source = source_id or repo_id
    scope = resolve_source_scope(db, resolved_source)
    doc_source_ids = scope["docs_ids"]
    if resolved_source and not doc_source_ids and db.get_source(resolved_source):
        source = db.get_source(resolved_source) or {}
        if source.get("source_type") == "docs":
            doc_source_ids = [resolved_source]
    safe_limit = clamp_limit(limit, default=20)
    safe_offset = clamp_offset(offset)
    results = merge_source_results(
        doc_source_ids,
        lambda sid, row_limit: db.search_docs(query, repo_id=sid, limit=row_limit, source_type="docs"),
        lambda row: (row.get("repo_id", ""), row.get("path", "")),
        safe_limit + safe_offset,
    )
    if not results and resolved_source and db.get_source(resolved_source):
        source = db.get_source(resolved_source) or {}
        if source.get("source_type") == "docs":
            results = db.search_docs(query, repo_id=resolved_source, limit=safe_limit + safe_offset, source_type="docs")

    if not results:
        return json.dumps({
            "status": "no_results",
            "message": f"No documentation found matching '{query}'.",
            "requested_source_id": resolved_source,
            "resolved_doc_source_ids": doc_source_ids,
            "suggestions": [
                "Try broader keywords",
                "Check if docs sources have been indexed",
                "Try search_wiki or search_by_concept",
            ],
        }, ensure_ascii=False, indent=2)

    paged, meta = paginate_list(results, safe_limit, safe_offset, default_limit=20)
    return json.dumps({
        "status": "ok",
        "count": meta["returned"],
        "pagination": meta,
        "results": paged,
    }, ensure_ascii=False, indent=2)


def search_wiki(db: Database, query: str, repo_id: Optional[str] = None,
                source_id: Optional[str] = None,
                limit: int = 20,
                offset: int = 0) -> str:
    """Search wiki sources only."""
    resolved_source = source_id or repo_id
    scope = resolve_source_scope(db, resolved_source)
    wiki_source_ids = scope["wiki_ids"]
    if resolved_source and not wiki_source_ids and db.get_source(resolved_source):
        source = db.get_source(resolved_source) or {}
        if source.get("source_type") == "wiki":
            wiki_source_ids = [resolved_source]
    safe_limit = clamp_limit(limit, default=20)
    safe_offset = clamp_offset(offset)
    results = merge_source_results(
        wiki_source_ids,
        lambda sid, row_limit: db.search_docs(query, repo_id=sid, limit=row_limit, source_type="wiki"),
        lambda row: (row.get("repo_id", ""), row.get("path", "")),
        safe_limit + safe_offset,
    )
    if not results and resolved_source and db.get_source(resolved_source):
        source = db.get_source(resolved_source) or {}
        if source.get("source_type") == "wiki":
            results = db.search_docs(query, repo_id=resolved_source, limit=safe_limit + safe_offset, source_type="wiki")

    if not results:
        return json.dumps({
            "status": "no_results",
            "message": f"No wiki pages found matching '{query}'.",
            "requested_source_id": resolved_source,
            "resolved_wiki_source_ids": wiki_source_ids,
            "suggestions": [
                "Try broader keywords",
                "Check if wiki sources have been cloned and indexed",
                "Try search_docs or search_by_concept",
            ],
        }, ensure_ascii=False, indent=2)
    paged, meta = paginate_list(results, safe_limit, safe_offset, default_limit=20)
    return json.dumps({
        "status": "ok",
        "count": meta["returned"],
        "pagination": meta,
        "results": paged,
    }, ensure_ascii=False, indent=2)


def read_doc(db: Database, repo_id: str, path: str,
             start_line: Optional[int] = None,
             end_line: Optional[int] = None,
             source_id: Optional[str] = None) -> str:
    """Read a document or wiki page from a specific source."""
    resolved_source = source_id or repo_id
    scope = resolve_source_scope(db, resolved_source)
    normalized = Path(path).as_posix()
    if ".." in normalized.split("/"):
        return json.dumps({
            "status": "error",
            "message": "Path traversal detected. Access denied.",
        }, ensure_ascii=False, indent=2)

    candidate_sources = []
    for source_ids in (scope["docs_ids"] or [], scope["wiki_ids"] or [], [resolved_source]):
        for item in source_ids:
            if item not in candidate_sources:
                candidate_sources.append(item)

    doc = None
    actual_source = resolved_source
    for candidate in candidate_sources:
        doc = db.get_doc_content(candidate, normalized)
        if doc is not None:
            actual_source = candidate
            break
    if doc is None:
        for candidate in candidate_sources:
            source_path = db.get_source_path(candidate)
            if source_path is None:
                continue

            full_path = Path(source_path) / normalized
            resolved = full_path.resolve()
            root = Path(source_path).resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                return json.dumps({
                    "status": "error",
                    "message": "Path traversal detected. Access denied.",
                }, ensure_ascii=False, indent=2)
            if not resolved.exists():
                continue
            try:
                content = resolved.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                return json.dumps({
                    "status": "error",
                    "message": f"Cannot read document: {exc}",
                }, ensure_ascii=False, indent=2)
            title = _extract_title(content, normalized)
            actual_source = candidate
            doc = {
                "content": content,
                "title": title,
                "tags": "",
                "repo_id": candidate,
                "path": normalized,
                "summary": "",
                "headings": "",
                "links": "",
                "source_type": (db.get_source(candidate) or {}).get("source_type", "docs"),
            }
            break

    if doc is None:
        return json.dumps({
            "status": "error",
            "message": f"Document not found: {normalized} in source '{resolved_source}'.",
        }, ensure_ascii=False, indent=2)

    content = doc["content"]
    lines = content.split("\n")
    total = len(lines)
    s = max(1, start_line or 1)
    e = min(total, end_line or total)
    numbered = [f"{i}: {line}" for i, line in enumerate(lines[s - 1:e], start=s)]
    return json.dumps({
        "status": "ok",
        "source_id": actual_source,
        "requested_source_id": resolved_source,
        "path": normalized,
        "title": doc.get("title", ""),
        "tags": doc.get("tags", ""),
        "summary": doc.get("summary", ""),
        "headings": doc.get("headings", ""),
        "links": doc.get("links", ""),
        "source_type": doc.get("source_type", ""),
        "total_lines": total,
        "range": {"start": s, "end": e},
        "content": "\n".join(numbered),
    }, ensure_ascii=False, indent=2)


def get_doc_summary(db: Database, symbol: str,
                    source_id: Optional[str] = None) -> str:
    """Get a summary of a documentation or wiki page."""
    scope = resolve_source_scope(db, source_id)
    doc = None
    normalized = Path(symbol).as_posix() if symbol else symbol
    try:
        doc = db.get_doc_by_id(int(symbol))
    except (ValueError, TypeError):
        pass

    if doc is None and normalized and "/" in normalized:
        for repo in scope["docs_ids"] or []:
            doc = db.get_doc_by_path(normalized, repo_id=repo, source_type="docs")
            if doc is not None:
                break
        if doc is None:
            for repo in scope["wiki_ids"] or []:
                doc = db.get_doc_by_path(normalized, repo_id=repo, source_type="wiki")
                if doc is not None:
                    break

    if doc is None:
        doc_results = merge_source_results(
            scope["docs_ids"],
            lambda sid, row_limit: db.search_docs(symbol, repo_id=sid, limit=row_limit, source_type="docs"),
            lambda row: (row.get("repo_id", ""), row.get("path", "")),
            1,
        )
        if doc_results:
            doc = db.get_doc_content(doc_results[0]["repo_id"], doc_results[0]["path"])
        if doc is None:
            wiki_results = merge_source_results(
                scope["wiki_ids"],
                lambda sid, row_limit: db.search_docs(symbol, repo_id=sid, limit=row_limit, source_type="wiki"),
                lambda row: (row.get("repo_id", ""), row.get("path", "")),
                1,
            )
            if wiki_results:
                doc = db.get_doc_content(wiki_results[0]["repo_id"], wiki_results[0]["path"])

    if doc is None:
        return json.dumps({
            "status": "not_found",
            "message": f"No documentation found for '{symbol}'.",
            "suggestions": [
                "Use search_docs or search_wiki to find available documents",
                "Check the exact path or title",
            ],
        }, ensure_ascii=False, indent=2)

    tags = [item.strip() for item in str(doc.get("tags", "") or "").split(",") if item.strip()]
    related_symbols = []
    for tag in tags[:3]:
        for symbol_row in db.search_symbols(tag, limit=3):
            related_symbols.append({
                "name": symbol_row["name"],
                "kind": symbol_row["kind"],
                "source_id": symbol_row["repo_id"],
                "path": symbol_row["rel_path"],
            })

    unique = []
    seen = set()
    for item in related_symbols:
        key = (item["name"], item["source_id"], item["path"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    return json.dumps({
        "status": "ok",
        "title": doc.get("title", ""),
        "source_id": doc.get("repo_id", ""),
        "path": doc.get("path", ""),
        "source_type": doc.get("source_type", ""),
        "tags": tags,
        "summary": doc.get("summary", "") or _extract_summary(doc.get("content", "")),
        "headings": str(doc.get("headings", "") or "").split(" | ") if doc.get("headings") else [],
        "links": str(doc.get("links", "") or "").split(" | ") if doc.get("links") else [],
        "related_symbols": unique[:10],
        "next_steps": [
            f"read_doc(repo_id='{doc.get('repo_id', '')}', path='{doc.get('path', '')}')",
            "search_by_concept with keywords from this doc",
        ],
    }, ensure_ascii=False, indent=2)


def _extract_title(content: str, path: str) -> str:
    match = re.search(r"^#\s+(.+)", content, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return Path(path).stem.replace("-", " ").replace("_", " ").title()


def _extract_summary(content: str, max_len: int = 500) -> str:
    lines = content.split("\n")
    summary_lines = []
    in_content = False

    for line in lines:
        stripped = line.strip()
        if not in_content:
            if stripped.startswith("#") or stripped.startswith("---") or not stripped:
                continue
            in_content = True

        if in_content:
            if stripped.startswith("#"):
                break
            summary_lines.append(stripped)
            if len("\n".join(summary_lines)) >= max_len:
                break

    summary = "\n".join(summary_lines).strip()
    return summary[:max_len] + "..." if len(summary) > max_len else summary
