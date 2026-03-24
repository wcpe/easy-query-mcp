"""Error knowledge mining: log parsing, pattern matching, troubleshooting card generation."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

import yaml

from .database import Database


def _load_error_patterns() -> list[dict]:
    """Load error patterns from config/error_patterns.yaml."""
    from .config import _find_project_root

    yaml_path = _find_project_root() / "config" / "error_patterns.yaml"
    if yaml_path.exists():
        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            patterns = []
            for slug, cfg in raw.get("patterns", {}).items():
                regex = cfg.get("regex", "")
                if not regex:
                    continue
                patterns.append({
                    "slug": slug,
                    "regex": re.compile(regex, re.IGNORECASE),
                    "category": cfg.get("category", "troubleshooting"),
                    "severity": cfg.get("severity", "medium"),
                    "aliases": cfg.get("aliases", []),
                    "applicable_versions": cfg.get("applicable_versions", []),
                    "probable_causes": cfg.get("probable_causes", []),
                    "related_cards": cfg.get("related_cards", []),
                    "related_symbols": cfg.get("related_symbols", []),
                    "suggested_checks": cfg.get("suggested_checks", []),
                })
            return patterns
        except Exception as e:
            print(f"[WARN] Failed to load error_patterns.yaml: {e}", file=sys.stderr)
    return []


class ErrorMiner:
    """Mines error patterns from code and generates troubleshooting cards."""

    def __init__(self, db: Database):
        self.db = db
        self.patterns = _load_error_patterns()

    def mine_error_knowledge(self) -> dict:
        """Scan code for error handling patterns and generate troubleshooting cards."""
        stats = {"patterns_loaded": len(self.patterns), "cards_generated": 0,
                 "relations_added": 0}

        if not self.patterns:
            print("[ERROR-MINE] No error patterns loaded", file=sys.stderr)
            return stats

        print(f"[ERROR-MINE] Loaded {len(self.patterns)} error patterns", file=sys.stderr)

        # Generate a troubleshooting card for each pattern
        for p in self.patterns:
            card = self._pattern_to_card(p)
            self.db.upsert_candidate(card)
            stats["cards_generated"] += 1

            # Create relations to related cards
            for related_slug in p.get("related_cards", []):
                self.db.upsert_relation(
                    f"error-{p['slug']}", related_slug,
                    "troubleshoots", 0.8,
                    f"error pattern '{p['slug']}' relates to '{related_slug}'"
                )
                stats["relations_added"] += 1

        print(f"[ERROR-MINE] Generated {stats['cards_generated']} troubleshooting cards",
              file=sys.stderr)
        return stats

    def _pattern_to_card(self, pattern: dict) -> dict:
        """Convert an error pattern to a candidate troubleshooting card."""
        slug = f"error-{pattern['slug']}"
        title = pattern["slug"].replace("-", " ").title() + " Troubleshooting"

        causes = "\n".join(f"- {c}" for c in pattern.get("probable_causes", []))
        checks = "\n".join(f"- {c}" for c in pattern.get("suggested_checks", []))
        symbols = ", ".join(pattern.get("related_symbols", []))
        related = ", ".join(pattern.get("related_cards", []))

        content = f"""# {title}

**Severity**: {pattern.get('severity', 'medium')}
**Pattern**: `{pattern['regex'].pattern}`

## Probable Causes

{causes}

## Suggested Checks

{checks}

## Related Knowledge Cards

{', '.join(f'`{c}`' for c in pattern.get('related_cards', [])) or 'None'}

## Related Symbols

{symbols or 'None'}
"""

        return {
            "card_id": f"gen-{slug}",
            "title": title,
            "slug": slug,
            "category": "troubleshooting",
            "summary": f"Troubleshooting guide for: {pattern['slug'].replace('-', ' ')}. "
                       f"Severity: {pattern.get('severity', 'medium')}. "
                       f"{len(pattern.get('probable_causes', []))} known causes.",
            "content": content,
            "tags": f"troubleshooting, error, {pattern.get('severity', 'medium')}, "
                    f"{pattern.get('category', '')}",
            "keywords": f"{pattern['slug'].replace('-', ', ')}, error, exception, "
                        f"{', '.join(pattern.get('related_symbols', []))}",
            "aliases": ", ".join(pattern.get("aliases", [])),
            "related_symbols": symbols,
            "related_docs": "",
            "related_examples": "",
            "source_type": "generated",
            "confidence": 0.85,
            "status": "candidate",
            "evidence": f"error_pattern: {pattern['slug']}, "
                        f"regex: {pattern['regex'].pattern[:100]}",
            "applicable_versions": ", ".join(pattern.get("applicable_versions", [])),
            "evidence_count": len(pattern.get("probable_causes", [])),
        }


def diagnose_from_log(db: Database, log_text: str,
                      repo_hint: Optional[str] = None) -> dict:
    """Analyze a log/error text and return diagnosis with related knowledge.

    Args:
        db: Database instance
        log_text: Error log, stack trace, or exception text
        repo_hint: Optional repo to focus search on

    Returns:
        Structured diagnosis with matches, causes, cards, and next steps.
    """
    patterns = _load_error_patterns()
    if not patterns:
        return {"status": "error", "message": "No error patterns loaded"}

    # Extract exception blocks from log
    blocks = _extract_exception_blocks(log_text)
    full_text = log_text if not blocks else "\n".join(blocks)

    # Match patterns
    matches = []
    for p in patterns:
        if p["regex"].search(full_text):
            matches.append(p)

    if not matches:
        # Try broader search on individual lines
        for line in log_text.split("\n"):
            for p in patterns:
                if p["regex"].search(line) and p not in matches:
                    matches.append(p)

    # Build diagnosis
    matched_patterns = []
    all_related_cards = set()
    all_related_symbols = set()
    all_causes = []
    all_checks = []
    relation_evidence = []
    stack_frames = _extract_stack_frames(log_text)

    for m in matches:
        matched_patterns.append({
            "pattern": m["slug"],
            "severity": m["severity"],
            "probable_causes": m["probable_causes"],
            "suggested_checks": m["suggested_checks"],
        })
        all_causes.extend(m["probable_causes"])
        all_checks.extend(m["suggested_checks"])
        for c in m.get("related_cards", []):
            all_related_cards.add(c)
        for s in m.get("related_symbols", []):
            all_related_symbols.add(s)
        pattern_card_slug = f"error-{m['slug']}"
        for relation in db.get_related_cards(pattern_card_slug, limit=10):
            target = relation["target_slug"] if relation["source_slug"] == pattern_card_slug else relation["source_slug"]
            if target != pattern_card_slug:
                all_related_cards.add(target)
                relation_evidence.append({
                    "source_slug": relation["source_slug"],
                    "target_slug": relation["target_slug"],
                    "relation_type": relation["relation_type"],
                    "weight": relation["weight"],
                })

    # Fetch related knowledge cards
    related_card_details = []
    proven_fix_patterns = []
    related_anti_patterns = []
    for slug in all_related_cards:
        card = db.get_knowledge_card(slug) or db.get_candidate(slug)
        if card:
            card_summary = {
                "slug": card["slug"],
                "title": card["title"],
                "summary": card.get("summary", "")[:200],
            }
            category = (card.get("category", "") or "").lower()
            if slug.startswith("fix-") or category == "fix-pattern":
                proven_fix_patterns.append(_format_fix_pattern(card))
            elif slug.startswith("anti-") or category == "anti-pattern":
                related_anti_patterns.append(card_summary)
            else:
                related_card_details.append(card_summary)

    # Search for related source files
    related_files = []
    if all_related_symbols:
        for sym in list(all_related_symbols)[:5]:
            results = db.search_symbols(sym, repo_id=repo_hint, limit=3)
            for r in results:
                related_files.append({
                    "symbol": r["name"],
                    "file": f"{r['repo_id']}:{r['rel_path']}",
                    "line": r.get("line_number", 0),
                })

    # Extract class/method names from stack trace
    stack_classes = _extract_stack_classes(log_text)
    root_cause = _locate_root_cause(db, stack_frames, repo_hint=repo_hint)
    if root_cause:
        related_files.insert(0, {
            "symbol": root_cause.get("symbol", ""),
            "file": f"{root_cause.get('repo_id', '')}:{root_cause.get('rel_path', '')}",
            "line": root_cause.get("line_number", 0),
        })

    recommended_tests = _recommend_tests(root_cause, proven_fix_patterns)
    recommended_files = _recommend_files(related_files, proven_fix_patterns)
    likely_owner_module = _infer_owner_module(root_cause)

    return {
        "status": "ok" if matches else "no_match",
        "matched_patterns": matched_patterns,
        "severity": max((m["severity"] for m in matches),
                        key=lambda s: {"critical": 4, "high": 3, "medium": 2, "low": 1}.get(s, 0),
                        default="unknown") if matches else "unknown",
        "probable_causes": list(dict.fromkeys(all_causes)),  # dedupe preserving order
        "suggested_checks": list(dict.fromkeys(all_checks)),
        "related_knowledge_cards": related_card_details,
        "proven_fix_patterns": proven_fix_patterns,
        "related_source_files": related_files[:10],
        "stack_frames": stack_frames[:12],
        "stack_classes": stack_classes[:10],
        "root_cause_location": root_cause,
        "repair_path": {
            "first_relevant_frame": root_cause or (stack_frames[0] if stack_frames else None),
            "likely_owner_module": likely_owner_module,
            "related_anti_patterns": related_anti_patterns,
            "proven_fix_patterns": proven_fix_patterns,
            "recommended_files_to_inspect": recommended_files,
            "recommended_tests_to_run": recommended_tests,
        },
        "evidence": {
            "graph_relations": relation_evidence[:12],
            "matched_error_patterns": [item["pattern"] for item in matched_patterns],
            "stack_depth_considered": min(len(stack_frames), 20),
        },
        "next_steps": _build_next_steps(matches, all_related_cards, root_cause),
    }


def _extract_exception_blocks(text: str) -> list[str]:
    """Extract exception/error blocks from log text."""
    blocks = []
    lines = text.split("\n")
    current_block = []
    in_block = False

    for line in lines:
        if re.search(r"Exception|Error|Caused by|at\s+\w+\.\w+", line):
            if not in_block:
                in_block = True
                current_block = []
            current_block.append(line)
        elif in_block:
            if line.strip().startswith("at ") or line.strip().startswith("..."):
                current_block.append(line)
            else:
                if current_block:
                    blocks.append("\n".join(current_block))
                current_block = []
                in_block = False

    if current_block:
        blocks.append("\n".join(current_block))

    return blocks


def _extract_stack_classes(text: str) -> list[str]:
    """Extract class names from stack trace lines."""
    classes = []
    seen = set()
    for m in re.finditer(r"at\s+([\w.]+)\.([\w$]+)\(", text):
        cls = m.group(1).split(".")[-1]
        if cls not in seen and not cls.startswith("$"):
            seen.add(cls)
            classes.append(cls)
    return classes


def _extract_stack_frames(text: str) -> list[dict]:
    """Parse structured stack frames out of a Java/Kotlin stack trace."""
    frames = []
    seen = set()
    for match in re.finditer(
        r"at\s+([\w.$]+)\.([\w$<>]+)\(([^:()]+)(?::(\d+))?\)",
        text,
    ):
        class_name = match.group(1)
        method_name = match.group(2)
        file_name = match.group(3)
        line_number = int(match.group(4)) if match.group(4) else 0
        if file_name in {"Native Method", "Unknown Source"}:
            continue
        key = (class_name, method_name, file_name, line_number)
        if key in seen:
            continue
        seen.add(key)
        frames.append({
            "class_name": class_name,
            "method_name": method_name,
            "file_name": file_name,
            "line_number": line_number,
        })
    return frames


def _is_framework_repo(repo: dict) -> bool:
    tags = {tag.strip().lower() for tag in (repo.get("tags", "") or "").split(",") if tag.strip()}
    return "framework" in tags or repo.get("id") in {"easyquery", "easyquery-docs"}


def _locate_root_cause(db: Database, frames: list[dict],
                       repo_hint: Optional[str] = None) -> Optional[dict]:
    """Map stack frames back to indexed symbols and prefer business-layer code."""
    if not frames:
        return None

    repo_meta = {repo["id"]: repo for repo in db.get_all_repos()}
    best: Optional[dict] = None
    best_score = float("-inf")

    for depth, frame in enumerate(frames[:20]):
        simple_name = frame["class_name"].split(".")[-1].split("$")[0]
        candidates = db.search_symbols(simple_name, repo_id=repo_hint, limit=8)
        if not candidates and repo_hint is None:
            candidates = db.search_symbols(simple_name, limit=8)

        for candidate in candidates:
            score = 0.0
            if candidate["name"] == simple_name:
                score += 3.0
            if candidate["qualified_name"].endswith(frame["class_name"]):
                score += 4.0
            if Path(candidate["rel_path"]).name == frame["file_name"]:
                score += 4.0
            if frame["line_number"] and candidate["line_number"] <= frame["line_number"] <= max(candidate["end_line"], candidate["line_number"]):
                score += 3.5
            repo = repo_meta.get(candidate["repo_id"], {})
            score += 2.0 if not _is_framework_repo(repo) else -0.5
            if repo_hint and candidate["repo_id"] == repo_hint:
                score += 1.5
            score -= depth * 0.15

            if score > best_score:
                best_score = score
                best = {
                    "class_name": frame["class_name"],
                    "method_name": frame["method_name"],
                    "file_name": frame["file_name"],
                    "line_number": candidate["line_number"] or frame["line_number"],
                    "repo_id": candidate["repo_id"],
                    "rel_path": candidate["rel_path"],
                    "symbol": candidate["name"],
                    "qualified_name": candidate["qualified_name"],
                    "confidence": round(min(1.0, max(score / 10.0, 0.1)), 3),
                }

    return best


def _build_next_steps(matches: list[dict], related_cards: set[str],
                      root_cause: Optional[dict]) -> list[str]:
    """Build prioritized next steps."""
    steps = []
    if root_cause:
        steps.append(
            "read_source("
            f"repo_id='{root_cause['repo_id']}', "
            f"path='{root_cause['rel_path']}', "
            f"start_line={max(1, root_cause['line_number'] - 20)}, "
            f"end_line={root_cause['line_number'] + 40})"
        )
    if related_cards:
        for slug in sorted(related_cards):
            steps.append(f"read_knowledge_card(slug='{slug}')")
    if matches:
        steps.append("Check the suggested_checks for each matched pattern")
        steps.append("Use read_source to examine the files mentioned in the stack trace")
    if not matches:
        steps.append("Try search_by_concept with keywords from the error message")
        steps.append("Use search_symbol to find related classes")
    return steps


def _extract_section(content: str, heading: str) -> list[str]:
    match = re.search(
        rf"##\s+{re.escape(heading)}\s*(.*?)(?:\n##\s+|\Z)",
        content,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return []
    section = match.group(1).strip()
    lines = []
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if line.startswith("- "):
            lines.append(line[2:].strip())
        elif line and not line.startswith("#"):
            lines.append(line)
    return lines


def _extract_commit_lines(content: str) -> list[str]:
    commits = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith("### "):
            commits.append(line[4:].strip())
    return commits[:8]


def _format_fix_pattern(card: dict) -> dict:
    content = card.get("content", "") or ""
    patch_hotspots = _extract_section(content, "Patch Hotspots")
    validation_steps = _extract_section(content, "Validation Steps")
    related_tests = _extract_section(content, "Related Tests")
    related_commits = _extract_commit_lines(content)
    symptom = _extract_section(content, "Symptom")
    root_causes = _extract_section(content, "Likely Root Cause")
    return {
        "slug": card["slug"],
        "title": card["title"],
        "summary": card.get("summary", "")[:220],
        "symptom": symptom[0] if symptom else card.get("summary", ""),
        "likely_root_cause": root_causes[0] if root_causes else card.get("summary", ""),
        "patch_hotspots": patch_hotspots[:5],
        "validation_steps": validation_steps[:5],
        "related_tests": related_tests[:5],
        "related_commits": related_commits,
        "evidence_count": card.get("evidence_count", 0),
    }


def _infer_owner_module(root_cause: Optional[dict]) -> str:
    if not root_cause:
        return ""
    rel_path = root_cause.get("rel_path", "") or ""
    repo_id = root_cause.get("repo_id", "") or ""
    parts = list(Path(rel_path).parts)
    if "src" in parts:
        for marker in ("kotlin", "java"):
            if marker in parts:
                idx = parts.index(marker)
                module_parts = parts[idx + 1: idx + 4]
                if module_parts:
                    return f"{repo_id}:{'.'.join(module_parts)}"
    if len(parts) >= 2:
        return f"{repo_id}:{parts[-2]}"
    return repo_id


def _recommend_files(related_files: list[dict], fix_patterns: list[dict]) -> list[str]:
    recommended = []
    seen = set()
    for item in related_files:
        file_ref = item.get("file", "")
        if file_ref and file_ref not in seen:
            seen.add(file_ref)
            recommended.append(file_ref)
    for pattern in fix_patterns:
        for hotspot in pattern.get("patch_hotspots", []):
            hint = f"hotspot:{hotspot}"
            if hotspot and hint not in seen:
                seen.add(hint)
                recommended.append(hint)
    return recommended[:8]


def _recommend_tests(root_cause: Optional[dict], fix_patterns: list[dict]) -> list[str]:
    recommended = []
    seen = set()
    for pattern in fix_patterns:
        for test_name in pattern.get("related_tests", []):
            if test_name and test_name not in seen:
                seen.add(test_name)
                recommended.append(test_name)
    symbol = root_cause.get("symbol", "") if root_cause else ""
    for candidate in [f"{symbol}Test", f"{symbol}IntegrationTest"] if symbol else []:
        if candidate not in seen:
            seen.add(candidate)
            recommended.append(candidate)
    return recommended[:8]
