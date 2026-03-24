"""Knowledge pipeline: review, publish, merge candidate cards."""

import json
import os
import sys
from pathlib import Path
from typing import Optional

from .database import Database
from .evaluation import run_release_gate


class KnowledgePipeline:
    """Manages the candidate -> reviewed -> published lifecycle."""

    def __init__(self, db: Database):
        self.db = db
        self.last_gate_result: Optional[dict] = None

    def list_candidates(self, status: Optional[str] = None,
                        category: Optional[str] = None) -> list[dict]:
        """List candidate cards with optional filters."""
        return self.db.get_candidates(status=status, category=category)

    def review_card(self, card_id: str, approve: bool = True,
                    reviewer: str = "",
                    review_notes: str = "",
                    owner: str = "",
                    change_summary: str = "",
                    evidence_quality: str = "") -> bool:
        """Mark a candidate as reviewed (approved or rejected)."""
        return self.db.update_candidate_review(
            card_id,
            approve=approve,
            reviewer=reviewer,
            review_notes=review_notes,
            owner=owner,
            change_summary=change_summary,
            evidence_quality=evidence_quality,
        )

    def publish_card(self, card_id: str,
                     fixture_dir: Optional[str] = None,
                     require_reviewed: bool = True,
                     min_evidence_count: int = 1,
                     max_stale_cards: int = 25) -> Optional[str]:
        """Publish a reviewed candidate to the knowledge_cards table.

        Returns the published slug or None on failure.
        """
        candidate = self.db.get_candidate(card_id)
        if candidate is None:
            print(f"[ERROR] Candidate not found: {card_id}", file=sys.stderr)
            return None

        if candidate["status"] not in ("candidate", "reviewed"):
            print(f"[WARN] Card {card_id} status is '{candidate['status']}', "
                  f"expected 'candidate' or 'reviewed'", file=sys.stderr)

        gate = run_release_gate(
            self.db,
            fixture_dir=Path(fixture_dir) if fixture_dir else None,
            candidate_id=card_id,
            require_reviewed=require_reviewed,
            min_evidence_count=min_evidence_count,
            max_stale_cards=max_stale_cards,
        )
        self.last_gate_result = gate
        if not gate["passed"]:
            print(f"[ERROR] Release gate blocked publish for {card_id}", file=sys.stderr)
            for check in gate.get("checks", []):
                if check.get("passed"):
                    continue
                print(f"  - {check['name']}: {json.dumps(check.get('details', {}), ensure_ascii=False)}",
                      file=sys.stderr)
            return None

        # Check for existing published card with same slug
        existing = self.db.get_knowledge_card(candidate["slug"])
        if existing:
            print(f"[INFO] Merging with existing card: {candidate['slug']}", file=sys.stderr)
            # Merge: append new content, update confidence
            merged_content = existing.get("content", "") + "\n\n---\n\n" + candidate.get("content", "")
            merged_tags = _merge_csv(existing.get("tags", ""), candidate.get("tags", ""))
            merged_kw = _merge_csv(existing.get("keywords", ""), candidate.get("keywords", ""))
            merged_aliases = _merge_csv(existing.get("aliases", ""), candidate.get("aliases", ""))
            merged_syms = _merge_csv(existing.get("related_symbols", ""), candidate.get("related_symbols", ""))

            card_data = {
                "card_id": existing["card_id"],
                "title": existing["title"],
                "slug": existing["slug"],
                "category": existing.get("category", candidate.get("category", "")),
                "summary": existing.get("summary", "") or candidate.get("summary", ""),
                "content": merged_content,
                "tags": merged_tags,
                "keywords": merged_kw,
                "aliases": merged_aliases,
                "related_symbols": merged_syms,
                "related_docs": _merge_csv(existing.get("related_docs", ""), candidate.get("related_docs", "")),
                "related_examples": _merge_csv(existing.get("related_examples", ""), candidate.get("related_examples", "")),
                "source_type": "hybrid",
                "priority": max(existing.get("priority", 50), 60),
                "owner": candidate.get("owner", existing.get("owner", "")),
                "reviewed_by": candidate.get("reviewed_by", existing.get("reviewed_by", "")),
                "review_notes": candidate.get("review_notes", existing.get("review_notes", "")),
                "change_summary": candidate.get("change_summary", existing.get("change_summary", "")),
                "evidence_quality": candidate.get("evidence_quality", existing.get("evidence_quality", "")),
                "applicable_versions": _merge_csv(existing.get("applicable_versions", ""), candidate.get("applicable_versions", "")),
                "lifecycle_status": candidate.get("lifecycle_status", existing.get("lifecycle_status", "active")),
                "last_evidence_at": candidate.get("last_evidence_at", existing.get("last_evidence_at", "")),
                "last_seen_commit": _merge_csv(existing.get("last_seen_commit", ""), candidate.get("last_seen_commit", "")),
                "stale_score": min(float(existing.get("stale_score", 0) or 0), float(candidate.get("stale_score", 0) or 0)),
                "evidence_count": int(existing.get("evidence_count", 0) or 0) + int(candidate.get("evidence_count", 0) or 0),
            }
        else:
            card_data = {
                "card_id": f"card-{candidate['slug']}",
                "title": candidate["title"],
                "slug": candidate["slug"],
                "category": candidate.get("category", ""),
                "summary": candidate.get("summary", ""),
                "content": candidate.get("content", ""),
                "tags": candidate.get("tags", ""),
                "keywords": candidate.get("keywords", ""),
                "aliases": candidate.get("aliases", ""),
                "related_symbols": candidate.get("related_symbols", ""),
                "related_docs": candidate.get("related_docs", ""),
                "related_examples": candidate.get("related_examples", ""),
                "source_type": "generated",
                "priority": 60,
                "owner": candidate.get("owner", ""),
                "reviewed_by": candidate.get("reviewed_by", ""),
                "review_notes": candidate.get("review_notes", ""),
                "change_summary": candidate.get("change_summary", ""),
                "evidence_quality": candidate.get("evidence_quality", ""),
                "applicable_versions": candidate.get("applicable_versions", ""),
                "lifecycle_status": candidate.get("lifecycle_status", "active"),
                "last_evidence_at": candidate.get("last_evidence_at", ""),
                "last_seen_commit": candidate.get("last_seen_commit", ""),
                "stale_score": candidate.get("stale_score", 0.0),
                "evidence_count": candidate.get("evidence_count", 0),
            }

        self.db.upsert_knowledge_card(card_data)
        self.db.update_candidate_status(card_id, "published")
        print(f"[OK] Published: {candidate['slug']}", file=sys.stderr)
        return candidate["slug"]

    def publish_all_reviewed(self, fixture_dir: Optional[str] = None) -> int:
        """Publish all reviewed candidates."""
        reviewed = self.db.get_candidates(status="reviewed")
        count = 0
        for c in reviewed:
            if self.publish_card(c["card_id"], fixture_dir=fixture_dir, require_reviewed=True, min_evidence_count=1):
                count += 1
        return count

    def publish_above_confidence(self, threshold: float = 0.7,
                                 fixture_dir: Optional[str] = None,
                                 min_evidence_count: int = 2) -> int:
        """Auto-publish candidates above a confidence threshold."""
        candidates = self.db.get_candidates(status="candidate")
        count = 0
        for c in candidates:
            if c.get("confidence", 0) >= threshold:
                if self.publish_card(
                    c["card_id"],
                    fixture_dir=fixture_dir,
                    require_reviewed=False,
                    min_evidence_count=min_evidence_count,
                ):
                    count += 1
        return count

    def export_candidate_md(self, card_id: str, output_dir: str = "knowledge_cards") -> Optional[str]:
        """Export a candidate as a markdown file with front matter."""
        candidate = self.db.get_candidate(card_id)
        if candidate is None:
            return None

        category = candidate.get("category", "general")
        out_dir = Path(output_dir) / category
        out_dir.mkdir(parents=True, exist_ok=True)

        slug = candidate["slug"]
        filepath = out_dir / f"{slug}.md"

        fm_lines = [
            "---",
            f"card_id: card-{slug}",
            f"title: {candidate['title']}",
            f"slug: {slug}",
            f"category: {category}",
            f"tags: {candidate.get('tags', '')}",
            f"keywords: {candidate.get('keywords', '')}",
            f"aliases: {candidate.get('aliases', '')}",
            f"source_type: {candidate.get('source_type', 'generated')}",
            f"priority: 60",
            f"applicable_versions: {candidate.get('applicable_versions', '')}",
            f"lifecycle_status: {candidate.get('lifecycle_status', 'active')}",
            f"stale_score: {candidate.get('stale_score', 0.0)}",
            "---",
            "",
        ]

        content = "\n".join(fm_lines) + candidate.get("content", "")
        filepath.write_text(content, encoding="utf-8")
        return str(filepath)


def _merge_csv(a: str, b: str) -> str:
    """Merge two comma-separated strings, deduplicating."""
    items_a = {x.strip() for x in a.split(",") if x.strip()}
    items_b = {x.strip() for x in b.split(",") if x.strip()}
    return ", ".join(sorted(items_a | items_b))
