"""Multi-source indexing pipeline with source-aware strategies."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .config import SourceConfig, SourceRegistry, load_source_registry
from .database import Database
from .indexer import (
    _compute_end_lines,
    _parse_knowledge_card,
    build_feature_tags,
    classify_content_domain,
    detect_language,
    extract_doc_title,
    extract_canonical_topics,
    extract_symbols_java,
    extract_symbols_kotlin,
    generate_doc_tags,
    infer_stability,
    infer_visibility,
    is_example_path,
)


CODE_EXTENSIONS = {".kt", ".kts", ".java"}
DOC_EXTENSIONS = {".md", ".mdx", ".adoc", ".txt"}
KNOWLEDGE_EXTENSIONS = {".md", ".mdx", ".yaml", ".yml"}
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_to_epoch(text: str) -> float:
    if not text:
        return 0.0
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _git_head(path: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _git_changed_entries(path: Path, base_rev: str) -> list[GitChange]:
    try:
        result = subprocess.run(
            ["git", "diff", "--name-status", base_rev, "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    changes: list[GitChange] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split("\t") if part.strip()]
        if not parts:
            continue
        status = parts[0].upper()
        if status.startswith(("R", "C")) and len(parts) >= 3:
            old_path = parts[1].replace("\\", "/")
            new_path = parts[2].replace("\\", "/")
            changes.append(GitChange(status=status, path=new_path, old_path=old_path))
        elif len(parts) >= 2:
            rel_path = parts[1].replace("\\", "/")
            changes.append(GitChange(status=status, path=rel_path))
    return changes


def _walk_files(root: Path, skip_dirs: set[str]) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [item for item in dirnames if item not in skip_dirs]
        for filename in filenames:
            yield Path(dirpath) / filename


def _extract_doc_metadata(content: str) -> dict:
    headings = [match.group(2).strip() for match in HEADING_RE.finditer(content)]
    links = [target.strip() for _, target in LINK_RE.findall(content)]
    summary_lines: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("---"):
            continue
        summary_lines.append(stripped)
        if len(" ".join(summary_lines)) >= 240:
            break
    summary = " ".join(summary_lines)[:280]
    return {
        "headings": headings,
        "links": links,
        "summary": summary,
    }


@dataclass
class SourceIndexStats:
    indexed_sources: int = 0
    indexed_files: int = 0
    indexed_symbols: int = 0
    indexed_examples: int = 0
    indexed_docs: int = 0
    indexed_cards: int = 0
    skipped_sources: int = 0

    def as_dict(self) -> dict:
        return {
            "sources": self.indexed_sources,
            "files": self.indexed_files,
            "symbols": self.indexed_symbols,
            "examples": self.indexed_examples,
            "docs": self.indexed_docs,
            "cards": self.indexed_cards,
            "skipped_sources": self.skipped_sources,
        }


@dataclass(frozen=True)
class GitChange:
    """A single git name-status change entry."""

    status: str
    path: str
    old_path: str = ""

    @property
    def kind(self) -> str:
        return (self.status[:1] or "").upper()


class IndexPipeline:
    """Index code, docs, wiki, and knowledge sources from the source registry."""

    def __init__(self, registry: SourceRegistry, db: Database):
        self.registry = registry
        self.db = db
        self.base_dir = Path(registry.config_path).resolve().parent if registry.config_path else Path.cwd()

    def index(self, source_id: Optional[str] = None,
              changed_only: bool = False) -> dict:
        self.db.initialize()
        stats = SourceIndexStats()
        sources = self.registry.enabled_sources()
        if source_id:
            sources = [source for source in sources if source.id == source_id]

        for source in sources:
            try:
                self._record_source(source)
                self._index_source(source, stats, changed_only=changed_only)
                stats.indexed_sources += 1
            except (FileNotFoundError, NotADirectoryError) as exc:
                print(f"[SKIP] {source.id}: {exc}", file=sys.stderr)
                stats.skipped_sources += 1
        return stats.as_dict()

    def _record_source(self, source: SourceConfig) -> None:
        self.db.upsert_source(
            source.id,
            source.name,
            str(source.resolved_path(self.base_dir)),
            ",".join(source.tags),
            source_type=source.type,
            git_url=source.git_url,
            linked_source_id=source.linked_source_id,
            enabled=source.enabled,
        )

    def _index_source(self, source: SourceConfig,
                      stats: SourceIndexStats,
                      changed_only: bool = False) -> None:
        root = source.resolved_path(self.base_dir)
        if not root.exists():
            raise FileNotFoundError(f"Source path does not exist: {root}")
        if not root.is_dir():
            raise NotADirectoryError(f"Source path is not a directory: {root}")

        existing = self.db.get_source(source.id) or {}
        current_head = _git_head(root)
        build_time = _utc_now()
        changed_entries = self._changed_entries(source, root, existing, changed_only)
        self._log_changed_entries(source, changed_entries)

        if source.type == "code":
            self._index_code_source(source, root, stats, changed_entries, changed_only, current_head, build_time)
        elif source.type in {"docs", "wiki"}:
            self._index_doc_source(source, root, stats, changed_entries, changed_only, current_head, build_time)
        elif source.type == "knowledge":
            self._index_knowledge_source(source, root, stats, changed_entries, changed_only, current_head, build_time)
        else:
            raise ValueError(f"Unsupported source type: {source.type}")

        self.db.update_source_index_state(source.id, indexed_at=_utc_now(), indexed_rev=current_head)

    def _changed_entries(self, source: SourceConfig, root: Path,
                         existing: dict, changed_only: bool) -> Optional[list[GitChange]]:
        if not changed_only:
            return None

        last_indexed_rev = str(existing.get("last_indexed_rev", "") or "").strip()
        if last_indexed_rev:
            changes = _git_changed_entries(root, last_indexed_rev)
            if changes:
                return changes

        last_indexed_at = str(existing.get("last_indexed_at", "") or "").strip()
        threshold = _iso_to_epoch(last_indexed_at)
        if threshold <= 0:
            return None

        changed: list[GitChange] = []
        for file_path in _walk_files(root, set(self.registry.skip_dirs)):
            try:
                if file_path.stat().st_mtime > threshold:
                    changed.append(GitChange("M", str(file_path.relative_to(root)).replace("\\", "/")))
            except FileNotFoundError:
                continue
        return changed or []

    def _iter_target_paths(self, root: Path, changed_entries: Optional[list[GitChange]],
                           allowed_exts: set[str]) -> list[Path]:
        if changed_entries is None:
            return [
                path for path in _walk_files(root, set(self.registry.skip_dirs))
                if path.suffix.lower() in allowed_exts
            ]
        results: list[Path] = []
        for change in changed_entries:
            if change.kind == "D":
                continue
            full_path = root / change.path
            if full_path.is_file() and full_path.suffix.lower() in allowed_exts:
                results.append(full_path)
        return results

    def _cleanup_paths(self, changed_entries: Optional[list[GitChange]]) -> list[str]:
        if not changed_entries:
            return []
        cleanup: list[str] = []
        seen = set()
        for change in changed_entries:
            if change.kind == "C":
                continue
            rel_path = change.old_path if change.kind == "R" and change.old_path else change.path
            rel_path = rel_path.replace("\\", "/")
            if rel_path and rel_path not in seen:
                seen.add(rel_path)
                cleanup.append(rel_path)
        return cleanup

    def _log_changed_entries(self, source: SourceConfig, changed_entries: Optional[list[GitChange]]) -> None:
        if changed_entries is None:
            return
        counts: dict[str, int] = {"A": 0, "M": 0, "D": 0, "R": 0, "C": 0, "?": 0}
        samples: list[str] = []
        for change in changed_entries:
            kind = change.kind or "?"
            counts[kind] = counts.get(kind, 0) + 1
            if change.old_path and kind in {"R", "C"}:
                samples.append(f"{kind}:{change.old_path} -> {change.path}")
            else:
                samples.append(f"{kind}:{change.path}")
        summary = ", ".join(f"{kind}={count}" for kind, count in counts.items() if count)
        if not summary:
            summary = "no_changes"
        if samples:
            summary = f"{summary} | {', '.join(samples[:5])}"
        print(f"[DIFF] {source.id}: {summary}", file=sys.stderr)

    def _index_code_source(self, source: SourceConfig, root: Path,
                           stats: SourceIndexStats,
                           changed_entries: Optional[list[GitChange]],
                           changed_only: bool,
                           build_commit: str,
                           build_time: str) -> None:
        if changed_entries is None:
            self.db.clear_source_files(source.id)
        elif changed_entries:
            cleanup_paths = self._cleanup_paths(changed_entries)
            if cleanup_paths:
                self.db.clear_source_files(source.id, cleanup_paths)
        elif changed_only:
            return

        for full_path in self._iter_target_paths(root, changed_entries, CODE_EXTENSIONS):
            rel_path = str(full_path.relative_to(root)).replace("\\", "/")
            try:
                content = full_path.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                print(f"[WARN] Cannot read {source.id}:{rel_path}: {exc}", file=sys.stderr)
                continue

            language = detect_language(full_path.name)
            is_example = is_example_path(rel_path)
            content_domain = classify_content_domain(
                rel_path,
                source.type,
                content=content,
                is_example=is_example,
            )
            visibility = infer_visibility(
                rel_path,
                source_type=source.type,
                content_domain=content_domain,
            )
            stability = infer_stability(
                rel_path,
                content=content,
                visibility=visibility,
            )
            feature_tags = build_feature_tags(
                rel_path,
                content=content,
                repo_tags=source.tags,
                content_domain=content_domain,
            )
            file_id = self.db.insert_file(
                source.id,
                rel_path,
                language,
                content,
                is_example,
                content_domain=content_domain,
                visibility=visibility,
                stability=stability,
                feature_tags=",".join(feature_tags),
                build_commit=build_commit,
                build_time=build_time,
            )
            stats.indexed_files += 1

            if language == "kotlin":
                symbols = extract_symbols_kotlin(content, rel_path)
            else:
                symbols = extract_symbols_java(content, rel_path)

            symbols = _compute_end_lines(symbols, content.count("\n") + 1)
            for symbol in symbols:
                symbol_domain = "api" if content_domain == "source" and symbol["kind"] in {"class", "interface", "annotation", "enum", "object"} else content_domain
                symbol_visibility = infer_visibility(
                    rel_path,
                    signature=symbol["signature"],
                    source_type=source.type,
                    content_domain=symbol_domain,
                )
                symbol_stability = infer_stability(
                    rel_path,
                    signature=symbol["signature"],
                    content=content,
                    visibility=symbol_visibility,
                )
                self.db.insert_symbol(
                    source.id,
                    file_id,
                    symbol["name"],
                    symbol["qualified_name"],
                    symbol["kind"],
                    symbol["signature"],
                    symbol["line_number"],
                    symbol["end_line"],
                    content_domain=symbol_domain,
                    visibility=symbol_visibility,
                    stability=symbol_stability,
                    feature_tags=",".join(feature_tags),
                    build_commit=build_commit,
                    build_time=build_time,
                )
                stats.indexed_symbols += 1

            if is_example:
                self.db.insert_example(
                    source.id,
                    file_id,
                    full_path.name,
                    content[:2000],
                    content_domain=content_domain,
                    visibility="public",
                    stability=stability,
                    feature_tags=",".join(feature_tags),
                    build_commit=build_commit,
                    build_time=build_time,
                )
                stats.indexed_examples += 1

    def _index_doc_source(self, source: SourceConfig, root: Path,
                          stats: SourceIndexStats,
                          changed_entries: Optional[list[GitChange]],
                          changed_only: bool,
                          build_commit: str,
                          build_time: str) -> None:
        if changed_entries is None:
            self.db.clear_source_docs(source.id)
        elif changed_entries:
            cleanup_paths = self._cleanup_paths(changed_entries)
            if cleanup_paths:
                self.db.clear_source_docs(source.id, cleanup_paths)
        elif changed_only:
            return

        for full_path in self._iter_target_paths(root, changed_entries, DOC_EXTENSIONS):
            rel_path = str(full_path.relative_to(root)).replace("\\", "/")
            try:
                content = full_path.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                print(f"[WARN] Cannot read {source.id}:{rel_path}: {exc}", file=sys.stderr)
                continue

            metadata = _extract_doc_metadata(content)
            title = extract_doc_title(content, full_path.name)
            tags = generate_doc_tags(rel_path, source.tags, title)
            content_domain = classify_content_domain(rel_path, source.type, title=title, content=content)
            visibility = infer_visibility(rel_path, source_type=source.type, content_domain=content_domain)
            stability = infer_stability(rel_path, title=title, content=content, visibility=visibility)
            feature_tags = build_feature_tags(
                rel_path,
                title=title,
                content=content,
                repo_tags=source.tags,
                content_domain=content_domain,
            )
            self.db.insert_doc(
                source.id,
                rel_path,
                title,
                ",".join(tags),
                content,
                language=detect_language(full_path.name),
                summary=metadata["summary"],
                headings=" | ".join(metadata["headings"]),
                links=" | ".join(metadata["links"]),
                source_type=source.type,
                content_domain=content_domain,
                visibility=visibility,
                stability=stability,
                feature_tags=",".join(feature_tags),
                build_commit=build_commit,
                build_time=build_time,
            )
            stats.indexed_docs += 1

    def _index_knowledge_source(self, source: SourceConfig, root: Path,
                                stats: SourceIndexStats,
                                changed_entries: Optional[list[GitChange]],
                                changed_only: bool,
                                build_commit: str,
                                build_time: str) -> None:
        if changed_entries is None:
            self.db.clear_source_cards(source.id)
        elif changed_entries and not changed_only:
            self.db.clear_source_cards(source.id)
        elif changed_only and not changed_entries:
            return

        if changed_entries:
            cleanup_paths = self._cleanup_paths(changed_entries)
            if cleanup_paths:
                self.db.clear_source_cards_by_path(source.id, cleanup_paths)

        for full_path in self._iter_target_paths(root, changed_entries, KNOWLEDGE_EXTENSIONS):
            rel_path = str(full_path.relative_to(root)).replace("\\", "/")
            try:
                raw = full_path.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                print(f"[WARN] Cannot read {source.id}:{rel_path}: {exc}", file=sys.stderr)
                continue

            card = _parse_knowledge_card(raw, full_path.name)
            if card is None:
                continue
            card["source_id"] = source.id
            card["source_path"] = rel_path
            if card.get("source_type", "manual") == "manual":
                card["source_type"] = "knowledge"
            content_domain = classify_content_domain(
                rel_path,
                source.type,
                title=card.get("title", ""),
                content=card.get("content", ""),
            )
            visibility = infer_visibility(rel_path, source_type=source.type, content_domain=content_domain)
            stability = infer_stability(
                rel_path,
                title=card.get("title", ""),
                content=card.get("content", ""),
                visibility=visibility,
            )
            feature_tags = build_feature_tags(
                rel_path,
                title=card.get("title", ""),
                content=card.get("content", ""),
                repo_tags=source.tags,
                content_domain=content_domain,
            )
            feature_tags.extend(topic for topic in extract_canonical_topics(card.get("tags", ""), card.get("keywords", "")) if topic not in feature_tags)
            card["content_domain"] = content_domain
            card["visibility"] = visibility
            card["stability"] = stability
            card["feature_tags"] = ",".join(feature_tags[:12])
            card["build_commit"] = build_commit
            card["build_time"] = build_time
            self.db.upsert_knowledge_card(card)
            stats.indexed_cards += 1


def build_index_pipeline(config_path: Optional[str] = None,
                         db_path: Optional[str] = None) -> tuple[IndexPipeline, Database]:
    registry = load_source_registry(config_path)
    db = Database(db_path or registry.db_path)
    return IndexPipeline(registry, db), db
