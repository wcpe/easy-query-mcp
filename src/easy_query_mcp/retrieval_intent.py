"""Helpers for distinguishing issue-oriented retrieval from concept lookup."""

from __future__ import annotations

import re

from .query_expansion import expand_query_terms


_SIGNAL_PATTERNS: list[tuple[str, float, str]] = [
    (r"\bfix(?:ing|ed)?\b", 1.4, "explicit_fix"),
    (r"\bbug\b", 1.2, "bug"),
    (r"\berror\b", 1.1, "error"),
    (r"\bexception\b", 1.3, "exception"),
    (r"\bcrash\b", 1.1, "crash"),
    (r"\bfail(?:ed|ure)?\b", 1.0, "failure"),
    (r"\bnpe\b", 1.2, "npe"),
    (r"\bnull(?:pointer)?\b", 1.2, "null"),
    (r"\bstack\s*trace\b", 1.1, "stack_trace"),
    (r"\bhow to repair\b", 1.1, "repair"),
    (r"\bwhy\b", 0.6, "why"),
    (r"\brepair\b", 1.0, "repair"),
    (r"\brollback\b", 0.8, "rollback"),
    (r"\bregression\b", 0.9, "regression"),
    (r"怎么修", 1.4, "explicit_fix"),
    (r"如何修", 1.4, "explicit_fix"),
    (r"怎么排查", 1.2, "troubleshoot"),
    (r"如何排查", 1.2, "troubleshoot"),
    (r"报错", 1.2, "error"),
    (r"异常", 1.2, "exception"),
    (r"错误", 1.1, "error"),
    (r"修复", 1.4, "explicit_fix"),
    (r"修一下", 1.3, "explicit_fix"),
    (r"空指针", 1.2, "npe"),
    (r"崩溃", 1.1, "crash"),
    (r"失效", 1.0, "failure"),
    (r"故障", 1.0, "failure"),
    (r"回归", 0.9, "regression"),
]

_EXCEPTION_TOKEN_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9]+(?:Exception|Error)|NoClassDefFoundError|ClassNotFoundException|KotlinNullPointerException)\b"
)
_STACK_TRACE_RE = re.compile(r"\bat\s+[\w.$]+\.[\w$<>]+\(")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_TOKEN_RE = re.compile(r"[\w.\-]+", re.UNICODE)

_NORMALIZED_EXCEPTION_TERMS: dict[str, list[str]] = {
    "kotlinnullpointerexception": ["kotlin npe", "null pointer", "startup npe", "空指针"],
    "nullpointerexception": ["npe", "null pointer", "空指针"],
    "noclassdeffounderror": ["class not found", "plugin load", "类缺失"],
    "classnotfoundexception": ["class not found", "plugin load", "类缺失"],
    "sqlsyntaxerrorexception": ["sql syntax", "数据库", "sql 语法"],
    "illegalpluginaccessexception": ["async access", "plugin lifecycle", "异步访问违规"],
    "outofmemoryerror": ["out of memory", "oom", "内存溢出"],
}

_CATEGORY_PRIORITY = {
    "fix-pattern": 0,
    "troubleshooting": 1,
    "anti-pattern": 2,
    "architecture": 3,
    "lifecycle": 4,
}


def contains_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def extract_issue_terms(text: str, limit: int = 16) -> list[str]:
    """Extract issue-focused terms including normalized exception aliases."""
    normalized = (text or "").strip()
    if not normalized:
        return []

    terms: list[str] = []
    seen = set()

    def add(term: str) -> None:
        candidate = (term or "").strip().lower()
        if not candidate or candidate in seen:
            return
        seen.add(candidate)
        terms.append(candidate)

    for term in expand_query_terms(normalized):
        add(term)

    lowered = normalized.lower()
    for token in _TOKEN_RE.findall(lowered):
        if len(token) >= 3:
            add(token)

    for match in _EXCEPTION_TOKEN_RE.findall(normalized):
        compact = match.lower()
        add(compact)
        for alias in _NORMALIZED_EXCEPTION_TERMS.get(compact, []):
            add(alias)

    if _STACK_TRACE_RE.search(normalized):
        add("stack trace")
        add("root cause")

    return terms[:limit]


def analyze_issue_query(text: str) -> dict:
    """Return structured intent signals for retrieval, telemetry, and prompts."""
    lowered = (text or "").lower()
    score = 0.0
    matched_signals: list[str] = []

    for pattern, weight, label in _SIGNAL_PATTERNS:
        if re.search(pattern, lowered, re.IGNORECASE):
            score += weight
            matched_signals.append(label)

    if _STACK_TRACE_RE.search(text or ""):
        score += 1.3
        matched_signals.append("stack_trace")

    normalized_terms = extract_issue_terms(text)
    if any(term in normalized_terms for term in ("kotlin npe", "null pointer", "class not found", "oom")):
        score += 0.8
        matched_signals.append("normalized_exception")

    preferred_categories = ["fix-pattern", "troubleshooting", "anti-pattern"]
    if "architecture" in lowered or "drift" in lowered or "边界" in lowered:
        preferred_categories.append("architecture")

    issue_oriented = score >= 1.0 or bool(_EXCEPTION_TOKEN_RE.search(text or ""))
    mode = "repair_path" if issue_oriented and "stack_trace" in matched_signals else "fix_first" if issue_oriented else "default"

    return {
        "issue_oriented": issue_oriented,
        "issue_score": round(score, 3),
        "matched_signals": list(dict.fromkeys(matched_signals)),
        "normalized_terms": normalized_terms,
        "preferred_categories": preferred_categories,
        "query_mode": mode,
    }


def is_issue_oriented_query(text: str) -> bool:
    return bool(analyze_issue_query(text)["issue_oriented"])


def card_category_priority(category: str) -> int:
    return _CATEGORY_PRIORITY.get((category or "").lower(), 5)


def issue_sort_key(card: dict, intent: dict | None = None) -> tuple:
    """Sort issue-oriented results so fix patterns beat broad conceptual cards."""
    breakdown = card.get("score_breakdown") or {}
    final_score = float(breakdown.get("final_score", 0.0) or 0.0)
    evidence_count = int(card.get("evidence_count", 0) or 0)
    category = str(card.get("category", "") or "").lower()
    category_rank = card_category_priority(category)

    if intent and category in set(intent.get("preferred_categories", [])):
        category_rank -= 1
    if category == "fix-pattern":
        final_score += 0.45
    elif category == "troubleshooting":
        final_score += 0.25

    return (
        category_rank,
        -round(final_score, 4),
        -evidence_count,
        -float(card.get("priority", 0) or 0),
        str(card.get("title", "")),
    )
