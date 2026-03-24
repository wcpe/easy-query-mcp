"""Freshness and lifecycle management for generated knowledge."""

from __future__ import annotations

import re
from .database import Database


_PATH_RE = re.compile(r"([a-zA-Z0-9_-]+):([^\s,`]+)")


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


def format_commit_map(repo_ids: list[str], repo_heads: dict[str, str]) -> str:
    """Encode repo@commit pairs for later staleness audits."""
    parts = []
    for repo_id in repo_ids:
        commit_hash = repo_heads.get(repo_id, "")
        if commit_hash:
            parts.append(f"{repo_id}@{commit_hash}")
    return ", ".join(parts)


def _extract_related_paths(row: dict) -> list[tuple[str, str]]:
    sources = [
        row.get("related_docs", ""),
        row.get("related_examples", ""),
        row.get("evidence", ""),
        row.get("content", ""),
    ]
    seen = set()
    paths: list[tuple[str, str]] = []
    for source in sources:
        for repo_id, path in _PATH_RE.findall(source):
            key = (repo_id, path)
            if key not in seen:
                seen.add(key)
                paths.append(key)
    return paths[:30]


def _path_exists(db: Database, repo_id: str, path: str) -> bool:
    if db.get_file_content(repo_id, path) is not None:
        return True
    return db.get_doc_content(repo_id, path) is not None


def _score_row(row: dict, repo_heads: dict[str, str], db: Database) -> tuple[float, str]:
    commit_map = _parse_commit_map(row.get("last_seen_commit", ""))
    mismatches = 0
    tracked = 0
    for repo_id, old_hash in commit_map.items():
        current_hash = repo_heads.get(repo_id)
        if not current_hash:
            continue
        tracked += 1
        if current_hash != old_hash:
            mismatches += 1
    mismatch_ratio = mismatches / tracked if tracked else 0.0

    related_paths = _extract_related_paths(row)
    missing = 0
    if related_paths:
        for repo_id, path in related_paths:
            if not _path_exists(db, repo_id, path):
                missing += 1
    missing_ratio = missing / len(related_paths) if related_paths else 0.0

    score = min(1.0, mismatch_ratio * 0.45 + missing_ratio * 0.55)
    if score >= 0.6:
        return round(score, 3), "stale"
    if score >= 0.25:
        return round(score, 3), "review"
    return round(score, 3), "active"


def refresh_knowledge_health(db: Database) -> dict:
    """Audit cards against current repo heads and indexed evidence."""
    repo_heads = db.snapshot_repo_heads()
    summary = {
        "repo_heads": repo_heads,
        "knowledge_cards": {"active": 0, "review": 0, "stale": 0},
        "candidate_cards": {"active": 0, "review": 0, "stale": 0},
    }

    for row in db.list_knowledge_cards(limit=5000):
        score, status = _score_row(row, repo_heads, db)
        db.update_card_health(
            row["slug"],
            table="knowledge_cards",
            stale_score=score,
            lifecycle_status=status,
            last_evidence_at=row.get("last_evidence_at", ""),
            last_seen_commit=row.get("last_seen_commit", ""),
            applicable_versions=row.get("applicable_versions", ""),
            evidence_count=int(row.get("evidence_count", 0) or 0),
        )
        summary["knowledge_cards"][status] += 1

    for row in db.get_candidates(limit=5000):
        score, status = _score_row(row, repo_heads, db)
        db.update_card_health(
            row["slug"],
            table="candidate_cards",
            stale_score=score,
            lifecycle_status=status,
            last_evidence_at=row.get("last_evidence_at", ""),
            last_seen_commit=row.get("last_seen_commit", ""),
            applicable_versions=row.get("applicable_versions", ""),
            evidence_count=int(row.get("evidence_count", 0) or 0),
        )
        summary["candidate_cards"][status] += 1

    return summary


def get_knowledge_health_summary(db: Database, limit: int = 20) -> dict:
    """Return the most stale knowledge cards plus tool-friendly summary."""
    cards = db.list_knowledge_cards(limit=5000)
    cards.sort(key=lambda c: (float(c.get("stale_score", 0) or 0), -int(c.get("priority", 0) or 0)), reverse=True)
    stale_cards = cards[:limit]
    counts = {"active": 0, "review": 0, "stale": 0}
    for card in cards:
        status = card.get("lifecycle_status", "active")
        counts[status] = counts.get(status, 0) + 1
    return {
        "counts": counts,
        "stale_cards": stale_cards,
    }
