"""Lightweight semantic-style ranking helpers without external vector dependencies."""

from __future__ import annotations

import re
from typing import Iterable


def normalize_terms(text: str) -> list[str]:
    raw = re.findall(r"[A-Za-z0-9_\-\u4e00-\u9fff]+", (text or "").lower())
    normalized: list[str] = []
    for token in raw:
        cleaned = token.strip("_- ")
        if len(cleaned) < 2:
            continue
        normalized.append(cleaned)
        if "-" in cleaned or "_" in cleaned:
            normalized.extend(part for part in re.split(r"[-_]", cleaned) if len(part) >= 2)
        camel_parts = re.sub(r"([a-z])([A-Z])", r"\1 \2", token).lower().split()
        normalized.extend(part for part in camel_parts if len(part) >= 2)
    deduped: list[str] = []
    seen = set()
    for token in normalized:
        if token not in seen:
            seen.add(token)
            deduped.append(token)
    return deduped


def weighted_semantic_score(query_terms: list[str], fields: dict[str, str], field_weights: dict[str, float]) -> tuple[float, dict]:
    if not query_terms:
        return 0.0, {"matched_terms": [], "coverage": 0.0, "field_scores": {}}

    field_scores: dict[str, float] = {}
    matched_terms = set()
    weighted_total = 0.0

    for field, text in fields.items():
        terms = set(normalize_terms(text))
        if not terms:
            field_scores[field] = 0.0
            continue
        overlap = {term for term in query_terms if term in terms or any(term in candidate or candidate in term for candidate in terms)}
        matched_terms.update(overlap)
        coverage = len(overlap) / max(1, len(query_terms))
        jaccard = len(overlap) / max(1, len(terms | set(query_terms)))
        prefix_hits = sum(1 for term in query_terms if any(candidate.startswith(term[:3]) for candidate in terms if len(term) >= 3))
        prefix_score = prefix_hits / max(1, len(query_terms))
        score = round((coverage * 0.65) + (jaccard * 0.2) + (prefix_score * 0.15), 4)
        field_scores[field] = score
        weighted_total += score * field_weights.get(field, 1.0)

    final_score = round(weighted_total / max(1.0, sum(field_weights.values())), 4)
    return final_score, {
        "matched_terms": sorted(matched_terms),
        "coverage": round(len(matched_terms) / max(1, len(query_terms)), 4),
        "field_scores": field_scores,
    }


def rerank_semantic_candidates(items: Iterable[dict], query: str, field_map: dict[str, str], field_weights: dict[str, float], limit: int) -> list[dict]:
    query_terms = normalize_terms(query)
    ranked = []
    for item in items:
        fields = {target: str(item.get(source, "") or "") for source, target in field_map.items()}
        score, details = weighted_semantic_score(query_terms, fields, field_weights)
        enriched = dict(item)
        enriched["semantic_score"] = score
        enriched["semantic_match"] = details
        ranked.append(enriched)
    ranked.sort(key=lambda row: (row.get("semantic_score", 0.0), row.get("priority", 0), row.get("title", row.get("name", ""))), reverse=True)
    return ranked[:limit]
