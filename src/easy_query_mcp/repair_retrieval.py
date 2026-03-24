"""Shared issue-oriented retrieval helpers."""

from __future__ import annotations

from .database import Database
from .query_expansion import expand_query_terms
from .retrieval_intent import analyze_issue_query, issue_sort_key


def dedup_cards(cards: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for item in cards:
        key = item.get("card_id") or item.get("slug")
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def collect_issue_cards(db: Database, query: str, limit: int = 12,
                        source_id: str | None = None) -> dict:
    """Collect fix/troubleshooting cards for an issue-oriented query."""
    intent = analyze_issue_query(query)
    terms = list(dict.fromkeys(intent.get("normalized_terms", []) + expand_query_terms(query)))
    categories = [item for item in intent.get("preferred_categories", []) if item in {"fix-pattern", "troubleshooting", "anti-pattern", "architecture"}]
    if not categories:
        categories = ["fix-pattern", "troubleshooting", "anti-pattern"]

    collected: dict[str, list[dict]] = {category: [] for category in categories}
    all_cards: list[dict] = []

    for category in categories:
        for term in terms[:8]:
            rows = db.search_knowledge_cards(term, category=category, limit=min(4, limit), source_id=source_id)
            for row in rows:
                row.setdefault("retrieval_reason", f"{category}:{term}")
            collected[category].extend(rows)
            all_cards.extend(rows)

    related_fix_patterns: list[dict] = []
    for card in dedup_cards(collected.get("troubleshooting", []))[:4]:
        for relation in db.get_related_cards(card.get("slug", ""), limit=8):
            related_slug = relation["target_slug"] if relation["source_slug"] == card.get("slug", "") else relation["source_slug"]
            linked = db.get_knowledge_card(related_slug)
            if not linked or linked.get("category") != "fix-pattern":
                continue
            linked.setdefault("retrieval_reason", f"relation:{card.get('slug', '')}")
            related_fix_patterns.append(linked)

    collected.setdefault("fix-pattern", []).extend(related_fix_patterns)
    all_cards.extend(related_fix_patterns)

    deduped_all = dedup_cards(all_cards)
    deduped_all.sort(key=lambda item: issue_sort_key(item, intent))

    result = {
        "intent": intent,
        "results": deduped_all[:limit],
        "fix_patterns": dedup_cards(collected.get("fix-pattern", []))[:limit],
        "troubleshooting": dedup_cards(collected.get("troubleshooting", []))[:limit],
        "anti_patterns": dedup_cards(collected.get("anti-pattern", []))[:limit],
    }
    result["fix_patterns"].sort(key=lambda item: issue_sort_key(item, intent))
    result["troubleshooting"].sort(key=lambda item: issue_sort_key(item, intent))
    result["anti_patterns"].sort(key=lambda item: issue_sort_key(item, intent))
    return result
