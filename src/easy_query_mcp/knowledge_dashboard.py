"""Operational dashboard for knowledge quality, usage, and release health."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone

from .architecture_drift import run_architecture_drift
from .config import load_config
from .database import Database
from .knowledge_health import get_knowledge_health_summary, refresh_knowledge_health
from .retrieval_intent import contains_cjk


def _top_counter(counter: Counter, limit: int) -> list[dict]:
    return [
        {"name": name, "count": count}
        for name, count in counter.most_common(limit)
    ]


def build_knowledge_dashboard(db: Database, limit: int = 10,
                              refresh: bool = False,
                              telemetry_limit: int = 2000) -> dict:
    audit = refresh_knowledge_health(db) if refresh else None
    health = get_knowledge_health_summary(db, limit=limit)
    cards = db.get_all_knowledge_cards()
    candidates = db.get_candidates(limit=5000)
    relations = db.get_all_relations()
    telemetry = db.get_tool_telemetry(limit=telemetry_limit)

    published_by_category = Counter(card.get("category", "unknown") or "unknown" for card in cards)
    published_by_status = Counter(card.get("lifecycle_status", "active") or "active" for card in cards)
    candidate_by_status = Counter(card.get("status", "candidate") or "candidate" for card in candidates)
    candidate_by_category = Counter(card.get("category", "unknown") or "unknown" for card in candidates)

    top_gate_failures = Counter()
    suspected_false_positives = Counter()
    false_positive_by_tool = Counter()
    false_positive_by_reason = Counter()
    false_positive_by_category = Counter()
    most_used_cards = Counter()
    diagnose_patterns = Counter()
    fix_patterns = Counter()
    source_hits = defaultdict(lambda: {"queries": 0, "hits": 0})
    bilingual = {"queries": 0, "successes": 0}
    no_match_calls = 0
    search_calls = 0

    for record in telemetry:
        tool_name = record.get("tool_name", "")
        status = record.get("status", "")
        details = record.get("details", {}) or {}
        query_text = record.get("query_text", "") or ""

        if tool_name in {"search_knowledge_cards", "search_docs", "search_by_concept", "suggest_knowledge_cards", "diagnose_from_log", "explain_error"}:
            search_calls += 1
            if status in {"no_match", "no_results", "partial_match", "error"}:
                no_match_calls += 1

        if contains_cjk(query_text) and tool_name in {"search_knowledge_cards", "search_docs", "search_by_concept", "suggest_knowledge_cards"}:
            bilingual["queries"] += 1
            if status == "ok":
                bilingual["successes"] += 1

        if status == "partial_match" and query_text:
            suspected_false_positives[query_text] += 1
            false_positive_by_tool[tool_name] += 1

        for reason in details.get("false_positive_reasons", []):
            false_positive_by_reason[reason] += 1
        if details.get("false_positive_signal"):
            false_positive_by_tool[tool_name] += 1
            if query_text:
                suspected_false_positives[query_text] += 1
        for category in details.get("selected_categories", []):
            if details.get("false_positive_signal"):
                false_positive_by_category[category or "unknown"] += 1

        for failure in details.get("gate_failures", []):
            top_gate_failures[failure] += 1

        accessed_slug = details.get("accessed_slug")
        if accessed_slug:
            most_used_cards[accessed_slug] += 1

        for slug in details.get("selected_slugs", []):
            most_used_cards[slug] += 1

        for pattern in details.get("matched_patterns", []):
            diagnose_patterns[pattern] += 1

        for slug in details.get("proven_fix_patterns", []):
            fix_patterns[slug] += 1

        for slug in details.get("selected_fix_patterns", []):
            fix_patterns[slug] += 1

        source_map = details.get("source_hits", {})
        if isinstance(source_map, dict):
            for source_name, hit in source_map.items():
                source_hits[source_name]["queries"] += 1
                source_hits[source_name]["hits"] += int(bool(hit))

    recent_published = sorted(
        (
            {
                "card_id": card["card_id"],
                "slug": card["slug"],
                "title": card["title"],
                "category": card.get("category", ""),
                "published_at": card.get("published_at", ""),
            }
            for card in candidates
            if card.get("published_at")
        ),
        key=lambda item: item["published_at"],
        reverse=True,
    )[:limit]
    architecture_drift = run_architecture_drift(db, limit=limit)

    dashboard = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "audit": audit,
        "cards": {
            "published_total": len(cards),
            "published_by_category": dict(sorted(published_by_category.items())),
            "published_by_status": dict(sorted(published_by_status.items())),
            "candidate_total": len(candidates),
            "candidate_by_status": dict(sorted(candidate_by_status.items())),
            "candidate_by_category": dict(sorted(candidate_by_category.items())),
            "recent_published_cards": recent_published,
        },
        "staleness": {
            "top_stale_cards": [
                {
                    "slug": card["slug"],
                    "title": card["title"],
                    "category": card.get("category", ""),
                    "stale_score": card.get("stale_score", 0),
                    "lifecycle_status": card.get("lifecycle_status", "active"),
                }
                for card in health.get("stale_cards", [])[:limit]
            ],
        },
        "graph": {
            "top_relations": relations[:limit],
        },
        "architecture_drift": architecture_drift,
        "telemetry": {
            "total_calls": len(telemetry),
            "no_match_rate": round((no_match_calls / search_calls) * 100.0, 2) if search_calls else 0.0,
            "suspected_false_positives": _top_counter(suspected_false_positives, limit),
            "false_positive_by_tool": _top_counter(false_positive_by_tool, limit),
            "false_positive_by_reason": _top_counter(false_positive_by_reason, limit),
            "false_positive_by_category": _top_counter(false_positive_by_category, limit),
            "publish_gate_failure_reasons": _top_counter(top_gate_failures, limit),
            "most_used_cards": _top_counter(most_used_cards, limit),
            "most_triggered_diagnose_patterns": _top_counter(diagnose_patterns, limit),
            "most_used_fix_patterns": _top_counter(fix_patterns, limit),
            "retrieval_hit_rate_by_source_type": {
                source_name: round((stats["hits"] / stats["queries"]) * 100.0, 2) if stats["queries"] else 0.0
                for source_name, stats in sorted(source_hits.items())
            },
            "bilingual_query_success_rate": round(
                (bilingual["successes"] / bilingual["queries"]) * 100.0,
                2,
            ) if bilingual["queries"] else 0.0,
        },
    }
    return dashboard


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a knowledge operations dashboard")
    parser.add_argument("--config", "-c", default=None, help="Path to repos.yaml")
    parser.add_argument("--db", "-d", default=None, help="Database path override")
    parser.add_argument("--limit", type=int, default=10, help="Maximum number of rows per section")
    parser.add_argument("--refresh", action="store_true", help="Refresh freshness state before generating dashboard output")
    parser.add_argument("--telemetry-limit", type=int, default=2000, help="How many telemetry rows to inspect")
    args = parser.parse_args()

    config = load_config(args.config)
    db = Database(args.db or config.db_path)
    db.initialize()
    try:
        dashboard = build_knowledge_dashboard(
            db,
            limit=args.limit,
            refresh=args.refresh,
            telemetry_limit=args.telemetry_limit,
        )
        print(json.dumps(dashboard, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
