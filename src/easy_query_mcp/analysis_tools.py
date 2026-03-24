"""Higher-level analysis tools for repo maps and call-chain tracing."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from .database import Database
from .source_scope import resolve_source_scope
from .query_guard import (
    clamp_depth,
    trim_list,
    truncation_meta,
    MAX_CALLERS_PER_LAYER,
    MAX_RELATED_FILES,
    MAX_ENTRYPOINTS,
    MAX_MODULES,
    MAX_DOCS,
    MAX_DOMAIN_TERMS,
)

ENTRYPOINT_HINTS = {
    "command": ("command", "Command"),
    "listener": ("listener", "Listener"),
    "packet": ("packet", "Packet"),
    "controller": ("controller", "Controller"),
    "service": ("service", "Service"),
    "repository": ("repository", "Repository"),
}

ENTRYPOINT_TYPES = {"command", "listener", "packet", "controller"}


def summarize_repo_map(
    db: Database,
    repo_id: str,
    source_id: Optional[str] = None,
    include_modules: bool = True,
    include_risks: bool = True,
    include_docs: bool = True,
) -> str:
    scope = resolve_source_scope(db, source_id or repo_id)
    anchor = _choose_anchor_repo(db, repo_id, scope)
    source_row = db.get_source(anchor)
    if not source_row:
        return json.dumps({
            "status": "error",
            "message": f"Repository/source '{repo_id}' not found.",
        }, ensure_ascii=False, indent=2)

    code_ids = scope.get("code_ids") or [anchor]
    docs_ids = scope.get("docs_ids") or []
    wiki_ids = scope.get("wiki_ids") or []
    knowledge_ids = scope.get("knowledge_ids") or []
    conn = db.connect()

    symbols = []
    files = []
    for code_id in code_ids:
        symbols.extend(db.search_symbols("", repo_id=code_id, limit=400) if False else _list_symbols(conn, code_id))
        files.extend(_list_files(conn, code_id))

    modules = _build_modules(files) if include_modules else []
    entrypoints = _extract_entrypoints(symbols)
    docs = []
    if include_docs:
        for doc_id in docs_ids + wiki_ids:
            docs.extend(db.get_docs_for_repo(doc_id)[:20])

    terms = _domain_terms(source_row, files, symbols, docs)
    risks = []
    if include_risks:
        risks = _risk_signals(db, anchor, source_row, docs, entrypoints)

    trimmed_entrypoints, entrypoints_truncated = trim_list(entrypoints, MAX_ENTRYPOINTS)
    trimmed_modules, modules_truncated = trim_list(modules, MAX_MODULES)
    trimmed_terms, terms_truncated = trim_list(terms, MAX_DOMAIN_TERMS)
    trimmed_docs, docs_truncated = trim_list(docs, MAX_DOCS)

    payload = {
        "status": "ok",
        "repo_id": anchor,
        "summary": {
            "name": source_row.get("name", anchor),
            "source_type": source_row.get("source_type", "code"),
            "tags": [item for item in str(source_row.get("tags", "") or "").split(",") if item],
            "path": source_row.get("path", ""),
            "linked_docs": docs_ids,
            "linked_wiki": wiki_ids,
            "linked_knowledge": knowledge_ids,
        },
        "entrypoints": trimmed_entrypoints,
        "modules": trimmed_modules,
        "domain_terms": trimmed_terms,
        "docs": [
            {
                "repo_id": item.get("repo_id", ""),
                "path": item.get("path", ""),
                "title": item.get("title", ""),
                "source_type": item.get("source_type", "docs"),
            }
            for item in docs[:10]
        ],
        "risk_signals": risks[:10],
        "truncation": truncation_meta(
            truncated=entrypoints_truncated or modules_truncated or terms_truncated or docs_truncated,
            reason="repo map sections trimmed to platform safety limits" if (entrypoints_truncated or modules_truncated or terms_truncated or docs_truncated) else "",
            limits={
                "entrypoints": MAX_ENTRYPOINTS,
                "modules": MAX_MODULES,
                "domain_terms": MAX_DOMAIN_TERMS,
                "docs": MAX_DOCS,
            },
        ),
        "next_steps": _repo_next_steps(anchor, trimmed_entrypoints, trimmed_terms),
    }

    return json.dumps(payload, ensure_ascii=False, indent=2)


def trace_call_chain(
    db: Database,
    symbol: str,
    repo_id: Optional[str] = None,
    source_id: Optional[str] = None,
    max_depth: int = 3,
    include_snippets: bool = True,
    include_entrypoints: bool = True,
) -> str:
    scope = resolve_source_scope(db, source_id or repo_id)
    code_ids = scope.get("code_ids") or ([repo_id] if repo_id else None)
    definitions = _search_symbol_candidates(db, symbol, code_ids)
    if not definitions:
        return json.dumps({
            "status": "not_found",
            "symbol": symbol,
            "message": f"No symbol definition found for '{symbol}'.",
            "suggestions": [
                "Try the simple method/class name",
                "Use search_symbol to inspect similar symbols",
            ],
        }, ensure_ascii=False, indent=2)

    definition = definitions[0]
    symbol_name = definition.get("name", symbol.split(".")[-1])
    visited = set()
    layers = []
    frontier = [(definition, 0)]
    related_files = {}
    collected_entrypoints = []

    while frontier:
        current, depth = frontier.pop(0)
        key = (current.get("repo_id", ""), current.get("rel_path", ""), current.get("line_number", 0), current.get("name", symbol_name))
        if key in visited or depth > max_depth:
            continue
        visited.add(key)

        usage_rows = db.find_usages(current.get("name", symbol_name), repo_id=current.get("repo_id"))
        callers = []
        for row in usage_rows:
            caller = {
                "repo_id": row.get("repo_id", ""),
                "path": row.get("rel_path", ""),
                "language": row.get("language", ""),
                "matches": row.get("matches", [])[:3] if include_snippets else [],
                "entrypoint_type": _entrypoint_type_from_path(row.get("rel_path", "")),
            }
            callers.append(caller)
            related_files[(caller["repo_id"], caller["path"])] = {
                "repo_id": caller["repo_id"],
                "path": caller["path"],
                "language": caller["language"],
            }
            if include_entrypoints and caller["entrypoint_type"] in ENTRYPOINT_TYPES:
                collected_entrypoints.append(caller)
            if depth + 1 <= max_depth:
                next_symbol = _guess_symbol_from_path(db, row.get("repo_id", ""), row.get("rel_path", ""))
                if next_symbol:
                    frontier.append((next_symbol, depth + 1))

        layers.append({
            "depth": depth,
            "symbol": current.get("qualified_name") or current.get("name", symbol_name),
            "definition": {
                "repo_id": current.get("repo_id", ""),
                "path": current.get("rel_path", ""),
                "line": current.get("line_number", 0),
                "end_line": current.get("end_line", 0),
                "kind": current.get("kind", ""),
                "signature": current.get("signature", ""),
            },
            "callers": callers[:12],
        })
        related_files[(current.get("repo_id", ""), current.get("rel_path", ""))] = {
            "repo_id": current.get("repo_id", ""),
            "path": current.get("rel_path", ""),
            "language": "",
        }

    trimmed_entrypoints, entrypoints_truncated = trim_list(_dedup_entrypoints(collected_entrypoints), MAX_ENTRYPOINTS)
    trimmed_related_files, related_files_truncated = trim_list(list(related_files.values()), MAX_RELATED_FILES)

    payload = {
        "status": "ok",
        "symbol": symbol,
        "definition": {
            "repo_id": definition.get("repo_id", ""),
            "path": definition.get("rel_path", ""),
            "line": definition.get("line_number", 0),
            "end_line": definition.get("end_line", 0),
            "kind": definition.get("kind", ""),
            "signature": definition.get("signature", ""),
            "qualified_name": definition.get("qualified_name", ""),
        },
        "entrypoints": trimmed_entrypoints,
        "call_chain": layers,
        "related_files": trimmed_related_files,
        "truncation": truncation_meta(
            truncated=entrypoints_truncated or related_files_truncated or any(layer.get("truncated") for layer in layers),
            reason="call chain output trimmed to platform safety limits" if (entrypoints_truncated or related_files_truncated or any(layer.get("truncated") for layer in layers)) else "",
            limits={
                "max_depth": max_depth,
                "callers_per_layer": MAX_CALLERS_PER_LAYER,
                "entrypoints": MAX_ENTRYPOINTS,
                "related_files": MAX_RELATED_FILES,
            },
        ),
        "notes": [
            "Call chain is usage-based and may include false positives.",
            "Use read_source on returned files to confirm exact business flow.",
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _choose_anchor_repo(db: Database, repo_id: str, scope: dict) -> str:
    if db.get_source(repo_id):
        row = db.get_source(repo_id) or {}
        if row.get("source_type") == "code":
            return repo_id
    code_ids = scope.get("code_ids") or []
    if code_ids:
        return code_ids[0]
    return repo_id


def _list_symbols(conn, repo_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT s.name, s.qualified_name, s.kind, s.signature, s.line_number, s.end_line, s.repo_id, f.rel_path
        FROM symbols s JOIN files f ON f.id = s.file_id WHERE s.repo_id = ?
        ORDER BY f.rel_path, s.line_number LIMIT 400
        """,
        (repo_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _list_files(conn, repo_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT repo_id, rel_path, language, is_example FROM files WHERE repo_id = ? ORDER BY rel_path LIMIT 500",
        (repo_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _build_modules(files: list[dict]) -> list[dict]:
    buckets: dict[str, list[str]] = defaultdict(list)
    for item in files:
        rel_path = str(item.get("rel_path", ""))
        parts = [p for p in Path(rel_path).parts if p not in {"src", "main", "kotlin", "java", "resources", "test"}]
        if not parts:
            continue
        label = _module_label_from_parts(parts)
        buckets[label].append(rel_path)
    results = []
    for name, evidence in buckets.items():
        results.append({"name": name, "evidence": evidence[:5], "file_count": len(evidence)})
    results.sort(key=lambda item: (-item["file_count"], item["name"]))
    return results


def _module_label_from_parts(parts: list[str]) -> str:
    lowered = [p.lower() for p in parts]
    for hint in ["command", "listener", "service", "repository", "domain", "bridge", "packet", "gui", "menu", "model"]:
        if hint in lowered:
            return hint
    if len(parts) >= 2:
        return parts[-2].lower()
    return parts[0].lower()


def _extract_entrypoints(symbols: list[dict]) -> list[dict]:
    results = []
    for symbol in symbols:
        path = symbol.get("rel_path", "")
        entrypoint_type = _entrypoint_type_from_path(path) or _entrypoint_type_from_name(symbol.get("name", ""))
        if not entrypoint_type:
            continue
        results.append({
            "type": entrypoint_type,
            "name": symbol.get("name", ""),
            "kind": symbol.get("kind", ""),
            "path": path,
            "line": symbol.get("line_number", 0),
            "repo_id": symbol.get("repo_id", ""),
        })
    return _dedup_entrypoints(results)


def _entrypoint_type_from_path(path: str) -> str:
    lower = path.lower()
    for kind, hints in ENTRYPOINT_HINTS.items():
        if any(h.lower() in lower for h in hints):
            return kind
    return ""


def _entrypoint_type_from_name(name: str) -> str:
    for kind, hints in ENTRYPOINT_HINTS.items():
        if any(name.endswith(h) for h in hints if h and h[0].isupper()):
            return kind
    return ""


def _domain_terms(source_row: dict, files: list[dict], symbols: list[dict], docs: list[dict]) -> list[str]:
    counter: Counter[str] = Counter()
    for tag in str(source_row.get("tags", "") or "").split(","):
        if tag:
            counter[tag.strip().lower()] += 4
    for item in files[:150]:
        for token in Path(str(item.get("rel_path", ""))).parts:
            normalized = token.lower().replace(".kt", "").replace(".java", "")
            if len(normalized) >= 4 and normalized not in {"src", "main", "kotlin", "java", "resources", "tests"}:
                counter[normalized] += 1
    for symbol in symbols[:120]:
        name = str(symbol.get("name", ""))
        normalized = name.replace("Service", "").replace("Command", "").replace("Listener", "")
        normalized = normalized.strip().lower()
        if len(normalized) >= 4:
            counter[normalized] += 2
    for doc in docs[:30]:
        for field in (doc.get("title", ""), doc.get("tags", ""), doc.get("summary", "")):
            for token in str(field).replace("/", " ").replace("-", " ").split():
                normalized = token.strip().lower()
                if len(normalized) >= 4:
                    counter[normalized] += 1
    return [term for term, _ in counter.most_common(20)]


def _risk_signals(db: Database, repo_id: str, source_row: dict, docs: list[dict], entrypoints: list[dict]) -> list[str]:
    from .anti_pattern_miner import AntiPatternMiner

    signals = []
    if not docs:
        signals.append("No linked docs/wiki indexed for this repository")
    if not entrypoints:
        signals.append("No clear command/listener/packet/controller entrypoints detected")
    try:
        findings = AntiPatternMiner(db).scan(repo_id=repo_id, persist=False).get("findings", [])
    except Exception:
        findings = []
        signals.append("Architecture risk scan could not run in the current environment")
    if findings:
        signals.append(f"Architecture risk scan reported {len(findings)} findings")
    last_indexed = source_row.get("last_indexed_at", "")
    if not last_indexed:
        signals.append("Repository has no last_indexed_at metadata")
    return signals


def _repo_next_steps(repo_id: str, entrypoints: list[dict], terms: list[str]) -> list[str]:
    steps = []
    if entrypoints:
        first = entrypoints[0]
        steps.append(f"trace_call_chain(symbol='{first['name']}', repo_id='{repo_id}')")
    if terms:
        steps.append(f"search_by_concept(concept='{terms[0]} flow', repo_id='{repo_id}')")
    steps.append(f"validate_sources(source_id='{repo_id}')")
    return steps


def _search_symbol_candidates(db: Database, symbol: str, code_ids: Optional[list[str]]) -> list[dict]:
    search_terms = [symbol]
    if "." in symbol:
        search_terms.append(symbol.split(".")[-1])
    results = []
    seen = set()
    for term in search_terms:
        if code_ids:
            for repo_id in code_ids:
                for row in db.search_symbols(term, repo_id=repo_id, limit=20):
                    key = (row.get("repo_id"), row.get("rel_path"), row.get("line_number"), row.get("qualified_name"))
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append(row)
        else:
            for row in db.search_symbols(term, limit=20):
                key = (row.get("repo_id"), row.get("rel_path"), row.get("line_number"), row.get("qualified_name"))
                if key in seen:
                    continue
                    
                seen.add(key)
                results.append(row)
    exact = [row for row in results if row.get("qualified_name") == symbol or row.get("name") == symbol.split(".")[-1]]
    return exact or results


def _guess_symbol_from_path(db: Database, repo_id: str, path: str) -> Optional[dict]:
    candidates = db.get_symbol_by_name(Path(path).stem)
    for row in candidates:
        if row.get("repo_id") == repo_id and row.get("rel_path") == path:
            return row
    return candidates[0] if candidates else None


def _dedup_entrypoints(items: list[dict]) -> list[dict]:
    seen = set()
    results = []
    for item in items:
        key = (item.get("repo_id", ""), item.get("path", ""), item.get("name", ""), item.get("type", item.get("entrypoint_type", "")))
        if key in seen:
            continue
        seen.add(key)
        result = dict(item)
        if "entrypoint_type" in result and "type" not in result:
            result["type"] = result.pop("entrypoint_type")
        results.append(result)
    return results
