"""Detect stale, drifting, and regressing knowledge signals."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .architecture_drift import run_architecture_drift
from .config import load_config
from .database import Database


_STATUS_PRIORITY = {
    "architecture_drift_detected": 0,
    "pattern_regression_detected": 0,
    "conflicting_evidence": 1,
    "possibly_stale": 2,
    "needs_review": 3,
}


def _parse_commit_map(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for chunk in (raw or "").split(","):
        item = chunk.strip()
        if "@" not in item:
            continue
        repo_id, commit_hash = item.split("@", 1)
        repo_id = repo_id.strip()
        commit_hash = commit_hash.strip()
        if repo_id and commit_hash:
            result[repo_id] = commit_hash
    return result


def _append_finding(findings: list[dict], seen: set[tuple[str, str]], finding: dict) -> None:
    key = (finding["slug"], finding["status"])
    if key in seen:
        return
    seen.add(key)
    findings.append(finding)


def run_drift_audit(db: Database,
                    changed_only: bool = False,
                    include_candidates: bool = True,
                    limit: int = 50,
                    telemetry_limit: int = 400,
                    regression_threshold: int = 3) -> dict:
    previous_heads = db.get_repo_heads()
    current_heads = db.snapshot_repo_heads()
    changed_repos = {
        repo_id
        for repo_id, commit_hash in current_heads.items()
        if previous_heads.get(repo_id) != commit_hash
    }
    if not changed_only:
        changed_repos = set(current_heads)

    findings: list[dict] = []
    seen: set[tuple[str, str]] = set()
    rows = [("published", row) for row in db.list_knowledge_cards(limit=5000)]
    if include_candidates:
        rows.extend(("candidate", row) for row in db.get_candidates(limit=5000))

    for origin, row in rows:
        slug = row["slug"]
        category = row.get("category", "")
        lifecycle = row.get("lifecycle_status", "active") or "active"
        stale_score = float(row.get("stale_score", 0.0) or 0.0)
        evidence_count = int(row.get("evidence_count", 0) or 0)
        commit_map = _parse_commit_map(row.get("last_seen_commit", ""))
        tracked_repos = set(commit_map)
        related_changes = sorted(changed_repos & tracked_repos)

        if changed_only and tracked_repos and not related_changes:
            continue

        if lifecycle == "stale" or stale_score >= 0.6:
            _append_finding(findings, seen, {
                "slug": slug,
                "title": row.get("title", slug),
                "category": category,
                "origin": origin,
                "status": "possibly_stale",
                "reasons": [f"stale_score={round(stale_score, 3)}", f"lifecycle_status={lifecycle}"],
                "affected_repos": related_changes or sorted(tracked_repos),
            })
            continue

        if stale_score >= 0.25 and evidence_count > 0 and (related_changes or lifecycle == "review"):
            _append_finding(findings, seen, {
                "slug": slug,
                "title": row.get("title", slug),
                "category": category,
                "origin": origin,
                "status": "conflicting_evidence",
                "reasons": [
                    f"tracked repos changed: {', '.join(related_changes) or 'none'}",
                    f"stale_score={round(stale_score, 3)} with evidence_count={evidence_count}",
                ],
                "affected_repos": related_changes or sorted(tracked_repos),
            })
            continue

        if lifecycle == "review" or (
            related_changes and category in {"architecture", "fix-pattern", "anti-pattern", "troubleshooting"}
        ) or (
            category == "fix-pattern" and evidence_count < 2
        ):
            _append_finding(findings, seen, {
                "slug": slug,
                "title": row.get("title", slug),
                "category": category,
                "origin": origin,
                "status": "needs_review",
                "reasons": [
                    f"lifecycle_status={lifecycle}",
                    f"affected_repos={', '.join(related_changes) or 'n/a'}",
                    f"evidence_count={evidence_count}",
                ],
                    "affected_repos": related_changes or sorted(tracked_repos),
            })

    architecture_drift = run_architecture_drift(db, repo_id=None, limit=limit)
    for finding in architecture_drift.get("findings", []):
        _append_finding(findings, seen, {
            "slug": finding["slug"],
            "title": finding.get("title", finding["slug"]),
            "category": "architecture",
            "origin": "architecture_drift",
            "status": "architecture_drift_detected",
            "reasons": [
                finding.get("reason", ""),
                f"type={finding.get('type', '')}",
                f"current_match_count={finding.get('current_match_count', 0)}",
            ],
            "affected_repos": finding.get("affected_repos", []),
            "occurrence_count": finding.get("current_match_count", 0),
        })

    diagnose_records = db.get_tool_telemetry(limit=telemetry_limit, tool_name="diagnose_from_log")
    pattern_counts = Counter()
    for record in diagnose_records:
        for pattern in record.get("details", {}).get("matched_patterns", []):
            pattern_counts[pattern] += 1
    for pattern, count in pattern_counts.items():
        if count < regression_threshold:
            continue
        _append_finding(findings, seen, {
            "slug": f"error-{pattern}",
            "title": pattern.replace("-", " ").title(),
            "category": "troubleshooting",
            "origin": "telemetry",
            "status": "pattern_regression_detected",
            "reasons": [f"{count} recent diagnoses matched pattern '{pattern}'"],
            "affected_repos": [],
            "occurrence_count": count,
        })

    findings.sort(key=lambda item: (
        _STATUS_PRIORITY.get(item["status"], 99),
        -int(item.get("occurrence_count", 0) or 0),
        item["slug"],
    ))
    findings = findings[:limit]

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "changed_only": changed_only,
        "changed_repos": sorted(changed_repos),
        "summary": {
            "total_findings": len(findings),
            "by_status": dict(sorted(Counter(item["status"] for item in findings).items())),
            "by_category": dict(sorted(Counter(item.get("category", "") or "unknown" for item in findings).items())),
        },
        "findings": findings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit knowledge drift and regression signals")
    parser.add_argument("--config", "-c", default=None, help="Path to repos.yaml")
    parser.add_argument("--db", "-d", default=None, help="Database path override")
    parser.add_argument("--changed-only", action="store_true", help="Only report items affected by repos whose HEAD changed since last snapshot")
    parser.add_argument("--limit", type=int, default=50, help="Maximum number of findings to return")
    parser.add_argument("--telemetry-limit", type=int, default=400, help="How many recent telemetry rows to inspect")
    parser.add_argument("--regression-threshold", type=int, default=3, help="How many repeated pattern hits trigger regression output")
    args = parser.parse_args()

    config = load_config(args.config)
    db = Database(args.db or config.db_path)
    db.initialize()
    try:
        result = run_drift_audit(
            db,
            changed_only=args.changed_only,
            limit=args.limit,
            telemetry_limit=args.telemetry_limit,
            regression_threshold=args.regression_threshold,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
