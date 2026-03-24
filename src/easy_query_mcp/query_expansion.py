"""Query expansion helpers for bilingual search and recommendation."""

from __future__ import annotations

import os
import re
import sys
from functools import lru_cache
from pathlib import Path

import yaml

from .config import _find_project_root


_DEFAULT_ALIAS_GROUPS: dict[str, list[str]] = {
    "lifecycle": ["生命周期", "启动阶段", "加载顺序", "awake", "onenable", "ondisable"],
    "thread": ["线程", "主线程", "异步", "async", "main thread", "thread safety"],
    "database": ["数据库", "db", "sql", "事务", "transaction", "orm"],
    "pagination": ["分页", "page", "pager", "page result", "toPageResult"],
    "dependency injection": ["依赖注入", "ioc", "inject", "bean", "组件装配"],
    "plugin load": ["插件加载", "启动失败", "enable fail", "plugin load", "plugin enable"],
    "root cause": ["根因", "定位", "root cause", "栈帧", "stack trace"],
    "fix pattern": ["修复模式", "补丁模式", "fix", "bugfix", "回归修复"],
    "null pointer": ["空指针", "npe", "nullpointerexception", "kotlinnullpointerexception"],
    "class not found": ["类缺失", "noclassdeffounderror", "classnotfoundexception"],
    "repair path": ["修复路径", "repair path", "recommended tests", "patch hotspots"],
    "anti pattern": ["反模式", "违规模式", "架构风险", "bad smell", "risk pattern"],
    "staleness": ["过期", "失效", "陈旧", "stale", "freshness"],
    "evaluation": ["评估", "回归", "benchmark", "eval", "测试基线"],
    "telemetry": ["遥测", "反馈", "命中率", "no match", "usage metrics"],
}

_TOKEN_RE = re.compile(r"[\w.\-]+", re.UNICODE)


def _config_path() -> str:
    override = os.environ.get("EASY_QUERY_MCP_QUERY_ALIASES", "").strip()
    if override:
        return str(Path(override).expanduser().resolve())
    return str((_find_project_root() / "config" / "query_aliases.yaml").resolve())


@lru_cache(maxsize=1)
def load_alias_groups() -> dict[str, set[str]]:
    """Load canonical alias groups from YAML with a safe built-in fallback."""
    path = Path(_config_path())
    raw_groups = _DEFAULT_ALIAS_GROUPS
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            raw_groups = raw.get("aliases", raw_groups)
        except Exception as exc:
            print(f"[WARN] Failed to load query aliases from {path}: {exc}", file=sys.stderr)

    groups: dict[str, set[str]] = {}
    for canonical, aliases in raw_groups.items():
        normalized = _normalize(canonical)
        variants = {_normalize(canonical)}
        variants.update(_normalize(alias) for alias in aliases if alias)
        groups[normalized] = {v for v in variants if v}
    return groups


@lru_cache(maxsize=1)
def build_alias_lookup() -> dict[str, set[str]]:
    """Create reverse lookup so any alias can expand to its full group."""
    lookup: dict[str, set[str]] = {}
    for canonical, aliases in load_alias_groups().items():
        group = set(aliases)
        group.add(canonical)
        for alias in group:
            lookup.setdefault(alias, set()).update(group)
    return lookup


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def extract_terms(text: str, limit: int = 12) -> list[str]:
    """Extract query terms while preserving the original phrase."""
    normalized = _normalize(text)
    if not normalized:
        return []

    terms: list[str] = [normalized]
    for token in _TOKEN_RE.findall(normalized):
        token = token.strip(".-")
        if len(token) >= 2:
            terms.append(token)

    deduped: list[str] = []
    seen = set()
    for term in terms:
        if term and term not in seen:
            seen.add(term)
            deduped.append(term)
        if len(deduped) >= limit:
            break
    return deduped


def expand_query_terms(text: str, limit: int = 16) -> list[str]:
    """Expand English/Chinese aliases into a deduplicated term list."""
    base_terms = extract_terms(text, limit=limit)
    if not base_terms:
        return []

    lookup = build_alias_lookup()
    canonical_groups = load_alias_groups()
    expanded: list[str] = []
    seen = set()
    haystack = _normalize(text)

    def add_term(term: str) -> None:
        if term and term not in seen and len(expanded) < limit:
            seen.add(term)
            expanded.append(term)

    # Prioritize canonical groups that are directly implied by the raw query.
    matched_canonicals: list[tuple[str, set[str]]] = []
    for canonical, aliases in canonical_groups.items():
        if canonical in haystack or any(alias in haystack for alias in aliases):
            matched_canonicals.append((canonical, aliases))

    for canonical, _aliases in matched_canonicals:
        add_term(canonical)
        if len(expanded) >= limit:
            return expanded[:limit]

    for _canonical, aliases in matched_canonicals:
        for candidate in sorted(aliases):
            add_term(candidate)
        if len(expanded) >= limit:
            return expanded[:limit]

    for term in base_terms:
        for candidate in lookup.get(term, {term}):
            add_term(candidate)
        for alias, group in lookup.items():
            if alias in haystack:
                for candidate in group:
                    add_term(candidate)
        if len(expanded) >= limit:
            break

    return expanded[:limit]


def build_fts_or_query(text: str) -> str:
    """Build an OR-based FTS query that tolerates bilingual aliases."""
    terms = expand_query_terms(text)
    if not terms:
        return "\"\""
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms[:8])


def score_text_match(text: str, terms: list[str]) -> float:
    """Simple lexical score used to rerank results after SQL retrieval."""
    normalized = _normalize(text)
    if not normalized:
        return 0.0

    score = 0.0
    for term in terms:
        if term == normalized:
            score += 4.0
        elif term in normalized:
            score += 1.5 + min(len(term) / 10.0, 1.5)
    return score
