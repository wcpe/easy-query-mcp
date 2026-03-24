"""Detect architecture drift between expected patterns and current code evidence."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .anti_pattern_miner import AntiPatternMiner
from .architecture_miner import ArchitectureMiner
from .config import load_config
from .database import Database


def run_architecture_drift(db: Database,
                           repo_id: Optional[str] = None,
                           limit: int = 30,
                           anti_pattern_threshold: int = 2) -> dict:
    """Compare current architecture evidence to published expectations."""
    miner = ArchitectureMiner(db)
    evidence = miner.collect_pattern_evidence(repo_id=repo_id)
    published = db.list_knowledge_cards(category="architecture", limit=5000)
    candidates = db.get_candidates(category="architecture", limit=5000)
    current_patterns = {
        slug: {
            "repos": sorted(repo_map.keys()),
            "files": sum(len(item["files"]) for item in repo_map.values()),
            "matches": sum(item["match_count"] for item in repo_map.values()),
        }
        for slug, repo_map in evidence.items()
    }
    findings: list[dict] = []

    known_cards = {}
    for row in published + candidates:
        slug = str(row.get("slug", "") or "")
        if not slug.startswith("arch-"):
            continue
        known_cards[slug.replace("arch-", "", 1)] = row

    for pattern_slug, card in known_cards.items():
        current = current_patterns.get(pattern_slug)
        historical_count = int(card.get("evidence_count", 0) or 0)
        if not current:
            findings.append({
                "type": "pattern_missing",
                "slug": f"arch-{pattern_slug}",
                "title": card.get("title", pattern_slug),
                "reason": "published architecture pattern has no current code evidence",
                "historical_evidence_count": historical_count,
                "current_match_count": 0,
                "affected_repos": [],
            })
            continue

        current_matches = int(current["matches"])
        if historical_count >= 4 and current_matches <= max(1, historical_count // 4):
            findings.append({
                "type": "pattern_evidence_regressed",
                "slug": f"arch-{pattern_slug}",
                "title": card.get("title", pattern_slug),
                "reason": "current architecture evidence is far below published baseline",
                "historical_evidence_count": historical_count,
                "current_match_count": current_matches,
                "affected_repos": current["repos"],
            })

    for pattern_slug, current in current_patterns.items():
        if pattern_slug in known_cards:
            continue
        findings.append({
            "type": "new_pattern_detected",
            "slug": f"arch-{pattern_slug}",
            "title": pattern_slug.replace("-", " ").title(),
            "reason": "current code suggests an architecture pattern without a published card",
            "historical_evidence_count": 0,
            "current_match_count": int(current["matches"]),
            "affected_repos": current["repos"],
        })

    anti_result = AntiPatternMiner(db).scan(repo_id=repo_id, persist=False)
    for item in anti_result.get("top_findings", []):
        count = int(item.get("count", 0) or 0)
        if count < anti_pattern_threshold:
            continue
        repos = sorted({example.get("repo_id", "") for example in item.get("examples", []) if example.get("repo_id")})
        findings.append({
            "type": "anti_pattern_pressure",
            "slug": f"anti-{item['slug']}",
            "title": item["slug"].replace("-", " ").title(),
            "reason": f"anti-pattern frequency reached {count} findings",
            "historical_evidence_count": 0,
            "current_match_count": count,
            "affected_repos": repos,
        })

    findings.sort(
        key=lambda item: (
            0 if item["type"] == "pattern_evidence_regressed" else
            1 if item["type"] == "pattern_missing" else
            2 if item["type"] == "anti_pattern_pressure" else 3,
            -int(item.get("current_match_count", 0) or 0),
            item["slug"],
        )
    )
    findings = findings[:limit]

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo_id": repo_id,
        "summary": {
            "known_patterns": len(known_cards),
            "detected_patterns": len(current_patterns),
            "total_findings": len(findings),
            "by_type": dict(sorted(Counter(item["type"] for item in findings).items())),
        },
        "findings": findings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect architecture drift against published knowledge")
    parser.add_argument("--config", "-c", default=None, help="Path to repos.yaml")
    parser.add_argument("--db", "-d", default=None, help="Database path override")
    parser.add_argument("--repo", "-r", default=None, help="Optional repo filter")
    parser.add_argument("--limit", type=int, default=30, help="Maximum findings to return")
    parser.add_argument("--anti-pattern-threshold", type=int, default=2, help="Minimum anti-pattern hits before surfacing drift")
    args = parser.parse_args()

    config = load_config(args.config)
    db = Database(args.db or config.db_path)
    db.initialize()
    try:
        result = run_architecture_drift(
            db,
            repo_id=args.repo,
            limit=args.limit,
            anti_pattern_threshold=args.anti_pattern_threshold,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
