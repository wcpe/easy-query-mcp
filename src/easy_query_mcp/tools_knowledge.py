"""MCP Tools - Knowledge Card search, read, summary, and suggestion tools."""

import json
import re
from typing import Optional

from .database import Database
from .query_expansion import expand_query_terms
from .repair_retrieval import collect_issue_cards, dedup_cards
from .retrieval_intent import analyze_issue_query, is_issue_oriented_query, issue_sort_key
from .source_scope import resolve_source_scope
from .pagination import clamp_limit, clamp_offset, paginate_list


def _sort_cards(cards: list[dict],
                issue_oriented: bool = False,
                intent: dict | None = None,
                query: str | None = None) -> list[dict]:
    if issue_oriented:
        cards.sort(key=lambda item: issue_sort_key(item, intent))
        return cards
    cards.sort(
        key=lambda item: (
            -float((item.get("score_breakdown") or {}).get("final_score", 0.0) or 0.0),
            -float(item.get("priority", 0) or 0),
            str(item.get("title", "")),
        )
    )
    return cards


def search_knowledge_cards(db: Database, query: str,
                           category: Optional[str] = None,
                           source_id: Optional[str] = None,
                           limit: int = 20,
                           offset: int = 0) -> str:
    """Search knowledge cards by title, tags, keywords, or content.

    Args:
        query: Search term
        category: Optional category filter (lifecycle, threading, packet, transaction, testing)
        limit: Max results

    Returns:
        JSON array of matching cards with metadata.
    """
    safe_limit = clamp_limit(limit, default=20)
    safe_offset = clamp_offset(offset)
    issue_oriented = is_issue_oriented_query(query)
    results = []
    intent = analyze_issue_query(query)
    scope = resolve_source_scope(db, source_id)
    knowledge_source_filter = None
    if source_id and scope["knowledge_ids"]:
        knowledge_source_filter = scope["knowledge_ids"][0]
    if issue_oriented and category in (None, "fix-pattern", "troubleshooting", "anti-pattern"):
        issue_payload = collect_issue_cards(db, query, limit=max(limit, 8), source_id=knowledge_source_filter)
        if category:
            category_key = {
                "fix-pattern": "fix_patterns",
                "troubleshooting": "troubleshooting",
                "anti-pattern": "anti_patterns",
            }.get(category, category)
            results.extend(issue_payload.get(category_key, []))
        else:
            results.extend(issue_payload["results"])
    results.extend(db.search_knowledge_cards(query, category=category, limit=limit, source_id=knowledge_source_filter))
    results = _sort_cards(_dedup_results(results), issue_oriented=issue_oriented, intent=intent)
    if not results:
        return json.dumps({
            "status": "no_results",
            "message": f"No knowledge cards found matching '{query}'.",
            "suggestions": [
                "Try broader keywords",
                "Check available categories: lifecycle, threading, packet, transaction, testing, architecture, troubleshooting, fix-pattern, anti-pattern",
                "Use search_by_concept for natural language queries",
            ],
        }, ensure_ascii=False, indent=2)

    paged, meta = paginate_list(results, safe_limit, safe_offset, default_limit=20)
    return json.dumps({
        "status": "ok",
        "strategy": intent["query_mode"] if issue_oriented else "default",
        "intent": intent,
        "count": meta["returned"],
        "pagination": meta,
        "results": paged,
    }, ensure_ascii=False, indent=2)


def read_knowledge_card(db: Database, slug: str) -> str:
    """Read a complete knowledge card by slug or card_id.

    Args:
        slug: Card slug (e.g. 'easyquery-transaction') or card_id

    Returns:
        Full card content including metadata, body, and related items.
    """
    card = db.get_knowledge_card(slug)
    if card is None:
        return json.dumps({
            "status": "not_found",
            "message": f"Knowledge card '{slug}' not found.",
            "suggestions": [
                "Use search_knowledge_cards to find available cards",
                "Check the slug spelling",
            ],
        }, ensure_ascii=False, indent=2)

    return json.dumps({
        "status": "ok",
        "card": {
            "card_id": card["card_id"],
            "title": card["title"],
            "slug": card["slug"],
            "category": card["category"],
            "summary": card["summary"],
            "content": card["content"],
            "tags": card["tags"],
            "keywords": card["keywords"],
            "when_to_use": card["when_to_use"],
            "avoid": card["avoid"],
            "best_practices": card["best_practices"],
            "related_symbols": card["related_symbols"],
            "related_docs": card["related_docs"],
            "related_examples": card["related_examples"],
            "related_errors": card["related_errors"],
            "priority": card["priority"],
            "applicable_versions": card.get("applicable_versions", ""),
            "lifecycle_status": card.get("lifecycle_status", "active"),
            "stale_score": card.get("stale_score", 0.0),
            "evidence_count": card.get("evidence_count", 0),
            "owner": card.get("owner", ""),
            "reviewed_by": card.get("reviewed_by", ""),
            "review_notes": card.get("review_notes", ""),
            "change_summary": card.get("change_summary", ""),
            "evidence_quality": card.get("evidence_quality", ""),
            "explainability": db.explain_knowledge_card(slug),
        },
    }, ensure_ascii=False, indent=2)


def get_knowledge_card_summary(db: Database, slug: str) -> str:
    """Get a brief summary of a knowledge card.

    Args:
        slug: Card slug or card_id

    Returns:
        JSON with title, summary, tags, category, and next steps.
    """
    card = db.get_knowledge_card(slug)
    if card is None:
        return json.dumps({
            "status": "not_found",
            "message": f"Knowledge card '{slug}' not found.",
        }, ensure_ascii=False, indent=2)

    return json.dumps({
        "status": "ok",
        "title": card["title"],
        "category": card["category"],
        "summary": card["summary"],
        "tags": card["tags"],
        "when_to_use": card["when_to_use"],
        "avoid": card["avoid"],
        "applicable_versions": card.get("applicable_versions", ""),
        "lifecycle_status": card.get("lifecycle_status", "active"),
        "stale_score": card.get("stale_score", 0.0),
        "evidence_count": card.get("evidence_count", 0),
        "change_summary": card.get("change_summary", ""),
        "review_notes": card.get("review_notes", ""),
        "next_steps": [
            f"read_knowledge_card(slug='{card['slug']}') for full content",
            f"search_by_concept with keywords from this card",
            f"search_symbol for related APIs: {card['related_symbols'][:100]}" if card["related_symbols"] else None,
        ],
    }, ensure_ascii=False, indent=2)


def suggest_knowledge_cards(db: Database, task: str, limit: int = 5) -> str:
    """Suggest relevant knowledge cards based on a task description.

    Args:
        task: Task description (e.g. 'fix packet registration issue')
        limit: Max suggestions

    Returns:
        JSON array of recommended cards with relevance reasoning.
    """
    # Extract keywords
    stop = {"the", "a", "an", "is", "are", "was", "were", "be", "been",
            "to", "of", "in", "for", "on", "with", "at", "by", "from",
            "and", "but", "or", "not", "no", "if", "when", "how", "what",
            "fix", "add", "implement", "create", "make", "update", "change",
            "need", "want", "should", "must", "can", "will", "would"}

    words = re.findall(r'\w+', task.lower())
    keywords = [w for w in words if w not in stop and len(w) > 2]
    keywords = list(dict.fromkeys(expand_query_terms(task) + keywords))

    if not keywords:
        keywords = words[:5]

    intent = analyze_issue_query(task)
    issue_oriented = bool(intent["issue_oriented"])
    suggestions = []
    if issue_oriented:
        issue_payload = collect_issue_cards(db, task, limit=max(limit, 8))
        suggestions.extend(issue_payload["results"])

    # Search with each keyword and collect results
    seen = set(item.get("card_id") for item in suggestions if item.get("card_id"))

    for kw in keywords[:6]:
        cards = db.search_knowledge_cards(kw, limit=3)
        for c in cards:
            cid = c["card_id"]
            if cid not in seen:
                seen.add(cid)
                suggestions.append({
                    "card_id": c["card_id"],
                    "title": c["title"],
                    "slug": c["slug"],
                    "category": c["category"],
                    "summary": c["summary"],
                    "priority": c["priority"],
                    "matched_keyword": kw,
                    "score_breakdown": c.get("score_breakdown"),
                    "match_details": c.get("match_details", []),
                })

    suggestions = dedup_cards(suggestions)
    _sort_cards(suggestions, issue_oriented=issue_oriented, intent=intent, query=task)
    suggestions = suggestions[:limit]

    if not suggestions:
        return json.dumps({
            "status": "no_suggestions",
            "message": f"No relevant knowledge cards found for: '{task}'",
            "keywords_tried": keywords,
        }, ensure_ascii=False, indent=2)

    return json.dumps({
        "status": "ok",
        "task": task,
        "strategy": intent["query_mode"] if issue_oriented else "default",
        "intent": intent,
        "keywords_used": keywords,
        "count": len(suggestions),
        "suggestions": suggestions,
    }, ensure_ascii=False, indent=2)


def explain_knowledge_decision(db: Database, slug: str, query: str = "") -> str:
    """Explain why a knowledge card is relevant, including score and evidence."""
    payload = db.explain_knowledge_card(slug, query=query)
    if payload is None:
        return json.dumps({
            "status": "not_found",
            "message": f"Knowledge card '{slug}' not found.",
        }, ensure_ascii=False, indent=2)
    return json.dumps({
        "status": "ok",
        **payload,
    }, ensure_ascii=False, indent=2)


def _dedup_results(results: list[dict]) -> list[dict]:
    return dedup_cards(results)
