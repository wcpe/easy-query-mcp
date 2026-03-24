"""Mine reusable fix patterns from local git history."""

from __future__ import annotations

import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

import yaml

from .database import Database
from .knowledge_health import format_commit_map


def _load_fix_patterns() -> list[dict]:
    from .config import _find_project_root

    path = _find_project_root() / "config" / "fix_patterns.yaml"
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except Exception as exc:
        print(f"[WARN] Failed to load fix patterns: {exc}", file=sys.stderr)
        return []

    patterns: list[dict] = []
    for slug, cfg in raw.get("patterns", {}).items():
        message_regexes = cfg.get("message_regexes", [])
        if not message_regexes:
            continue
        patterns.append({
            "slug": slug,
            "description": cfg.get("description", ""),
            "message_res": [re.compile(regex, re.IGNORECASE) for regex in message_regexes],
            "file_re": re.compile("|".join(cfg.get("file_hints", ["."])), re.IGNORECASE),
            "related_cards": cfg.get("related_cards", []),
            "suggested_actions": cfg.get("suggested_actions", []),
        })
    return patterns


class FixPatternMiner:
    """Extract fix knowledge from local repository history."""

    def __init__(self, db: Database):
        self.db = db
        self.patterns = _load_fix_patterns()

    def mine(self, repo_id: Optional[str] = None,
             max_commits: int = 120,
             persist: bool = False) -> dict:
        stats = {"patterns_loaded": len(self.patterns), "matched_commits": 0, "cards_generated": 0}
        if not self.patterns:
            return stats

        repo_heads = self.db.snapshot_repo_heads()
        repo_rows = [repo for repo in self.db.get_all_repos() if not repo_id or repo["id"] == repo_id]
        grouped: dict[str, list[dict]] = defaultdict(list)

        for repo in repo_rows:
            for commit in self._iter_commits(repo["path"], max_commits):
                commit["repo_id"] = repo["id"]
                for pattern in self.patterns:
                    if self._matches_pattern(pattern, commit):
                        grouped[pattern["slug"]].append(commit)
                        stats["matched_commits"] += 1
                        break

        if persist:
            for pattern in self.patterns:
                commits = grouped.get(pattern["slug"], [])
                if not commits:
                    continue
                repos = sorted({commit["repo_id"] for commit in commits})
                profile = self._build_fix_profile(pattern, commits)
                slug = f"fix-{pattern['slug']}"
                card = {
                    "card_id": f"gen-{slug}",
                    "title": pattern["slug"].replace("-", " ").title() + " Fix Pattern",
                    "slug": slug,
                    "category": "fix-pattern",
                    "summary": (
                        f"Observed fix pattern '{pattern['slug']}' in {len(commits)} commits across "
                        f"{len(repos)} repos. Typical hotspots: {', '.join(profile['patch_hotspots'][:3]) or 'n/a'}."
                    ),
                    "content": self._build_content(pattern, profile),
                    "tags": ", ".join(["fix-pattern"] + repos),
                    "keywords": ", ".join(
                        [pattern["slug"].replace("-", " ")] + profile["patch_hotspots"][:4] + profile["related_tests"][:3]
                    ),
                    "aliases": ", ".join([pattern["slug"].replace("-", " "), profile["symptom"]]),
                    "related_symbols": "",
                    "related_docs": "",
                    "related_examples": "",
                    "source_type": "generated",
                    "confidence": min(0.95, 0.62 + min(len(commits) / 15.0, 0.25)),
                    "status": "candidate",
                    "evidence": "\n".join(
                        f"{commit['repo_id']}:{commit['hash']} {commit['subject']}" for commit in commits[:25]
                    ),
                    "last_evidence_at": profile["last_evidence_at"],
                    "last_seen_commit": format_commit_map(repos, repo_heads),
                    "stale_score": 0.0,
                    "evidence_count": len(commits),
                }
                self.db.upsert_candidate(card)
                for related in pattern.get("related_cards", []):
                    self.db.upsert_relation(slug, related, "fixes", 0.8, "fix-pattern")
                stats["cards_generated"] += 1

        stats["top_patterns"] = [
            {
                "slug": slug,
                "count": len(commits),
                "examples": commits[:3],
            }
            for slug, commits in sorted(grouped.items(), key=lambda item: len(item[1]), reverse=True)
        ]
        return stats

    def _iter_commits(self, repo_path: str, max_commits: int) -> list[dict]:
        try:
            result = subprocess.run(
                [
                    "git",
                    "log",
                    "--no-merges",
                    f"-n{max_commits}",
                    "--date=short",
                    "--name-only",
                    "--pretty=format:%H%x1f%ad%x1f%s%x1e",
                ],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=25,
                encoding="utf-8",
                errors="replace",
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []
        if result.returncode != 0 or not result.stdout:
            return []

        commits: list[dict] = []
        for block in result.stdout.split("\x1e"):
            block = block.strip()
            if not block:
                continue
            lines = [line for line in block.splitlines() if line.strip()]
            if not lines:
                continue
            meta = lines[0].split("\x1f")
            if len(meta) < 3:
                continue
            commit_hash, date, subject = meta[0].strip(), meta[1].strip(), meta[2].strip()
            files = [line.strip() for line in lines[1:] if line.strip()]
            commits.append({
                "hash": commit_hash,
                "date": date,
                "subject": subject,
                "files": files,
            })
        return commits

    def _matches_pattern(self, pattern: dict, commit: dict) -> bool:
        text = f"{commit['subject']} {' '.join(commit.get('files', []))}"
        message_hits = sum(1 for regex in pattern["message_res"] if regex.search(text))
        file_hit = any(pattern["file_re"].search(path) for path in commit.get("files", [])) or not commit.get("files")
        return message_hits >= 2 and file_hit

    def _build_fix_profile(self, pattern: dict, commits: list[dict]) -> dict:
        hotspot_counts: dict[str, int] = defaultdict(int)
        test_files: list[str] = []
        for commit in commits:
            for file_path in commit.get("files", []):
                name = Path(file_path).name
                if not name:
                    continue
                hotspot_counts[name] += 1
                lower_name = name.lower()
                if ("test" in lower_name or "spec" in lower_name) and name not in test_files:
                    test_files.append(name)

        patch_hotspots = [
            name for name, _ in sorted(hotspot_counts.items(), key=lambda item: (-item[1], item[0]))
        ][:6]
        latest_commit = max((commit.get("date", "") for commit in commits), default="")
        validation_steps = list(pattern.get("suggested_actions", []))
        if test_files:
            validation_steps.append(f"Re-run related tests: {', '.join(test_files[:3])}")
        if patch_hotspots:
            validation_steps.append(f"Inspect hotspots: {', '.join(patch_hotspots[:3])}")

        return {
            "symptom": pattern["slug"].replace("-", " "),
            "likely_root_cause": pattern["description"],
            "patch_hotspots": patch_hotspots,
            "validation_steps": validation_steps,
            "related_tests": test_files[:6],
            "related_commits": commits[:8],
            "last_evidence_at": latest_commit,
        }

    def _build_content(self, pattern: dict, profile: dict) -> str:
        parts = [f"# {pattern['slug'].replace('-', ' ').title()} Fix Pattern\n"]
        parts.append(f"**Description**: {pattern['description']}")
        parts.append("\n## Symptom\n")
        parts.append(f"- {profile['symptom']}")
        parts.append("\n## Likely Root Cause\n")
        parts.append(f"- {profile['likely_root_cause']}")
        parts.append("\n## Patch Hotspots\n")
        for hotspot in profile["patch_hotspots"] or ["No dominant hotspot observed"]:
            parts.append(f"- {hotspot}")
        parts.append("\n## Validation Steps\n")
        for step in profile["validation_steps"] or ["Reproduce the issue and verify the affected flow end-to-end"]:
            parts.append(f"- {step}")
        parts.append("\n## Related Tests\n")
        for test_name in profile["related_tests"] or ["No direct test file found in sampled commits"]:
            parts.append(f"- {test_name}")
        parts.append("\n## Related Commits\n")
        for commit in profile["related_commits"]:
            parts.append(f"### {commit['repo_id']} {commit['hash'][:8]} ({commit['date']})")
            parts.append(f"- Subject: {commit['subject']}")
            if commit.get("files"):
                parts.append(f"- Files: {', '.join(commit['files'][:6])}")
        if pattern.get("related_cards"):
            parts.append("\n## Related Cards\n")
            for slug in pattern["related_cards"]:
                parts.append(f"- `{slug}`")
        return "\n".join(parts)
