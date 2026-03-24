"""MCP Tools - Phase 1: Core search and read tools."""

import json
import re
from pathlib import Path
from typing import Optional

from .database import Database
from .source_scope import merge_source_results, resolve_source_scope
from .pagination import clamp_limit, clamp_offset, paginate_list


def search_symbol(db: Database, query: str, repo_id: Optional[str] = None,
                  source_id: Optional[str] = None,
                  kind: Optional[str] = None, limit: int = 30,
                  offset: int = 0) -> str:
    """Search for symbols (classes, functions, methods, annotations) by name, signature, or qualified name.

    Args:
        query: Search term (name, partial name, or signature fragment)
        repo_id: Optional repo filter (e.g. 'easyquery')
        kind: Optional kind filter (class, interface, function, method, enum, annotation, object)
        limit: Max results (default 30)

    Returns:
        JSON array of matching symbols with file path, line number, signature, repo info.
    """
    resolved_source = source_id or repo_id
    scope = resolve_source_scope(db, resolved_source)
    safe_limit = clamp_limit(limit, default=30)
    safe_offset = clamp_offset(offset)
    results = merge_source_results(
        scope["code_ids"],
        lambda sid, row_limit: db.search_symbols(query, repo_id=sid, kind=kind, limit=row_limit),
        lambda row: (
            row.get("repo_id", ""),
            row.get("qualified_name", ""),
            row.get("line_number", 0),
        ),
        safe_limit + safe_offset,
    )
    if results:
        normalized_query = query.replace("-", "").replace("_", "").replace(" ", "").lower()
        definition_kinds = {"annotation", "class", "interface", "object", "enum"}
        exact_definitions = [
            item for item in results
            if str(item.get("kind", "")).lower() in definition_kinds
            and str(item.get("name", "")).replace("-", "").replace("_", "").replace(" ", "").lower() == normalized_query
        ]
        if exact_definitions:
            others = [item for item in results if item not in exact_definitions]
            results = exact_definitions + others

    if not results:
        return json.dumps({
            "status": "no_results",
            "message": f"No symbols found matching '{query}'.",
            "suggestions": [
                "Try a broader search term",
                "Check if the repo has been indexed",
                "Try searching without kind filter",
            ],
        }, ensure_ascii=False, indent=2)

    paged, meta = paginate_list(results, safe_limit, safe_offset, default_limit=20)
    return json.dumps({
        "status": "ok",
        "count": meta["returned"],
        "pagination": meta,
        "results": paged,
    }, ensure_ascii=False, indent=2)


def read_source(db: Database, repo_id: str, path: str,
                start_line: Optional[int] = None,
                end_line: Optional[int] = None,
                source_id: Optional[str] = None) -> str:
    """Read source code from a specific file in a repository.

    Args:
        repo_id: Repository identifier
        path: Relative file path within the repo
        start_line: Optional start line (1-based)
        end_line: Optional end line (1-based, inclusive)

    Returns:
        Source code content with line numbers, or error message.
    """
    # Path safety: normalize and check for traversal
    normalized = Path(path).as_posix()
    if ".." in normalized.split("/"):
        return json.dumps({
            "status": "error",
            "message": "Path traversal detected. Access denied.",
        }, ensure_ascii=False, indent=2)

    resolved_source = source_id or repo_id
    scope = resolve_source_scope(db, resolved_source)
    candidate_sources = scope["code_ids"] or [resolved_source]

    # Try database first
    content = None
    actual_source = repo_id
    for candidate in candidate_sources:
        content = db.get_file_content(candidate, normalized)
        if content is not None:
            actual_source = candidate
            break

    # Also try with backslash variant
    if content is None:
        for candidate in candidate_sources:
            content = db.get_file_content(candidate, path.replace("\\", "/"))
            if content is not None:
                actual_source = candidate
                break

    if content is None:
        # Try reading from filesystem as fallback
        repo_path = None
        for candidate in candidate_sources:
            repo_path = db.get_repo_path(candidate)
            if repo_path:
                actual_source = candidate
                break
        if repo_path is None:
            return json.dumps({
                "status": "error",
                "message": f"Repository/source '{resolved_source}' not found.",
            }, ensure_ascii=False, indent=2)

        full_path = Path(repo_path) / normalized
        resolved = full_path.resolve()
        repo_resolved = Path(repo_path).resolve()

        # Security check: ensure resolved path is within repo
        try:
            resolved.relative_to(repo_resolved)
        except ValueError:
            return json.dumps({
                "status": "error",
                "message": "Path traversal detected. Access denied.",
            }, ensure_ascii=False, indent=2)

        if not resolved.exists():
            return json.dumps({
                "status": "error",
                "message": f"File not found: {normalized} in source '{resolved_source}'.",
            }, ensure_ascii=False, indent=2)

        try:
            content = resolved.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"Cannot read file: {e}",
            }, ensure_ascii=False, indent=2)

    lines = content.split("\n")
    total = len(lines)

    # Apply line range
    s = max(1, start_line or 1)
    e = min(total, end_line or total)

    selected = lines[s - 1:e]
    numbered = [f"{i}: {line}" for i, line in enumerate(selected, start=s)]

    return json.dumps({
        "status": "ok",
        "repo_id": actual_source,
        "source_id": resolved_source,
        "path": normalized,
        "total_lines": total,
        "range": {"start": s, "end": e},
        "content": "\n".join(numbered),
    }, ensure_ascii=False, indent=2)


def get_examples(db: Database, query: str, repo_id: Optional[str] = None,
                 source_id: Optional[str] = None,
                 limit: int = 20,
                 offset: int = 0) -> str:
    """Search for code examples, demos, tests, and samples.

    Args:
        query: Search term (API name, concept, or keyword)
        repo_id: Optional repo filter
        limit: Max results (default 20)

    Returns:
        JSON array of matching examples with title, file path, repo, and code snippet.
    """
    resolved_source = source_id or repo_id
    scope = resolve_source_scope(db, resolved_source)
    safe_limit = clamp_limit(limit, default=20)
    safe_offset = clamp_offset(offset)
    results = merge_source_results(
        scope["code_ids"],
        lambda sid, row_limit: db.get_examples(query, repo_id=sid, limit=row_limit),
        lambda row: (
            row.get("repo_id", ""),
            row.get("rel_path", ""),
            row.get("title", ""),
        ),
        safe_limit + safe_offset,
    )
    if scope["code_ids"] is not None:
        allowed_ids = set(scope["code_ids"])
        results = [row for row in results if row.get("repo_id") in allowed_ids]
    if not results:
        return json.dumps({
            "status": "no_results",
            "message": f"No examples found matching '{query}'.",
            "suggestions": [
                "Try a broader keyword",
                "Search for the class or function name directly",
                "Check if example/test directories exist in the repo",
            ],
        }, ensure_ascii=False, indent=2)

    paged, meta = paginate_list(results, safe_limit, safe_offset, default_limit=20)
    return json.dumps({
        "status": "ok",
        "count": meta["returned"],
        "pagination": meta,
        "results": paged,
    }, ensure_ascii=False, indent=2)


def get_api_summary(db: Database, symbol: str) -> str:
    """Get a summary of an API symbol including type, location, signature, and suggested next steps.

    Args:
        symbol: Symbol name to look up

    Returns:
        JSON summary with type, location, signature, and recommendations.
    """
    results = db.get_symbol_by_name(symbol)
    if not results:
        return json.dumps({
            "status": "not_found",
            "message": f"Symbol '{symbol}' not found in the index.",
            "suggestions": [
                f"Try search_symbol with a partial name",
                f"Check if the repository containing '{symbol}' has been indexed",
                f"The symbol might be in a dependency not indexed locally",
            ],
        }, ensure_ascii=False, indent=2)

    summaries = []
    for r in results:
        summary = {
            "name": r["name"],
            "kind": r["kind"],
            "signature": r["signature"],
            "qualified_name": r["qualified_name"],
            "location": {
                "repo": r["repo_id"],
                "file": r["rel_path"],
                "line": r["line_number"],
                "end_line": r["end_line"],
            },
            "next_steps": _suggest_next_steps(r),
        }
        summaries.append(summary)

    return json.dumps({
        "status": "ok",
        "count": len(summaries),
        "summaries": summaries,
    }, ensure_ascii=False, indent=2)


def _suggest_next_steps(sym: dict) -> list[str]:
    """Generate suggested next steps based on symbol kind."""
    steps = []
    kind = sym.get("kind", "")
    name = sym.get("name", "")
    repo = sym.get("repo_id", "")
    path = sym.get("rel_path", "")

    steps.append(f"read_source(repo_id='{repo}', path='{path}', "
                 f"start_line={sym.get('line_number', 1)}, "
                 f"end_line={sym.get('end_line', sym.get('line_number', 1) + 30)})")

    if kind in ("class", "interface", "object"):
        steps.append(f"find_usages(symbol='{name}') to see where it's used")
        steps.append(f"search_symbol(query='{name}', kind='method') to find related methods")
    elif kind in ("function", "method"):
        steps.append(f"find_usages(symbol='{name}') to see call sites")
        steps.append(f"get_examples(query='{name}') to find usage examples")
    elif kind == "annotation":
        steps.append(f"find_usages(symbol='{name}') to see where this annotation is applied")

    return steps
