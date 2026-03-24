"""Architecture anti-pattern mining and risk reporting."""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

import yaml

from .database import Database
from .knowledge_health import format_commit_map


_DEFAULT_PROXIMITY = 30
_EXCLUSION_CONTEXT_LINES = 5


def _load_patterns() -> list[dict]:
    from .config import _find_project_root

    path = _find_project_root() / "config" / "anti_patterns.yaml"
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except Exception as exc:
        print(f"[WARN] Failed to load anti-pattern config: {exc}", file=sys.stderr)
        return []

    patterns: list[dict] = []
    for slug, cfg in raw.get("patterns", {}).items():
        keywords = cfg.get("keywords", [])
        indicators = cfg.get("indicators", [])
        if not keywords or not indicators:
            continue
        try:
            keyword_re = re.compile("|".join(keywords), re.IGNORECASE)
            indicator_re = re.compile("|".join(indicators), re.IGNORECASE)
        except re.error as exc:
            print(f"[WARN] Skipping anti-pattern '{slug}' due to invalid regex: {exc}", file=sys.stderr)
            continue

        # Load optional exclusion patterns
        exclusion_re = None
        exclusions = cfg.get("exclusions", [])
        if exclusions:
            try:
                exclusion_re = re.compile("|".join(exclusions), re.IGNORECASE)
            except re.error as exc:
                print(f"[WARN] Invalid exclusion regex for '{slug}': {exc}", file=sys.stderr)

        patterns.append({
            "slug": slug,
            "category": cfg.get("category", "anti-pattern"),
            "confidence": float(cfg.get("confidence", 0.6)),
            "description": cfg.get("description", ""),
            "avoid": cfg.get("avoid", []),
            "recommended_cards": cfg.get("recommended_cards", []),
            "keyword_re": keyword_re,
            "indicator_re": indicator_re,
            "exclusion_re": exclusion_re,
            "proximity": int(cfg.get("proximity", _DEFAULT_PROXIMITY)),
        })
    return patterns


def _find_matching_lines(regex: re.Pattern, lines: list[str]) -> list[int]:
    """Return 0-based line indices where *regex* matches."""
    return [i for i, line in enumerate(lines) if regex.search(line)]


def _has_nearby_exclusion(exclusion_re: re.Pattern | None,
                          lines: list[str],
                          indicator_line: int) -> bool:
    """Check whether an exclusion pattern appears within ±EXCLUSION_CONTEXT_LINES of *indicator_line*."""
    if exclusion_re is None:
        return False
    start = max(0, indicator_line - _EXCLUSION_CONTEXT_LINES)
    end = min(len(lines), indicator_line + _EXCLUSION_CONTEXT_LINES + 1)
    context = "\n".join(lines[start:end])
    return bool(exclusion_re.search(context))


class AntiPatternMiner:
    """Detect risky architectural patterns from indexed code."""

    def __init__(self, db: Database):
        self.db = db
        self.patterns = _load_patterns()

    def scan(self, repo_id: Optional[str] = None, persist: bool = False) -> dict:
        stats = {"patterns_loaded": len(self.patterns), "findings": 0, "cards_generated": 0}
        if not self.patterns:
            return stats

        repo_heads = self.db.snapshot_repo_heads()
        conn = self.db.connect()
        sql = """SELECT repo_id, rel_path, content, language FROM files
                 WHERE language IN ('kotlin', 'java') AND content != ''"""
        params: list = []
        if repo_id:
            sql += " AND repo_id = ?"
            params.append(repo_id)
        rows = conn.execute(sql, params).fetchall()

        findings: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            item = dict(row)
            content = item["content"]
            lines = content.splitlines()
            for pattern in self.patterns:
                # Quick whole-file pre-filter (cheap)
                if not pattern["keyword_re"].search(content):
                    continue
                if not pattern["indicator_re"].search(content):
                    continue

                # Line-level proximity check
                kw_lines = _find_matching_lines(pattern["keyword_re"], lines)
                ind_lines = _find_matching_lines(pattern["indicator_re"], lines)
                if not kw_lines or not ind_lines:
                    continue

                proximity = pattern["proximity"]
                exclusion_re = pattern["exclusion_re"]

                # Find indicator lines that are within proximity of any keyword line
                matched = False
                for ind_ln in ind_lines:
                    if any(abs(ind_ln - kw_ln) <= proximity for kw_ln in kw_lines):
                        # Check exclusion patterns in the indicator's neighbourhood
                        if _has_nearby_exclusion(exclusion_re, lines, ind_ln):
                            continue
                        line_no = ind_ln + 1  # 1-based
                        start = max(0, ind_ln - 2)
                        end = min(len(lines), ind_ln + 3)
                        findings[pattern["slug"]].append({
                            "repo_id": item["repo_id"],
                            "path": item["rel_path"],
                            "line": line_no,
                            "snippet": "\n".join(lines[start:end])[:400],
                        })
                        stats["findings"] += 1
                        matched = True
                        break  # one finding per file per pattern
                # (no finding recorded if all candidates were excluded or too far)

        if persist:
            for pattern in self.patterns:
                matches = findings.get(pattern["slug"], [])
                if not matches:
                    continue
                repos = sorted({m["repo_id"] for m in matches})
                slug = f"anti-{pattern['slug']}"
                card = {
                    "card_id": f"gen-{slug}",
                    "title": pattern["slug"].replace("-", " ").title() + " Risk",
                    "slug": slug,
                    "category": "anti-pattern",
                    "summary": f"Risk pattern '{pattern['slug']}' detected in {len(matches)} files across {len(repos)} repos.",
                    "content": self._build_content(pattern, matches),
                    "tags": ", ".join(["anti-pattern", "risk"] + repos),
                    "keywords": pattern["slug"].replace("-", ", "),
                    "aliases": pattern["slug"].replace("-", " "),
                    "related_symbols": "",
                    "related_docs": "",
                    "related_examples": "",
                    "source_type": "generated",
                    "confidence": min(1.0, pattern["confidence"] + min(len(matches) / 20.0, 0.15)),
                    "status": "candidate",
                    "evidence": "\n".join(
                        f"{m['repo_id']}:{m['path']}:{m['line']}" for m in matches[:20]
                    ),
                    "last_evidence_at": "",
                    "last_seen_commit": format_commit_map(repos, repo_heads),
                    "stale_score": 0.0,
                    "evidence_count": len(matches),
                }
                self.db.upsert_candidate(card)
                for related in pattern.get("recommended_cards", []):
                    self.db.upsert_relation(slug, related, "warns_about", 0.72, "anti-pattern")
                stats["cards_generated"] += 1

        stats["top_findings"] = [
            {
                "slug": slug,
                "count": len(matches),
                "examples": matches[:3],
            }
            for slug, matches in sorted(findings.items(), key=lambda item: len(item[1]), reverse=True)
        ]
        return stats

    def _build_content(self, pattern: dict, matches: list[dict]) -> str:
        parts = [f"# {pattern['slug'].replace('-', ' ').title()} Risk\n"]
        parts.append(f"**Description**: {pattern['description']}")
        if pattern.get("avoid"):
            parts.append("\n## Avoid\n")
            for line in pattern["avoid"]:
                parts.append(f"- {line}")
        parts.append("\n## Findings\n")
        for finding in matches[:5]:
            parts.append(f"### {finding['repo_id']}:{finding['path']} (L{finding['line']})")
            parts.append(f"```text\n{finding['snippet']}\n```")
        if pattern.get("recommended_cards"):
            parts.append("\n## Recommended Cards\n")
            for slug in pattern["recommended_cards"]:
                parts.append(f"- `{slug}`")
        return "\n".join(parts)
