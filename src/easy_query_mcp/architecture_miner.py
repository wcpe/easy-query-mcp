"""Architecture knowledge mining: detect project structure, layering, and design patterns."""

import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

import yaml

from .database import Database
from .knowledge_health import format_commit_map


_DEFAULT_ARCH_PROXIMITY = 40


def _load_arch_patterns() -> list[dict]:
    """Load architecture patterns from config/arch_patterns.yaml."""
    from .config import _find_project_root

    yaml_path = _find_project_root() / "config" / "arch_patterns.yaml"
    if yaml_path.exists():
        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            patterns = []
            for slug, cfg in raw.get("patterns", {}).items():
                kws = cfg.get("keywords", [])
                indicators = cfg.get("indicators", [])
                if not kws:
                    continue
                patterns.append({
                    "slug": slug,
                    "category": cfg.get("category", "architecture"),
                    "confidence": float(cfg.get("confidence", 0.5)),
                    "description": cfg.get("description", ""),
                    "keyword_re": re.compile("|".join(kws), re.IGNORECASE),
                    "indicator_re": re.compile("|".join(indicators), re.IGNORECASE) if indicators else None,
                    "proximity": int(cfg.get("proximity", _DEFAULT_ARCH_PROXIMITY)),
                })
            return patterns
        except Exception as e:
            print(f"[WARN] Failed to load arch_patterns.yaml: {e}", file=sys.stderr)
    return []


def _find_matching_lines(regex: re.Pattern, lines: list[str]) -> list[int]:
    """Return 0-based line indices where *regex* matches."""
    return [i for i, line in enumerate(lines) if regex.search(line)]


class ArchitectureMiner:
    """Mines architecture patterns from indexed source code."""

    def __init__(self, db: Database):
        self.db = db
        self.patterns = _load_arch_patterns()

    def mine_architecture(self, repo_id: Optional[str] = None) -> dict:
        """Scan code for architecture patterns and generate cards."""
        stats = {"patterns_loaded": len(self.patterns), "cards_generated": 0,
                 "relations_added": 0}

        if not self.patterns:
            print("[ARCH-MINE] No architecture patterns loaded", file=sys.stderr)
            return stats

        print(f"[ARCH-MINE] Loaded {len(self.patterns)} architecture patterns",
              file=sys.stderr)

        repo_heads = self.db.snapshot_repo_heads()
        evidence = self.collect_pattern_evidence(repo_id=repo_id)

        # Generate candidate cards from evidence
        for p in self.patterns:
            slug = p["slug"]
            if slug not in evidence:
                continue

            repo_evidence = evidence[slug]
            total_files = sum(len(v["files"]) for v in repo_evidence.values())
            total_matches = sum(v["match_count"] for v in repo_evidence.values())
            total_repos = len(repo_evidence)

            if total_files < 2:
                continue  # Skip patterns with too little evidence

            # Calculate confidence
            base = p["confidence"]
            file_boost = min(total_files / 100.0, 0.15)
            repo_boost = min(total_repos * 0.05, 0.1)
            confidence = min(base + file_boost + repo_boost, 1.0)

            # Collect all symbols and files
            all_symbols = set()
            all_files = set()
            all_snippets = []
            for repo, ev in repo_evidence.items():
                all_symbols |= ev["symbols"]
                all_files |= {f"{repo}:{f}" for f in ev["files"]}
                all_snippets.extend(ev["snippets"])

            card = self._build_arch_card(
                slug=slug,
                description=p["description"],
                confidence=confidence,
                repos=sorted(repo_evidence.keys()),
                total_files=total_files,
                total_matches=total_matches,
                symbols=sorted(list(all_symbols))[:30],
                files=sorted(list(all_files))[:20],
                snippets=all_snippets[:5],
                repo_heads=repo_heads,
            )
            self.db.upsert_candidate(card)
            stats["cards_generated"] += 1

            # Create relations to related concept cards
            relations = self._infer_relations(slug)
            for target, rtype, weight in relations:
                self.db.upsert_relation(f"arch-{slug}", target, rtype, weight,
                                        f"architecture pattern '{slug}'")
                stats["relations_added"] += 1

        print(f"[ARCH-MINE] Generated {stats['cards_generated']} architecture cards",
              file=sys.stderr)
        return stats

    def collect_pattern_evidence(self, repo_id: Optional[str] = None) -> dict[str, dict[str, dict]]:
        """Collect raw architecture evidence without persisting cards."""
        conn = self.db.connect()
        sql = """SELECT repo_id, rel_path, language, content
                 FROM files WHERE language IN ('kotlin', 'java') AND content != ''"""
        params: list = []
        if repo_id:
            sql += " AND repo_id = ?"
            params.append(repo_id)

        rows = conn.execute(sql, params).fetchall()
        print(f"[ARCH-MINE] Scanning {len(rows)} code files...", file=sys.stderr)

        evidence: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(
            lambda: {"files": set(), "symbols": set(), "snippets": [], "match_count": 0}
        ))

        for row in rows:
            r = dict(row)
            content = r["content"]
            repo = r["repo_id"]
            path = r["rel_path"]
            lines = content.split("\n")

            for p in self.patterns:
                # Quick whole-file pre-filter
                kw_matches = p["keyword_re"].findall(content)
                if not kw_matches:
                    continue

                if p["slug"] == "service-layer" and not re.search(
                    r"(?:class|interface|object)\s+\w*Service\b", content, re.IGNORECASE
                ):
                    continue
                if p["slug"] == "api-impl-separation" and "Impl" not in content:
                    continue

                # Line-level proximity check when indicators are defined
                proximity = p["proximity"]
                kw_line_indices = _find_matching_lines(p["keyword_re"], lines)

                if p["indicator_re"]:
                    ind_line_indices = _find_matching_lines(p["indicator_re"], lines)
                    # Only count matches where keyword and indicator are nearby
                    paired_count = 0
                    for ind_ln in ind_line_indices:
                        if any(abs(ind_ln - kw_ln) <= proximity for kw_ln in kw_line_indices):
                            paired_count += 1
                    if paired_count == 0 and ind_line_indices:
                        continue  # indicators exist but none are near keywords
                    effective_count = len(kw_line_indices) + paired_count
                else:
                    effective_count = len(kw_line_indices)

                ev = evidence[p["slug"]][repo]
                ev["files"].add(path)
                ev["match_count"] += effective_count

                sym_re = re.compile(r"(?:class|interface|object|fun)\s+(\w+)", re.MULTILINE)
                for match in sym_re.finditer(content):
                    ev["symbols"].add(match.group(1))

                if len(ev["snippets"]) < 3:
                    for i in kw_line_indices:
                        start = max(0, i - 1)
                        end = min(len(lines), i + 4)
                        snippet = "\n".join(lines[start:end])
                        ev["snippets"].append({
                            "file": f"{repo}:{path}",
                            "line": i + 1,
                            "text": snippet[:300],
                        })
                        break

        return evidence

    def _build_arch_card(self, slug: str, description: str,
                         confidence: float, repos: list[str],
                         total_files: int, total_matches: int,
                         symbols: list[str], files: list[str],
                         snippets: list[dict],
                         repo_heads: dict[str, str]) -> dict:
        """Build a candidate architecture knowledge card."""
        title = slug.replace("-", " ").title() + " Pattern"

        content_parts = [f"# {title}\n"]
        content_parts.append(f"**Description**: {description}")
        content_parts.append(f"**Evidence**: {total_matches} matches in {total_files} files "
                             f"across {len(repos)} repos\n")

        if snippets:
            content_parts.append("## Code Examples\n")
            for s in snippets[:3]:
                content_parts.append(f"### {s['file']} (L{s['line']})")
                content_parts.append(f"```\n{s['text']}\n```\n")

        if symbols:
            content_parts.append("## Key Symbols\n")
            content_parts.append(", ".join(symbols[:20]))

        if files:
            content_parts.append("\n## Key Files\n")
            for f in files[:10]:
                content_parts.append(f"- `{f}`")

        return {
            "card_id": f"gen-arch-{slug}",
            "title": title,
            "slug": f"arch-{slug}",
            "category": "architecture",
            "summary": f"Architecture pattern: {description}. "
                       f"Found in {total_files} files across {len(repos)} repos.",
            "content": "\n".join(content_parts),
            "tags": f"architecture, pattern, {slug.replace('-', ', ')}, "
                    + ", ".join(repos),
            "keywords": ", ".join(symbols[:15]),
            "aliases": slug.replace("-", " "),
            "related_symbols": ", ".join(symbols[:20]),
            "related_docs": "",
            "related_examples": "",
            "source_type": "generated",
            "confidence": round(confidence, 3),
            "status": "candidate",
            "evidence": f"files={total_files}, matches={total_matches}, repos={repos}",
            "last_seen_commit": format_commit_map(repos, repo_heads),
            "stale_score": 0.0,
            "evidence_count": total_matches,
        }

    def _infer_relations(self, slug: str) -> list[tuple[str, str, float]]:
        """Infer relations between architecture patterns and existing cards."""
        ARCH_RELATIONS = {
            "service-layer": [
                ("arch-repository-pattern", "related", 0.8),
            ],
            "repository-pattern": [
                ("easyquery-transaction", "related", 0.72),
                ("h2-concurrency-testing", "related", 0.68),
            ],
            "transaction-boundary": [
                ("easyquery-transaction", "extends", 0.9),
                ("h2-concurrency-testing", "related", 0.72),
            ],
            "entity-configuration": [
                ("easyquery-transaction", "related", 0.65),
            ],
            "query-builder-flow": [
                ("easyquery-transaction", "related", 0.62),
            ],
            "api-impl-separation": [
                ("arch-repository-pattern", "related", 0.6),
            ],
            "code-first-bootstrap": [
                ("easyquery-transaction", "related", 0.58),
            ],
        }

        relations = []
        for target, rtype, weight in ARCH_RELATIONS.get(slug, []):
            # Only add if target card exists
            card = self.db.get_knowledge_card(target)
            candidate = self.db.get_candidate(target)
            if card or candidate:
                relations.append((target, rtype, weight))
        return relations
