"""SQLite database module with FTS5 support."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .query_expansion import build_fts_or_query, expand_query_terms, score_text_match


DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS repositories (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    tags TEXT DEFAULT '',
    source_type TEXT DEFAULT 'code',
    git_url TEXT DEFAULT '',
    linked_source_id TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    last_synced_at TEXT DEFAULT '',
    last_synced_rev TEXT DEFAULT '',
    last_indexed_at TEXT DEFAULT '',
    last_indexed_rev TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    language TEXT DEFAULT '',
    content TEXT DEFAULT '',
    is_example INTEGER DEFAULT 0,
    content_domain TEXT DEFAULT 'source',
    visibility TEXT DEFAULT 'public',
    stability TEXT DEFAULT 'stable',
    feature_tags TEXT DEFAULT '',
    build_commit TEXT DEFAULT '',
    build_time TEXT DEFAULT '',
    FOREIGN KEY (repo_id) REFERENCES repositories(id),
    UNIQUE(repo_id, rel_path)
);

CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id TEXT NOT NULL,
    file_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT DEFAULT '',
    kind TEXT NOT NULL,
    signature TEXT DEFAULT '',
    line_number INTEGER DEFAULT 0,
    end_line INTEGER DEFAULT 0,
    content_domain TEXT DEFAULT 'api',
    visibility TEXT DEFAULT 'public',
    stability TEXT DEFAULT 'stable',
    feature_tags TEXT DEFAULT '',
    build_commit TEXT DEFAULT '',
    build_time TEXT DEFAULT '',
    FOREIGN KEY (repo_id) REFERENCES repositories(id),
    FOREIGN KEY (file_id) REFERENCES files(id)
);

CREATE TABLE IF NOT EXISTS examples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id TEXT NOT NULL,
    file_id INTEGER NOT NULL,
    title TEXT DEFAULT '',
    snippet TEXT DEFAULT '',
    content_domain TEXT DEFAULT 'example',
    visibility TEXT DEFAULT 'public',
    stability TEXT DEFAULT 'stable',
    feature_tags TEXT DEFAULT '',
    build_commit TEXT DEFAULT '',
    build_time TEXT DEFAULT '',
    FOREIGN KEY (repo_id) REFERENCES repositories(id),
    FOREIGN KEY (file_id) REFERENCES files(id)
);

-- FTS5 virtual tables for full-text search
CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
    name, qualified_name, signature, kind,
    content='symbols',
    content_rowid='id'
);

CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
    rel_path, content,
    content='files',
    content_rowid='id'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS symbols_ai AFTER INSERT ON symbols BEGIN
    INSERT INTO symbols_fts(rowid, name, qualified_name, signature, kind)
    VALUES (new.id, new.name, new.qualified_name, new.signature, new.kind);
END;

CREATE TRIGGER IF NOT EXISTS symbols_ad AFTER DELETE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name, signature, kind)
    VALUES ('delete', old.id, old.name, old.qualified_name, old.signature, old.kind);
END;

CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
    INSERT INTO files_fts(rowid, rel_path, content)
    VALUES (new.id, new.rel_path, new.content);
END;

CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
    INSERT INTO files_fts(files_fts, rowid, rel_path, content)
    VALUES ('delete', old.id, old.rel_path, old.content);
END;

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_repo ON symbols(repo_id);
CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);
CREATE INDEX IF NOT EXISTS idx_files_repo ON files(repo_id);
CREATE INDEX IF NOT EXISTS idx_files_example ON files(is_example);

-- ── Documentation tables ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS docs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id TEXT NOT NULL,
    path TEXT NOT NULL,
    title TEXT DEFAULT '',
    tags TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    headings TEXT DEFAULT '',
    links TEXT DEFAULT '',
    source_type TEXT DEFAULT 'docs',
    content TEXT DEFAULT '',
    language TEXT DEFAULT 'markdown',
    content_domain TEXT DEFAULT 'doc',
    visibility TEXT DEFAULT 'public',
    stability TEXT DEFAULT 'stable',
    feature_tags TEXT DEFAULT '',
    build_commit TEXT DEFAULT '',
    build_time TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (repo_id) REFERENCES repositories(id),
    UNIQUE(repo_id, path)
);

CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    title, tags, content, path,
    content='docs',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
    INSERT INTO docs_fts(rowid, title, tags, content, path)
    VALUES (new.id, new.title, new.tags, new.content, new.path);
END;

CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, tags, content, path)
    VALUES ('delete', old.id, old.title, old.tags, old.content, old.path);
END;

CREATE INDEX IF NOT EXISTS idx_docs_repo ON docs(repo_id);
CREATE INDEX IF NOT EXISTS idx_docs_path ON docs(path);

-- ── Knowledge Card tables ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS knowledge_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    slug TEXT UNIQUE NOT NULL,
    category TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    content TEXT DEFAULT '',
    tags TEXT DEFAULT '',
    keywords TEXT DEFAULT '',
    aliases TEXT DEFAULT '',
    when_to_use TEXT DEFAULT '',
    avoid TEXT DEFAULT '',
    best_practices TEXT DEFAULT '',
    related_symbols TEXT DEFAULT '',
    related_docs TEXT DEFAULT '',
    related_examples TEXT DEFAULT '',
    related_errors TEXT DEFAULT '',
    source_type TEXT DEFAULT 'manual',
    source_id TEXT DEFAULT '',
    source_path TEXT DEFAULT '',
    content_domain TEXT DEFAULT 'knowledge',
    visibility TEXT DEFAULT 'public',
    stability TEXT DEFAULT 'stable',
    feature_tags TEXT DEFAULT '',
    build_commit TEXT DEFAULT '',
    build_time TEXT DEFAULT '',
    priority INTEGER DEFAULT 50,
    owner TEXT DEFAULT '',
    reviewed_by TEXT DEFAULT '',
    review_notes TEXT DEFAULT '',
    change_summary TEXT DEFAULT '',
    evidence_quality TEXT DEFAULT '',
    applicable_versions TEXT DEFAULT '',
    lifecycle_status TEXT DEFAULT 'active',
    last_evidence_at TEXT DEFAULT '',
    last_seen_commit TEXT DEFAULT '',
    stale_score REAL DEFAULT 0.0,
    evidence_count INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE VIRTUAL TABLE IF NOT EXISTS kc_fts USING fts5(
    title, summary, content, tags, keywords, aliases, category,
    content='knowledge_cards',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS kc_ai AFTER INSERT ON knowledge_cards BEGIN
    INSERT INTO kc_fts(rowid, title, summary, content, tags, keywords, aliases, category)
    VALUES (new.id, new.title, new.summary, new.content, new.tags, new.keywords, new.aliases, new.category);
END;

CREATE TRIGGER IF NOT EXISTS kc_ad AFTER DELETE ON knowledge_cards BEGIN
    INSERT INTO kc_fts(kc_fts, rowid, title, summary, content, tags, keywords, aliases, category)
    VALUES ('delete', old.id, old.title, old.summary, old.content, old.tags, old.keywords, old.aliases, old.category);
END;

CREATE INDEX IF NOT EXISTS idx_kc_slug ON knowledge_cards(slug);
CREATE INDEX IF NOT EXISTS idx_kc_category ON knowledge_cards(category);
CREATE INDEX IF NOT EXISTS idx_kc_priority ON knowledge_cards(priority DESC);

-- ── Source Chunks table ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS source_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    chunk_type TEXT NOT NULL,
    title TEXT DEFAULT '',
    content TEXT DEFAULT '',
    language TEXT DEFAULT '',
    start_line INTEGER DEFAULT 0,
    end_line INTEGER DEFAULT 0,
    symbols TEXT DEFAULT '',
    tags TEXT DEFAULT '',
    content_domain TEXT DEFAULT 'source',
    visibility TEXT DEFAULT 'public',
    stability TEXT DEFAULT 'stable',
    feature_tags TEXT DEFAULT '',
    build_commit TEXT DEFAULT '',
    build_time TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(repo_id, file_path, start_line, chunk_type)
);

CREATE INDEX IF NOT EXISTS idx_sc_repo ON source_chunks(repo_id);
CREATE INDEX IF NOT EXISTS idx_sc_type ON source_chunks(chunk_type);

-- ── Candidate Cards table ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS candidate_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    slug TEXT UNIQUE NOT NULL,
    category TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    content TEXT DEFAULT '',
    tags TEXT DEFAULT '',
    keywords TEXT DEFAULT '',
    aliases TEXT DEFAULT '',
    related_symbols TEXT DEFAULT '',
    related_docs TEXT DEFAULT '',
    related_examples TEXT DEFAULT '',
    source_type TEXT DEFAULT 'generated',
    source_id TEXT DEFAULT '',
    content_domain TEXT DEFAULT 'knowledge',
    visibility TEXT DEFAULT 'internal',
    stability TEXT DEFAULT 'draft',
    feature_tags TEXT DEFAULT '',
    build_commit TEXT DEFAULT '',
    build_time TEXT DEFAULT '',
    confidence REAL DEFAULT 0.0,
    status TEXT DEFAULT 'candidate',
    owner TEXT DEFAULT '',
    reviewed_by TEXT DEFAULT '',
    review_notes TEXT DEFAULT '',
    change_summary TEXT DEFAULT '',
    evidence_quality TEXT DEFAULT '',
    evidence TEXT DEFAULT '',
    applicable_versions TEXT DEFAULT '',
    lifecycle_status TEXT DEFAULT 'active',
    last_evidence_at TEXT DEFAULT '',
    last_seen_commit TEXT DEFAULT '',
    stale_score REAL DEFAULT 0.0,
    evidence_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    reviewed_at TEXT DEFAULT '',
    published_at TEXT DEFAULT ''
);

CREATE VIRTUAL TABLE IF NOT EXISTS cc_fts USING fts5(
    title, summary, content, tags, keywords,
    content='candidate_cards',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS cc_ai AFTER INSERT ON candidate_cards BEGIN
    INSERT INTO cc_fts(rowid, title, summary, content, tags, keywords)
    VALUES (new.id, new.title, new.summary, new.content, new.tags, new.keywords);
END;

CREATE TRIGGER IF NOT EXISTS cc_ad AFTER DELETE ON candidate_cards BEGIN
    INSERT INTO cc_fts(cc_fts, rowid, title, summary, content, tags, keywords)
    VALUES ('delete', old.id, old.title, old.summary, old.content, old.tags, old.keywords);
END;

CREATE INDEX IF NOT EXISTS idx_cc_status ON candidate_cards(status);
CREATE INDEX IF NOT EXISTS idx_cc_category ON candidate_cards(category);
CREATE INDEX IF NOT EXISTS idx_cc_confidence ON candidate_cards(confidence DESC);

-- ── Knowledge Card Relations table ────────────────────────────────

CREATE TABLE IF NOT EXISTS card_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_slug TEXT NOT NULL,
    target_slug TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    weight REAL DEFAULT 1.0,
    evidence TEXT DEFAULT '',
    UNIQUE(source_slug, target_slug, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_cr_source ON card_relations(source_slug);
CREATE INDEX IF NOT EXISTS idx_cr_target ON card_relations(target_slug);

CREATE TABLE IF NOT EXISTS repo_heads (
    repo_id TEXT PRIMARY KEY,
    commit_hash TEXT DEFAULT '',
    recorded_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tool_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name TEXT NOT NULL,
    status TEXT DEFAULT '',
    latency_ms REAL DEFAULT 0.0,
    query_text TEXT DEFAULT '',
    details TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tool_telemetry_name ON tool_telemetry(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_telemetry_status ON tool_telemetry(status);
"""


class Database:
    """SQLite database wrapper with FTS5 support."""

    def __init__(self, db_path: str = "index.db"):
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None

    def connect(self) -> sqlite3.Connection:
        """Open or return existing connection."""
        if self.conn is None:
            self.conn = sqlite3.connect(self.db_path)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
        return self.conn

    def _metadata_text(self, value: object, default: str = "") -> str:
        if value is None:
            return default
        if isinstance(value, (list, tuple, set)):
            items = [str(item).strip() for item in value if str(item).strip()]
            return ", ".join(items) if items else default
        text = str(value).strip()
        return text or default

    def _split_metadata_tags(self, value: object) -> list[str]:
        text = self._metadata_text(value)
        if not text:
            return []
        parts = re.split(r"[,\|;/]+", text)
        if len(parts) == 1 and " " in text and not any(ch in text for ch in ",|;/"):
            parts = text.split()
        return [part.strip().lower() for part in parts if part.strip()]

    def _merge_metadata_tags(self, *values: object) -> str:
        seen = set()
        merged: list[str] = []
        for value in values:
            for tag in self._split_metadata_tags(value):
                if tag in seen:
                    continue
                seen.add(tag)
                merged.append(tag)
        return ", ".join(merged)

    def _metadata_now(self, build_time: object = "") -> str:
        text = self._metadata_text(build_time)
        if text:
            return text
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _file_metadata(self, file_id: int) -> dict:
        conn = self.connect()
        row = conn.execute(
            "SELECT rel_path, is_example, content_domain, visibility, stability, feature_tags, build_commit, build_time "
            "FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()
        return dict(row) if row else {}

    def _infer_content_domain(self, *, rel_path: str = "", title: str = "", source_type: str = "",
                              is_example: bool = False, chunk_type: str = "", kind: str = "") -> str:
        normalized = " ".join(
            part for part in (
                rel_path.replace("\\", "/") if rel_path else "",
                title,
                source_type,
                chunk_type,
                kind,
            )
            if part
        ).lower()
        filename = Path(rel_path or title or "").name.lower()
        if "test" in normalized or filename.startswith("test_") or filename.endswith("_test.kt") or filename.endswith("_test.java") or filename.endswith("test.kt") or filename.endswith("test.java"):
            return "test"
        if is_example or "example" in normalized or "demo" in normalized or "sample" in normalized:
            return "example"
        if "migration" in normalized or "migrate" in normalized:
            return "migration"
        if "faq" in normalized:
            return "faq"
        if source_type in {"docs", "wiki"}:
            if "api" in normalized or filename in {"api.md", "api.mdx", "api.adoc"}:
                return "api"
            if "migration" in normalized:
                return "migration"
            if "faq" in normalized:
                return "faq"
            return "doc"
        if source_type == "knowledge":
            if "faq" in normalized:
                return "faq"
            if "migration" in normalized:
                return "migration"
            return "knowledge"
        if kind in {"class", "interface", "object", "enum", "annotation", "function", "method"}:
            return "api"
        return "source"

    def _infer_visibility(self, explicit: object = "", *, source_type: str = "", is_example: bool = False) -> str:
        text = self._metadata_text(explicit)
        if text:
            return text
        if source_type == "candidate":
            return "internal"
        if source_type == "knowledge" and not is_example:
            return "public"
        return "public"

    def _infer_stability(self, explicit: object = "", *, rel_path: str = "", title: str = "",
                         source_type: str = "", is_example: bool = False) -> str:
        text = self._metadata_text(explicit)
        if text:
            return text
        normalized = " ".join(part for part in (rel_path, title, source_type) if part).lower()
        if "deprecated" in normalized:
            return "deprecated"
        if "experimental" in normalized or "preview" in normalized or "beta" in normalized:
            return "experimental"
        if source_type == "candidate":
            return "draft"
        if is_example:
            return "stable"
        return "stable"

    def _infer_feature_tags(self, explicit: object = "", *fallbacks: object) -> str:
        merged: list[str] = []
        seen = set()
        for tag in self._split_metadata_tags(explicit):
            if tag not in seen:
                seen.add(tag)
                merged.append(tag)
        if merged:
            return ", ".join(merged)
        for fallback in fallbacks:
            text = self._metadata_text(fallback)
            if not text:
                continue
            normalized = text.replace("\\", "/").lower()
            for token in re.split(r"[,\|;/\s]+", normalized):
                token = token.strip()
                if not token or len(token) < 2:
                    continue
                if token in {"src", "main", "test", "tests", "example", "examples", "demo", "docs", "wiki"}:
                    continue
                if token in seen:
                    continue
                seen.add(token)
                merged.append(token)
        return ", ".join(merged)

    def initialize(self) -> None:
        """Create all tables and indexes."""
        conn = self.connect()
        conn.executescript(DB_SCHEMA)
        self._apply_migrations()
        conn.commit()

    def _apply_migrations(self) -> None:
        """Apply lightweight schema migrations for existing databases."""
        migrations = {
            "repositories": {
                "source_type": "TEXT DEFAULT 'code'",
                "git_url": "TEXT DEFAULT ''",
                "linked_source_id": "TEXT DEFAULT ''",
                "enabled": "INTEGER DEFAULT 1",
                "last_synced_at": "TEXT DEFAULT ''",
                "last_synced_rev": "TEXT DEFAULT ''",
                "last_indexed_at": "TEXT DEFAULT ''",
                "last_indexed_rev": "TEXT DEFAULT ''",
            },
            "files": {
                "content_domain": "TEXT DEFAULT 'source'",
                "visibility": "TEXT DEFAULT 'public'",
                "stability": "TEXT DEFAULT 'stable'",
                "feature_tags": "TEXT DEFAULT ''",
                "build_commit": "TEXT DEFAULT ''",
                "build_time": "TEXT DEFAULT ''",
            },
            "symbols": {
                "content_domain": "TEXT DEFAULT 'api'",
                "visibility": "TEXT DEFAULT 'public'",
                "stability": "TEXT DEFAULT 'stable'",
                "feature_tags": "TEXT DEFAULT ''",
                "build_commit": "TEXT DEFAULT ''",
                "build_time": "TEXT DEFAULT ''",
            },
            "examples": {
                "content_domain": "TEXT DEFAULT 'example'",
                "visibility": "TEXT DEFAULT 'public'",
                "stability": "TEXT DEFAULT 'stable'",
                "feature_tags": "TEXT DEFAULT ''",
                "build_commit": "TEXT DEFAULT ''",
                "build_time": "TEXT DEFAULT ''",
            },
            "docs": {
                "summary": "TEXT DEFAULT ''",
                "headings": "TEXT DEFAULT ''",
                "links": "TEXT DEFAULT ''",
                "source_type": "TEXT DEFAULT 'docs'",
                "content_domain": "TEXT DEFAULT 'doc'",
                "visibility": "TEXT DEFAULT 'public'",
                "stability": "TEXT DEFAULT 'stable'",
                "feature_tags": "TEXT DEFAULT ''",
                "build_commit": "TEXT DEFAULT ''",
                "build_time": "TEXT DEFAULT ''",
            },
            "knowledge_cards": {
                "source_id": "TEXT DEFAULT ''",
                "source_path": "TEXT DEFAULT ''",
                "content_domain": "TEXT DEFAULT 'knowledge'",
                "visibility": "TEXT DEFAULT 'public'",
                "stability": "TEXT DEFAULT 'stable'",
                "feature_tags": "TEXT DEFAULT ''",
                "build_commit": "TEXT DEFAULT ''",
                "build_time": "TEXT DEFAULT ''",
                "owner": "TEXT DEFAULT ''",
                "reviewed_by": "TEXT DEFAULT ''",
                "review_notes": "TEXT DEFAULT ''",
                "change_summary": "TEXT DEFAULT ''",
                "evidence_quality": "TEXT DEFAULT ''",
                "applicable_versions": "TEXT DEFAULT ''",
                "lifecycle_status": "TEXT DEFAULT 'active'",
                "last_evidence_at": "TEXT DEFAULT ''",
                "last_seen_commit": "TEXT DEFAULT ''",
                "stale_score": "REAL DEFAULT 0.0",
                "evidence_count": "INTEGER DEFAULT 0",
            },
            "source_chunks": {
                "content_domain": "TEXT DEFAULT 'source'",
                "visibility": "TEXT DEFAULT 'public'",
                "stability": "TEXT DEFAULT 'stable'",
                "feature_tags": "TEXT DEFAULT ''",
                "build_commit": "TEXT DEFAULT ''",
                "build_time": "TEXT DEFAULT ''",
            },
            "candidate_cards": {
                "aliases": "TEXT DEFAULT ''",
                "source_id": "TEXT DEFAULT ''",
                "content_domain": "TEXT DEFAULT 'knowledge'",
                "visibility": "TEXT DEFAULT 'internal'",
                "stability": "TEXT DEFAULT 'draft'",
                "feature_tags": "TEXT DEFAULT ''",
                "build_commit": "TEXT DEFAULT ''",
                "build_time": "TEXT DEFAULT ''",
                "owner": "TEXT DEFAULT ''",
                "reviewed_by": "TEXT DEFAULT ''",
                "review_notes": "TEXT DEFAULT ''",
                "change_summary": "TEXT DEFAULT ''",
                "evidence_quality": "TEXT DEFAULT ''",
                "applicable_versions": "TEXT DEFAULT ''",
                "lifecycle_status": "TEXT DEFAULT 'active'",
                "last_evidence_at": "TEXT DEFAULT ''",
                "last_seen_commit": "TEXT DEFAULT ''",
                "stale_score": "REAL DEFAULT 0.0",
                "evidence_count": "INTEGER DEFAULT 0",
            },
        }
        for table, columns in migrations.items():
            for column, ddl in columns.items():
                self._ensure_column(table, column, ddl)
        self._ensure_post_migration_indexes()

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        """Add a column if it does not exist yet."""
        conn = self.connect()
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in rows}
        if column in existing:
            return
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _ensure_post_migration_indexes(self) -> None:
        """Create indexes that depend on migrated columns."""
        conn = self.connect()
        conn.execute("CREATE INDEX IF NOT EXISTS idx_kc_lifecycle_status ON knowledge_cards(lifecycle_status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_lifecycle_status ON candidate_cards(lifecycle_status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_repo_source_type ON repositories(source_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_docs_source_type ON docs(source_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_kc_source_id ON knowledge_cards(source_id)")

    def close(self) -> None:
        """Close the database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None

    def clear_repo(self, repo_id: str) -> None:
        """Remove all data for a specific repo before re-indexing."""
        conn = self.connect()
        conn.execute("DELETE FROM examples WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM symbols WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM docs WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM files WHERE repo_id = ?", (repo_id,))
        conn.commit()

    def upsert_repo(self, repo_id: str, name: str, path: str, tags: str) -> None:
        """Insert or update a repository record."""
        self.upsert_source(repo_id, name, path, tags, source_type="code")

    def upsert_source(self, source_id: str, name: str, path: str, tags: str,
                      source_type: str = "code",
                      git_url: str = "",
                      linked_source_id: str = "",
                      enabled: bool = True) -> None:
        """Insert or update a source record."""
        conn = self.connect()
        existing = conn.execute(
            "SELECT last_synced_at, last_synced_rev, last_indexed_at, last_indexed_rev FROM repositories WHERE id = ?",
            (source_id,),
        ).fetchone()
        conn.execute(
            """INSERT OR REPLACE INTO repositories
               (id, name, path, tags, source_type, git_url, linked_source_id, enabled,
                last_synced_at, last_synced_rev, last_indexed_at, last_indexed_rev)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_id,
                name,
                path,
                tags,
                source_type,
                git_url,
                linked_source_id,
                int(enabled),
                existing["last_synced_at"] if existing else "",
                existing["last_synced_rev"] if existing else "",
                existing["last_indexed_at"] if existing else "",
                existing["last_indexed_rev"] if existing else "",
            ),
        )
        conn.commit()

    def get_source(self, source_id: str) -> Optional[dict]:
        """Get source metadata by id."""
        conn = self.connect()
        row = conn.execute("SELECT * FROM repositories WHERE id = ?", (source_id,)).fetchone()
        return dict(row) if row else None

    def list_sources(self, source_type: Optional[str] = None,
                     enabled_only: bool = False) -> list[dict]:
        """List registered sources from the database."""
        conn = self.connect()
        sql = "SELECT * FROM repositories WHERE 1=1"
        params: list = []
        if source_type:
            sql += " AND source_type = ?"
            params.append(source_type)
        if enabled_only:
            sql += " AND enabled = 1"
        sql += " ORDER BY source_type, id"
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def update_source_sync_state(self, source_id: str,
                                 synced_at: str = "",
                                 synced_rev: str = "") -> bool:
        """Persist last sync timestamp/revision for a source."""
        conn = self.connect()
        conn.execute(
            """UPDATE repositories
               SET last_synced_at = ?, last_synced_rev = ?
               WHERE id = ?""",
            (synced_at, synced_rev, source_id),
        )
        conn.commit()
        return conn.total_changes > 0

    def update_source_index_state(self, source_id: str,
                                  indexed_at: str = "",
                                  indexed_rev: str = "") -> bool:
        """Persist last indexed timestamp/revision for a source."""
        conn = self.connect()
        conn.execute(
            """UPDATE repositories
               SET last_indexed_at = ?, last_indexed_rev = ?
               WHERE id = ?""",
            (indexed_at, indexed_rev, source_id),
        )
        conn.commit()
        return conn.total_changes > 0

    def clear_source_docs(self, source_id: str, paths: Optional[list[str]] = None) -> None:
        """Delete docs rows for a specific source, optionally only selected paths."""
        conn = self.connect()
        if paths:
            conn.executemany(
                "DELETE FROM docs WHERE repo_id = ? AND path = ?",
                [(source_id, path) for path in paths],
            )
        else:
            conn.execute("DELETE FROM docs WHERE repo_id = ?", (source_id,))
        conn.commit()

    def clear_source_files(self, source_id: str, rel_paths: Optional[list[str]] = None) -> None:
        """Delete indexed code rows for a source, optionally only selected files."""
        conn = self.connect()
        if rel_paths:
            rows = conn.execute(
                f"SELECT id FROM files WHERE repo_id = ? AND rel_path IN ({', '.join('?' for _ in rel_paths)})",
                [source_id, *rel_paths],
            ).fetchall()
            file_ids = [row["id"] for row in rows]
            if file_ids:
                placeholders = ", ".join("?" for _ in file_ids)
                conn.execute(f"DELETE FROM examples WHERE file_id IN ({placeholders})", file_ids)
                conn.execute(f"DELETE FROM symbols WHERE file_id IN ({placeholders})", file_ids)
            conn.executemany(
                "DELETE FROM files WHERE repo_id = ? AND rel_path = ?",
                [(source_id, path) for path in rel_paths],
            )
        else:
            rows = conn.execute("SELECT id FROM files WHERE repo_id = ?", (source_id,)).fetchall()
            file_ids = [row["id"] for row in rows]
            if file_ids:
                placeholders = ", ".join("?" for _ in file_ids)
                conn.execute(f"DELETE FROM examples WHERE file_id IN ({placeholders})", file_ids)
                conn.execute(f"DELETE FROM symbols WHERE file_id IN ({placeholders})", file_ids)
            conn.execute("DELETE FROM files WHERE repo_id = ?", (source_id,))
        conn.commit()

    def clear_source_cards(self, source_id: str, slugs: Optional[list[str]] = None) -> None:
        """Delete knowledge cards for a specific knowledge source."""
        conn = self.connect()
        if slugs:
            conn.executemany(
                "DELETE FROM knowledge_cards WHERE source_id = ? AND slug = ?",
                [(source_id, slug) for slug in slugs],
            )
        else:
            conn.execute("DELETE FROM knowledge_cards WHERE source_id = ?", (source_id,))
        conn.commit()

    def clear_source_cards_by_path(self, source_id: str, source_paths: list[str]) -> None:
        """Delete knowledge cards for a source by their indexed source path."""
        if not source_paths:
            return
        conn = self.connect()
        conn.executemany(
            "DELETE FROM knowledge_cards WHERE source_id = ? AND source_path = ?",
            [(source_id, source_path) for source_path in source_paths],
        )
        conn.commit()

    def insert_file(self, repo_id: str, rel_path: str, language: str,
                    content: str, is_example: bool,
                    content_domain: str = "", visibility: str = "",
                    stability: str = "", feature_tags: object = "",
                    build_commit: str = "", build_time: str = "") -> int:
        """Insert a file record and return its id."""
        conn = self.connect()
        inferred_domain = self._infer_content_domain(rel_path=rel_path, is_example=is_example)
        inferred_visibility = self._infer_visibility(visibility, is_example=is_example)
        inferred_stability = self._infer_stability(stability, rel_path=rel_path, is_example=is_example)
        tags = self._infer_feature_tags(feature_tags, rel_path)
        cur = conn.execute(
            """INSERT INTO files
               (repo_id, rel_path, language, content, is_example,
                content_domain, visibility, stability, feature_tags, build_commit, build_time)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                repo_id,
                rel_path,
                language,
                content,
                int(is_example),
                content_domain or inferred_domain,
                inferred_visibility,
                inferred_stability,
                tags,
                self._metadata_text(build_commit),
                self._metadata_now(build_time),
            ),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore

    def insert_symbol(self, repo_id: str, file_id: int, name: str,
                      qualified_name: str, kind: str, signature: str,
                      line_number: int, end_line: int,
                      content_domain: str = "", visibility: str = "",
                      stability: str = "", feature_tags: object = "",
                      build_commit: str = "", build_time: str = "") -> int:
        """Insert a symbol record and return its id."""
        conn = self.connect()
        file_meta = self._file_metadata(file_id)
        inferred_domain = self._infer_content_domain(
            rel_path=str(file_meta.get("rel_path", "") or ""),
            is_example=bool(file_meta.get("is_example", 0)),
            kind=kind,
        )
        inferred_visibility = self._infer_visibility(
            visibility,
            is_example=bool(file_meta.get("is_example", 0)),
        )
        inferred_stability = self._infer_stability(
            stability,
            rel_path=str(file_meta.get("rel_path", "") or ""),
            source_type="knowledge" if bool(file_meta.get("is_example", 0)) else "",
            is_example=bool(file_meta.get("is_example", 0)),
        )
        tags = self._infer_feature_tags(
            feature_tags,
            qualified_name,
            kind,
            file_meta.get("rel_path", ""),
        )
        cur = conn.execute(
            """INSERT INTO symbols
               (repo_id, file_id, name, qualified_name, kind, signature, line_number, end_line,
                content_domain, visibility, stability, feature_tags, build_commit, build_time)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                repo_id,
                file_id,
                name,
                qualified_name,
                kind,
                signature,
                line_number,
                end_line,
                content_domain or inferred_domain,
                inferred_visibility,
                inferred_stability,
                tags,
                self._metadata_text(build_commit or file_meta.get("build_commit", "")),
                self._metadata_now(build_time or file_meta.get("build_time", "")),
            ),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore

    def insert_example(self, repo_id: str, file_id: int, title: str,
                       snippet: str,
                       content_domain: str = "", visibility: str = "",
                       stability: str = "", feature_tags: object = "",
                       build_commit: str = "", build_time: str = "") -> int:
        """Insert an example record and return its id."""
        conn = self.connect()
        file_meta = self._file_metadata(file_id)
        inferred_domain = self._infer_content_domain(
            rel_path=str(file_meta.get("rel_path", "") or ""),
            title=title,
            is_example=True,
        )
        inferred_visibility = self._infer_visibility(
            visibility,
            is_example=True,
        )
        inferred_stability = self._infer_stability(
            stability,
            rel_path=str(file_meta.get("rel_path", "") or ""),
            title=title,
            is_example=True,
        )
        tags = self._infer_feature_tags(feature_tags, title, file_meta.get("rel_path", ""))
        cur = conn.execute(
            """INSERT INTO examples
               (repo_id, file_id, title, snippet, content_domain, visibility, stability,
                feature_tags, build_commit, build_time)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                repo_id,
                file_id,
                title,
                snippet,
                content_domain or inferred_domain,
                inferred_visibility,
                inferred_stability,
                tags,
                self._metadata_text(build_commit or file_meta.get("build_commit", "")),
                self._metadata_now(build_time or file_meta.get("build_time", "")),
            ),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore

    def _normalize_bm25(self, raw_value: object) -> float:
        if raw_value is None:
            return 0.0
        try:
            numeric = abs(float(raw_value))
        except (TypeError, ValueError):
            return 0.0
        return round(1.6 / (1.0 + numeric), 4)

    def _extract_query_versions(self, query: str) -> list[str]:
        return re.findall(r"\b\d+\.\d+(?:\.\d+)?\b", query or "")

    def _relation_signals(self, slugs: list[str]) -> dict[str, float]:
        if not slugs:
            return {}
        conn = self.connect()
        placeholders = ", ".join("?" for _ in slugs)
        outgoing = conn.execute(
            f"""SELECT source_slug AS slug, SUM(weight) AS total_weight
                FROM card_relations
                WHERE source_slug IN ({placeholders})
                GROUP BY source_slug""",
            slugs,
        ).fetchall()
        incoming = conn.execute(
            f"""SELECT target_slug AS slug, SUM(weight) AS total_weight
                FROM card_relations
                WHERE target_slug IN ({placeholders})
                GROUP BY target_slug""",
            slugs,
        ).fetchall()
        signals: dict[str, float] = {}
        for row in outgoing + incoming:
            slug = row["slug"]
            signals[slug] = signals.get(slug, 0.0) + float(row["total_weight"] or 0.0)
        return signals

    def _freshness_components(self, item: dict, query_versions: list[str]) -> tuple[float, float, list[str]]:
        freshness = 0.0
        stale_penalty = float(item.get("stale_score", 0.0) or 0.0) * 5.0
        reasons: list[str] = []

        lifecycle = str(item.get("lifecycle_status", "active") or "active").lower()
        if lifecycle == "active":
            freshness += 1.2
            reasons.append("active lifecycle")
        elif lifecycle == "review":
            freshness += 0.45
            reasons.append("review lifecycle")
        elif lifecycle == "stale":
            stale_penalty += 1.2
            reasons.append("stale lifecycle penalty")

        evidence_count = int(item.get("evidence_count", 0) or 0)
        if evidence_count:
            evidence_boost = min(0.9, evidence_count / 12.0)
            freshness += evidence_boost
            reasons.append(f"{evidence_count} evidence points")

        last_evidence_at = str(item.get("last_evidence_at", "") or "").strip()
        if last_evidence_at and evidence_count:
            try:
                normalized = last_evidence_at.replace("Z", "+00:00")
                evidence_time = datetime.fromisoformat(normalized)
                if evidence_time.tzinfo is None:
                    evidence_time = evidence_time.replace(tzinfo=timezone.utc)
                age_days = (datetime.now(timezone.utc) - evidence_time).days
                if age_days <= 30:
                    freshness += 0.7
                    reasons.append("recent evidence <=30d")
                elif age_days <= 90:
                    freshness += 0.35
                    reasons.append("recent evidence <=90d")
                elif age_days >= 365:
                    stale_penalty += 0.6
                    reasons.append("evidence older than 1y")
            except ValueError:
                pass

        applicable_versions = str(item.get("applicable_versions", "") or "").lower()
        if query_versions and applicable_versions:
            if any(version.lower() in applicable_versions for version in query_versions):
                freshness += 0.55
                reasons.append("version-aligned")
            else:
                stale_penalty += 1.5
                reasons.append("version mismatch penalty")

        return round(freshness, 4), round(stale_penalty, 4), reasons

    def _match_details(self, item: dict, terms: list[str], field_weights: dict[str, float]) -> list[dict]:
        details: list[dict] = []
        for field, weight in field_weights.items():
            value = str(item.get(field, "") or "")
            if not value:
                continue
            matched_terms = [term for term in terms if term and term in value.lower()]
            if not matched_terms:
                continue
            details.append({
                "field": field,
                "matched_terms": matched_terms[:6],
                "weight": round(weight, 3),
            })
        return details

    def _rank_rows(self, rows: list[sqlite3.Row], terms: list[str],
                   field_weights: dict[str, float],
                   limit: int = 20,
                   priority_field: Optional[str] = None,
                   priority_scale: float = 20.0,
                   graph_field: Optional[str] = None,
                   stale_field: Optional[str] = None,
                   bm25_field: Optional[str] = None,
                   query_text: str = "") -> list[dict]:
        """Rerank rows in Python with freshness, graph, and bilingual signals."""
        query_versions = self._extract_query_versions(query_text)
        relation_signals = self._relation_signals(
            [str(dict(row).get(graph_field, "") or "") for row in rows]
        ) if graph_field else {}
        ranked: list[dict] = []
        for row in rows:
            item = dict(row)
            relevance = 0.0
            match_details = self._match_details(item, terms, field_weights)
            for field, weight in field_weights.items():
                relevance += score_text_match(str(item.get(field, "")), terms) * weight
            if bm25_field:
                relevance += self._normalize_bm25(item.get(bm25_field))

            priority_signal = 0.0
            if priority_field:
                priority_signal = float(item.get(priority_field, 0) or 0) / max(priority_scale, 0.1)

            graph_signal = 0.0
            if graph_field:
                raw_graph = relation_signals.get(str(item.get(graph_field, "") or ""), 0.0)
                graph_signal = min(2.5, raw_graph / 2.5)

            freshness = 0.0
            stale_penalty = float(item.get(stale_field, 0.0) or 0.0) * 5.0 if stale_field else 0.0
            freshness_reasons: list[str] = []
            if stale_field or "lifecycle_status" in item or "last_evidence_at" in item:
                freshness, computed_penalty, freshness_reasons = self._freshness_components(item, query_versions)
                stale_penalty = computed_penalty if stale_field else 0.0

            final_score = relevance + priority_signal + graph_signal + freshness - stale_penalty
            item["_score"] = round(final_score, 4)
            if graph_field or priority_field or stale_field:
                item["score_breakdown"] = {
                    "relevance": round(relevance, 4),
                    "priority": round(priority_signal, 4),
                    "graph_signal": round(graph_signal, 4),
                    "freshness": round(freshness, 4),
                    "stale_penalty": round(stale_penalty, 4),
                    "final_score": round(final_score, 4),
                    "freshness_reasons": freshness_reasons,
                }
            if match_details:
                item["match_details"] = match_details
            ranked.append(item)

        def _kind_priority(item: dict) -> int:
            kind = str(item.get("kind", "") or "").lower()
            name = str(item.get("name", "") or "")
            qualified_name = str(item.get("qualified_name", "") or "")
            lowered_query = (query_text or "").replace("-", "").replace("_", "").replace(" ", "").lower()
            normalized_name = name.replace("-", "").replace("_", "").replace(" ", "").lower()
            normalized_qname = qualified_name.replace("-", "").replace("_", "").replace(" ", "").lower()
            exact_symbol_match = 1 if lowered_query and normalized_name == lowered_query else 0
            exact_qname_suffix_match = 1 if lowered_query and normalized_qname.endswith(f".{lowered_query}") else 0
            kind_score_map = {
                "annotation": 60,
                "class": 55,
                "interface": 50,
                "object": 45,
                "enum": 40,
                "function": 20,
                "method": 15,
            }
            return exact_symbol_match * 100 + exact_qname_suffix_match * 30 + kind_score_map.get(kind, 0)

        ranked.sort(key=lambda r: (_kind_priority(r), r["_score"], r.get("priority", 0)), reverse=True)
        for item in ranked:
            item.pop("_score", None)
        return ranked[:limit]

    def _expanded_terms(self, query: str) -> list[str]:
        terms = expand_query_terms(query)
        return terms or [query.lower()]

    def _normalize_symbol_token(self, text: str) -> str:
        return str(text or "").replace("-", "").replace("_", "").replace(" ", "").lower()

    def _symbol_sort_key(self, item: dict, query: str) -> tuple:
        normalized_query = self._normalize_symbol_token(query)
        name = str(item.get("name", "") or "")
        qualified_name = str(item.get("qualified_name", "") or "")
        kind = str(item.get("kind", "") or "").lower()
        normalized_name = self._normalize_symbol_token(name)
        normalized_qname = self._normalize_symbol_token(qualified_name)
        qname_last = self._normalize_symbol_token(qualified_name.split(".")[-1] if qualified_name else "")
        exact_name = 1 if normalized_query and normalized_name == normalized_query else 0
        exact_qname_tail = 1 if normalized_query and qname_last == normalized_query else 0
        kind_priority = {
            "annotation": 60,
            "class": 55,
            "interface": 50,
            "object": 45,
            "enum": 40,
            "function": 20,
            "method": 15,
        }.get(kind, 0)
        is_definition = 1 if exact_qname_tail and kind in {"annotation", "class", "interface", "object", "enum"} else 0
        member_depth_penalty = -qualified_name.count(".")
        score = 0.0
        breakdown = item.get("score_breakdown") or {}
        if breakdown:
            score = float(breakdown.get("final_score", 0.0) or 0.0)
        has_member_suffix = 1 if exact_qname_tail and normalized_name != normalized_query else 0
        path_prefers_definition = 1 if normalized_query and self._normalize_symbol_token(item.get("rel_path", "")).endswith(f"{normalized_query}.java") else 0
        return (exact_name, is_definition, path_prefers_definition, 1 - has_member_suffix, exact_qname_tail, kind_priority, member_depth_penalty, score, -int(item.get("line_number", 0) or 0), -len(normalized_qname))

    def search_symbols(self, query: str, repo_id: Optional[str] = None,
                       kind: Optional[str] = None, limit: int = 30) -> list[dict]:
        """Search symbols using FTS5."""
        conn = self.connect()
        fts_query = build_fts_or_query(query)
        terms = self._expanded_terms(query)

        sql = """
            SELECT s.name, s.qualified_name, s.kind, s.signature,
                   s.line_number, s.end_line, s.repo_id,
                   s.content_domain, s.visibility, s.stability, s.feature_tags, s.build_commit, s.build_time,
                   f.rel_path
            FROM symbols_fts fts
            JOIN symbols s ON s.id = fts.rowid
            JOIN files f ON f.id = s.file_id
            WHERE symbols_fts MATCH ?
        """
        params: list = [fts_query]

        if repo_id:
            sql += " AND s.repo_id = ?"
            params.append(repo_id)
        if kind:
            sql += " AND s.kind = ?"
            params.append(kind)

        sql += " ORDER BY bm25(symbols_fts), s.line_number LIMIT ?"
        params.append(limit)

        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # Fallback to LIKE search if FTS fails
            return self._search_symbols_like(query, repo_id, kind, limit)

        ranked = self._rank_rows(
            rows,
            terms,
            {
                "name": 4.0,
                "qualified_name": 3.0,
                "signature": 2.0,
                "rel_path": 1.2,
            },
            limit=limit,
            query_text=query,
        )
        ranked.sort(key=lambda item: self._symbol_sort_key(item, query), reverse=True)
        return ranked[:limit]

    def _search_symbols_like(self, query: str, repo_id: Optional[str] = None,
                             kind: Optional[str] = None, limit: int = 30) -> list[dict]:
        """Fallback symbol search using LIKE."""
        conn = self.connect()
        sql = """
            SELECT s.name, s.qualified_name, s.kind, s.signature,
                   s.line_number, s.end_line, s.repo_id,
                   s.content_domain, s.visibility, s.stability, s.feature_tags, s.build_commit, s.build_time,
                   f.rel_path
            FROM symbols s
            JOIN files f ON f.id = s.file_id
            WHERE (s.name LIKE ? OR s.qualified_name LIKE ? OR s.signature LIKE ?)
        """
        like = f"%{query}%"
        params: list = [like, like, like]

        if repo_id:
            sql += " AND s.repo_id = ?"
            params.append(repo_id)
        if kind:
            sql += " AND s.kind = ?"
            params.append(kind)

        sql += " LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        ranked = self._rank_rows(
            rows,
            self._expanded_terms(query),
            {
                "name": 4.0,
                "qualified_name": 3.0,
                "signature": 2.0,
                "rel_path": 1.2,
            },
            limit=limit,
            query_text=query,
        )
        ranked.sort(key=lambda item: self._symbol_sort_key(item, query), reverse=True)
        return ranked[:limit]

    def search_files_fts(self, query: str, repo_id: Optional[str] = None,
                         limit: int = 20) -> list[dict]:
        """Search file contents using FTS5."""
        conn = self.connect()
        fts_query = build_fts_or_query(query)
        terms = self._expanded_terms(query)

        sql = """
            SELECT f.id, f.repo_id, f.rel_path, f.language, f.is_example,
                   f.rel_path as title,
                   f.content_domain, f.visibility, f.stability, f.feature_tags, f.build_commit, f.build_time,
                   snippet(files_fts, 1, '>>>', '<<<', '...', 40) as snippet
            FROM files_fts fts
            JOIN files f ON f.id = fts.rowid
            WHERE files_fts MATCH ?
        """
        params: list = [fts_query]

        if repo_id:
            sql += " AND f.repo_id = ?"
            params.append(repo_id)

        sql += " ORDER BY bm25(files_fts) LIMIT ?"
        params.append(limit)

        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []

        return self._rank_rows(
            rows,
            terms,
            {
                "rel_path": 2.0,
                "snippet": 1.6,
                "language": 0.3,
            },
            limit=limit,
        )

    def get_examples(self, query: str, repo_id: Optional[str] = None,
                     limit: int = 20) -> list[dict]:
        """Search example files with support for multi-keyword ranking."""
        conn = self.connect()
        terms = self._expanded_terms(query)
        phrase_like = f"%{query}%"
        keyword_terms = [term for term in terms if len(term.strip()) >= 3][:8]

        conditions = ["(e.title LIKE ? OR e.snippet LIKE ? OR f.rel_path LIKE ?)"]
        params: list = [phrase_like, phrase_like, phrase_like]
        for term in keyword_terms:
            like = f"%{term}%"
            conditions.append("(e.title LIKE ? OR e.snippet LIKE ? OR f.rel_path LIKE ?)")
            params.extend([like, like, like])

        sql = f"""
            SELECT e.title, e.snippet, e.repo_id, f.rel_path,
                   e.content_domain, e.visibility, e.stability, e.feature_tags, e.build_commit, e.build_time
            FROM examples e
            JOIN files f ON f.id = e.file_id
            WHERE {' OR '.join(conditions)}
        """

        if repo_id:
            sql += " AND e.repo_id = ?"
            params.append(repo_id)

        sql += " LIMIT ?"
        params.append(max(limit * 5, 50))

        rows = conn.execute(sql, params).fetchall()
        return self._rank_rows(
            rows,
            terms,
            {
                "title": 3.0,
                "rel_path": 2.4,
                "snippet": 2.8,
            },
            limit=limit,
            query_text=query,
        )

    def get_symbol_by_name(self, name: str) -> list[dict]:
        """Get symbols by exact or partial name match."""
        conn = self.connect()
        rows = conn.execute(
            """
            SELECT s.name, s.qualified_name, s.kind, s.signature,
                   s.line_number, s.end_line, s.repo_id,
                   f.rel_path
            FROM symbols s
            JOIN files f ON f.id = s.file_id
            WHERE s.name = ? OR s.qualified_name LIKE ?
            LIMIT 20
            """,
            (name, f"%{name}%"),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_file_content(self, repo_id: str, rel_path: str) -> Optional[str]:
        """Get file content by repo and path."""
        conn = self.connect()
        row = conn.execute(
            "SELECT content FROM files WHERE repo_id = ? AND rel_path = ?",
            (repo_id, rel_path),
        ).fetchone()
        return row["content"] if row else None

    def get_repo_path(self, repo_id: str) -> Optional[str]:
        """Get the filesystem path for a repo."""
        conn = self.connect()
        row = conn.execute(
            "SELECT path FROM repositories WHERE id = ?",
            (repo_id,),
        ).fetchone()
        return row["path"] if row else None

    def get_source_path(self, source_id: str) -> Optional[str]:
        """Get filesystem path for any registered source."""
        return self.get_repo_path(source_id)

    def get_all_repos(self) -> list[dict]:
        """Get all repository records."""
        return self.list_sources(source_type="code")

    def find_usages(self, symbol_name: str, repo_id: Optional[str] = None,
                    limit: int = 30) -> list[dict]:
        """Find files that reference a symbol name."""
        conn = self.connect()
        like = f"%{symbol_name}%"

        sql = """
            SELECT f.repo_id, f.rel_path, f.language
            FROM files f
            WHERE f.content LIKE ?
        """
        params: list = [like]

        if repo_id:
            sql += " AND f.repo_id = ?"
            params.append(repo_id)

        sql += " LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        results = []
        for r in rows:
            row_dict = dict(r)
            # Extract matching lines
            content = conn.execute(
                "SELECT content FROM files WHERE repo_id = ? AND rel_path = ?",
                (row_dict["repo_id"], row_dict["rel_path"]),
            ).fetchone()
            if content:
                lines = content["content"].split("\n")
                matching = []
                for i, line in enumerate(lines, 1):
                    if symbol_name.lower() in line.lower():
                        matching.append({"line": i, "text": line.strip()})
                        if len(matching) >= 5:
                            break
                row_dict["matches"] = matching
            results.append(row_dict)
        return results

    # ── Documentation methods ──────────────────────────────────────

    def insert_doc(self, repo_id: str, path: str, title: str,
                   tags: str, content: str, language: str = "markdown",
                   summary: str = "", headings: str = "", links: str = "",
                   source_type: str = "docs",
                   content_domain: str = "", visibility: str = "",
                   stability: str = "", feature_tags: object = "",
                   build_commit: str = "", build_time: str = "") -> int:
        """Insert a documentation record and return its id."""
        conn = self.connect()
        inferred_domain = self._infer_content_domain(rel_path=path, title=title, source_type=source_type)
        inferred_visibility = self._infer_visibility(visibility, source_type=source_type)
        inferred_stability = self._infer_stability(stability, rel_path=path, title=title, source_type=source_type)
        inferred_tags = self._merge_metadata_tags(tags, feature_tags)
        cur = conn.execute(
            """INSERT OR REPLACE INTO docs
               (repo_id, path, title, tags, summary, headings, links, source_type, content, language,
                content_domain, visibility, stability, feature_tags, build_commit, build_time)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                repo_id,
                path,
                title,
                tags,
                summary,
                headings,
                links,
                source_type,
                content,
                language,
                content_domain or inferred_domain,
                inferred_visibility,
                inferred_stability,
                inferred_tags,
                self._metadata_text(build_commit),
                self._metadata_now(build_time),
            ),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore

    def search_docs(self, query: str, repo_id: Optional[str] = None,
                    limit: int = 20,
                    source_type: Optional[str] = None) -> list[dict]:
        """Search docs using FTS5, with LIKE fallback."""
        conn = self.connect()
        fts_query = build_fts_or_query(query)
        terms = self._expanded_terms(query)

        sql = """
            SELECT d.id, d.repo_id, d.path, d.title, d.tags, d.summary, d.headings, d.links, d.source_type, d.language,
                   d.content_domain, d.visibility, d.stability, d.feature_tags, d.build_commit, d.build_time,
                   snippet(docs_fts, 2, '>>>', '<<<', '...', 50) as snippet,
                   bm25(docs_fts) as bm25_rank
            FROM docs_fts fts
            JOIN docs d ON d.id = fts.rowid
            WHERE docs_fts MATCH ?
        """
        params: list = [fts_query]

        if repo_id:
            sql += " AND d.repo_id = ?"
            params.append(repo_id)
        if source_type:
            sql += " AND d.source_type = ?"
            params.append(source_type)

        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)

        try:
            rows = conn.execute(sql, params).fetchall()
            if rows:
                return self._rank_rows(
                    rows,
                    terms,
                    {
                        "title": 3.0,
                        "summary": 2.2,
                        "headings": 2.0,
                        "tags": 2.0,
                        "path": 1.8,
                        "snippet": 1.4,
                    },
                    limit=limit,
                    bm25_field="bm25_rank",
                    query_text=query,
                )
        except sqlite3.OperationalError:
            pass

        # LIKE fallback
        return self._search_docs_like(query, repo_id, limit, source_type=source_type)

    def _search_docs_like(self, query: str, repo_id: Optional[str] = None,
                          limit: int = 20,
                          source_type: Optional[str] = None) -> list[dict]:
        """Fallback doc search using LIKE."""
        conn = self.connect()
        like = f"%{query}%"
        sql = """
            SELECT d.id, d.repo_id, d.path, d.title, d.tags, d.summary, d.headings, d.links, d.source_type, d.language,
                   d.content_domain, d.visibility, d.stability, d.feature_tags, d.build_commit, d.build_time,
                   substr(d.content, 1, 300) as snippet
            FROM docs d
            WHERE (d.title LIKE ? OR d.tags LIKE ? OR d.summary LIKE ? OR d.headings LIKE ? OR d.content LIKE ? OR d.path LIKE ?)
        """
        params: list = [like, like, like, like, like, like]

        if repo_id:
            sql += " AND d.repo_id = ?"
            params.append(repo_id)
        if source_type:
            sql += " AND d.source_type = ?"
            params.append(source_type)

        sql += " LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        return self._rank_rows(
            rows,
            terms=self._expanded_terms(query),
            field_weights={
                "title": 3.0,
                "summary": 2.2,
                "headings": 2.0,
                "tags": 2.0,
                "path": 1.8,
                "snippet": 1.4,
            },
            limit=limit,
            query_text=query,
        )

    def get_doc_content(self, repo_id: str, path: str) -> Optional[dict]:
        """Get a single doc by repo_id and path."""
        conn = self.connect()
        row = conn.execute(
            "SELECT id, repo_id, path, title, tags, summary, headings, links, source_type, content, language, "
            "content_domain, visibility, stability, feature_tags, build_commit, build_time "
            "FROM docs WHERE repo_id = ? AND path = ?",
            (repo_id, path),
        ).fetchone()
        return dict(row) if row else None

    def get_doc_by_path(self, path: str, repo_id: Optional[str] = None,
                        source_type: Optional[str] = None) -> Optional[dict]:
        """Get a document by exact relative path, optionally scoped by repo/source type."""
        conn = self.connect()
        sql = (
            "SELECT id, repo_id, path, title, tags, summary, headings, links, source_type, content, language, "
            "content_domain, visibility, stability, feature_tags, build_commit, build_time "
            "FROM docs WHERE path = ?"
        )
        params: list = [path]
        if repo_id:
            sql += " AND repo_id = ?"
            params.append(repo_id)
        if source_type:
            sql += " AND source_type = ?"
            params.append(source_type)
        sql += " LIMIT 1"
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def get_doc_by_id(self, doc_id: int) -> Optional[dict]:
        """Get a single doc by its id."""
        conn = self.connect()
        row = conn.execute(
            "SELECT id, repo_id, path, title, tags, summary, headings, links, source_type, content, language, "
            "content_domain, visibility, stability, feature_tags, build_commit, build_time "
            "FROM docs WHERE id = ?",
            (doc_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_docs_for_repo(self, repo_id: str) -> list[dict]:
        """List all docs for a repo (without full content)."""
        conn = self.connect()
        rows = conn.execute(
            "SELECT id, repo_id, path, title, tags, summary, headings, links, source_type, language, "
            "content_domain, visibility, stability, feature_tags, build_commit, build_time FROM docs WHERE repo_id = ?",
            (repo_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Knowledge Card methods ─────────────────────────────────────

    def clear_knowledge_cards(self) -> None:
        """Remove all knowledge cards before re-indexing."""
        conn = self.connect()
        conn.execute("DELETE FROM knowledge_cards")
        conn.commit()

    def upsert_knowledge_card(self, card: dict) -> int:
        """Insert or replace a knowledge card. Returns row id."""
        conn = self.connect()
        content_domain = self._metadata_text(card.get("content_domain", ""))
        inferred_domain = self._infer_content_domain(
            rel_path=str(card.get("source_path", "") or ""),
            title=str(card.get("title", "") or ""),
            source_type="knowledge",
        )
        visibility = self._infer_visibility(card.get("visibility", ""), source_type="knowledge")
        stability = self._infer_stability(
            card.get("stability", ""),
            rel_path=str(card.get("source_path", "") or ""),
            title=str(card.get("title", "") or ""),
            source_type="knowledge",
        )
        feature_tags = self._merge_metadata_tags(card.get("tags", ""), card.get("keywords", ""), card.get("feature_tags", ""))
        build_commit = self._metadata_text(card.get("build_commit", ""))
        build_time = self._metadata_now(card.get("build_time", ""))
        columns = [
            "card_id", "title", "slug", "category", "summary", "content", "tags", "keywords",
            "aliases", "when_to_use", "avoid", "best_practices", "related_symbols", "related_docs",
            "related_examples", "related_errors", "source_type", "priority", "source_id", "source_path",
            "content_domain", "visibility", "stability", "feature_tags", "build_commit", "build_time",
            "owner", "reviewed_by", "review_notes", "change_summary", "evidence_quality",
            "applicable_versions", "lifecycle_status", "last_evidence_at", "last_seen_commit",
            "stale_score", "evidence_count",
        ]
        values = [
            card["card_id"],
            card["title"],
            card["slug"],
            card.get("category", ""),
            card.get("summary", ""),
            card.get("content", ""),
            card.get("tags", ""),
            card.get("keywords", ""),
            card.get("aliases", ""),
            card.get("when_to_use", ""),
            card.get("avoid", ""),
            card.get("best_practices", ""),
            card.get("related_symbols", ""),
            card.get("related_docs", ""),
            card.get("related_examples", ""),
            card.get("related_errors", ""),
            card.get("source_type", "manual"),
            card.get("priority", 50),
            card.get("source_id", ""),
            card.get("source_path", ""),
            content_domain or inferred_domain,
            visibility,
            stability,
            feature_tags,
            build_commit,
            build_time,
            card.get("owner", ""),
            card.get("reviewed_by", ""),
            card.get("review_notes", ""),
            card.get("change_summary", ""),
            card.get("evidence_quality", ""),
            card.get("applicable_versions", ""),
            card.get("lifecycle_status", "active"),
            card.get("last_evidence_at", ""),
            card.get("last_seen_commit", ""),
            card.get("stale_score", 0.0),
            card.get("evidence_count", 0),
        ]
        cur = conn.execute(
            f"""INSERT OR REPLACE INTO knowledge_cards
            ({', '.join(columns)})
            VALUES ({', '.join('?' for _ in columns)})""",
            values,
        )
        conn.commit()
        return cur.lastrowid  # type: ignore

    def search_knowledge_cards(self, query: str, category: Optional[str] = None,
                               limit: int = 20,
                               source_id: Optional[str] = None) -> list[dict]:
        """Search knowledge cards using FTS5 with LIKE fallback."""
        conn = self.connect()
        fts_q = build_fts_or_query(query)
        terms = self._expanded_terms(query)

        sql = """
            SELECT kc.id, kc.card_id, kc.title, kc.slug, kc.category,
                   kc.summary, snippet(kc_fts, 2, '>>>', '<<<', '...', 24) as content_snippet,
                   kc.tags, kc.keywords, kc.aliases, kc.priority, kc.source_id, kc.source_path,
                   kc.content_domain, kc.visibility, kc.stability, kc.feature_tags, kc.build_commit, kc.build_time,
                   kc.related_docs, kc.related_examples,
                   kc.owner, kc.reviewed_by, kc.review_notes, kc.change_summary, kc.evidence_quality,
                   kc.applicable_versions, kc.lifecycle_status, kc.stale_score,
                   kc.last_evidence_at, kc.evidence_count, bm25(kc_fts) as bm25_rank
            FROM kc_fts fts
            JOIN knowledge_cards kc ON kc.id = fts.rowid
            WHERE kc_fts MATCH ?
        """
        params: list = [fts_q]
        if category:
            sql += " AND kc.category = ?"
            params.append(category)
        if source_id:
            sql += " AND kc.source_id = ?"
            params.append(source_id)
        sql += " ORDER BY bm25(kc_fts), kc.priority DESC, kc.stale_score ASC LIMIT ?"
        params.append(limit)

        try:
            rows = conn.execute(sql, params).fetchall()
            if rows:
                return self._rank_rows(
                    rows,
                    terms,
                    {
                        "title": 4.0,
                        "summary": 2.5,
                        "tags": 2.0,
                        "keywords": 1.8,
                        "aliases": 1.6,
                        "category": 1.1,
                    },
                    limit=limit,
                    priority_field="priority",
                    graph_field="slug",
                    stale_field="stale_score",
                    bm25_field="bm25_rank",
                    query_text=query,
                )
        except sqlite3.OperationalError:
            pass

        # LIKE fallback
        like = f"%{query}%"
        sql2 = """
            SELECT id, card_id, title, slug, category, summary, tags, keywords,
                   aliases, priority, source_id, source_path, related_docs, related_examples, owner, reviewed_by, review_notes, change_summary,
                   content_domain, visibility, stability, feature_tags, build_commit, build_time,
                   evidence_quality, applicable_versions, lifecycle_status, stale_score,
                   last_evidence_at, evidence_count, substr(content, 1, 220) as content_snippet
            FROM knowledge_cards
            WHERE (title LIKE ? OR summary LIKE ? OR tags LIKE ?
                   OR keywords LIKE ? OR aliases LIKE ? OR content LIKE ?)
        """
        params2: list = [like, like, like, like, like, like]
        if category:
            sql2 += " AND category = ?"
            params2.append(category)
        if source_id:
            sql2 += " AND source_id = ?"
            params2.append(source_id)
        sql2 += " ORDER BY priority DESC LIMIT ?"
        params2.append(limit)
        rows = conn.execute(sql2, params2).fetchall()
        return self._rank_rows(
            rows,
            terms,
            {
                "title": 4.0,
                "summary": 2.5,
                "tags": 2.0,
                "keywords": 1.8,
                "aliases": 1.6,
                "category": 1.1,
            },
            limit=limit,
            priority_field="priority",
            graph_field="slug",
            stale_field="stale_score",
            query_text=query,
        )

    def get_knowledge_card(self, slug_or_id: str) -> Optional[dict]:
        """Get a knowledge card by slug or card_id."""
        conn = self.connect()
        row = conn.execute(
            "SELECT * FROM knowledge_cards WHERE slug = ? OR card_id = ?",
            (slug_or_id, slug_or_id),
        ).fetchone()
        return dict(row) if row else None

    def get_all_knowledge_cards(self) -> list[dict]:
        """Get all knowledge cards (metadata only, no full content)."""
        conn = self.connect()
        rows = conn.execute(
            "SELECT id, card_id, title, slug, category, summary, tags, keywords, "
            "aliases, priority, source_id, source_path, content_domain, visibility, stability, feature_tags, build_commit, build_time, "
            "owner, reviewed_by, review_notes, change_summary, evidence_quality, "
            "applicable_versions, lifecycle_status, stale_score, last_evidence_at, evidence_count "
            "FROM knowledge_cards ORDER BY priority DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def explain_knowledge_card(self, slug_or_id: str, query: str = "",
                               relation_limit: int = 10) -> Optional[dict]:
        """Explain why a card is recommended, including score and evidence signals."""
        card = self.get_knowledge_card(slug_or_id)
        if not card:
            return None

        terms = self._expanded_terms(query) if query else []
        field_weights = {
            "title": 4.0,
            "summary": 2.5,
            "tags": 2.0,
            "keywords": 1.8,
            "aliases": 1.6,
            "category": 1.1,
            "content": 1.0,
        }
        relevance = 0.0
        for field, weight in field_weights.items():
            relevance += score_text_match(str(card.get(field, "")), terms) * weight
        priority_signal = float(card.get("priority", 0) or 0) / 20.0
        graph_signal = min(2.5, self._relation_signals([card["slug"]]).get(card["slug"], 0.0) / 2.5)
        freshness, stale_penalty, freshness_reasons = self._freshness_components(
            card,
            self._extract_query_versions(query),
        )
        final_score = round(relevance + priority_signal + graph_signal + freshness - stale_penalty, 4)
        relations = self.get_related_cards(card["slug"], limit=relation_limit)
        return {
            "card": card,
            "query": query,
            "score_breakdown": {
                "relevance": round(relevance, 4),
                "priority": round(priority_signal, 4),
                "graph_signal": round(graph_signal, 4),
                "freshness": round(freshness, 4),
                "stale_penalty": round(stale_penalty, 4),
                "final_score": final_score,
                "freshness_reasons": freshness_reasons,
            },
            "match_details": self._match_details(card, terms, field_weights),
            "relations": relations,
            "evidence": {
                "last_evidence_at": card.get("last_evidence_at", ""),
                "evidence_count": card.get("evidence_count", 0),
                "last_seen_commit": card.get("last_seen_commit", ""),
                "applicable_versions": card.get("applicable_versions", ""),
                "review_notes": card.get("review_notes", ""),
                "change_summary": card.get("change_summary", ""),
                "content_snippet": (card.get("content", "") or "")[:320],
            },
        }

    # ── Source Chunk methods ───────────────────────────────────────

    def clear_chunks(self, repo_id: Optional[str] = None) -> None:
        conn = self.connect()
        if repo_id:
            conn.execute("DELETE FROM source_chunks WHERE repo_id = ?", (repo_id,))
        else:
            conn.execute("DELETE FROM source_chunks")
        conn.commit()

    def clear_chunks_by_paths(self, repo_id: str, file_paths: list[str]) -> None:
        """Delete source chunks for a repo by file path."""
        if not file_paths:
            return
        conn = self.connect()
        conn.executemany(
            "DELETE FROM source_chunks WHERE repo_id = ? AND file_path = ?",
            [(repo_id, file_path) for file_path in file_paths],
        )
        conn.commit()

    def insert_chunk(self, chunk: dict) -> int:
        conn = self.connect()
        content_domain = self._metadata_text(chunk.get("content_domain", ""))
        inferred_domain = self._infer_content_domain(
            rel_path=str(chunk.get("file_path", "") or ""),
            title=str(chunk.get("title", "") or ""),
            chunk_type=str(chunk.get("chunk_type", "") or ""),
        )
        visibility = self._infer_visibility(chunk.get("visibility", ""))
        stability = self._infer_stability(
            chunk.get("stability", ""),
            rel_path=str(chunk.get("file_path", "") or ""),
            title=str(chunk.get("title", "") or ""),
        )
        feature_tags = self._merge_metadata_tags(chunk.get("tags", ""), chunk.get("feature_tags", ""))
        cur = conn.execute(
            """INSERT OR REPLACE INTO source_chunks
            (repo_id, file_path, chunk_type, title, content, language,
             start_line, end_line, symbols, tags, content_domain, visibility, stability,
             feature_tags, build_commit, build_time)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (chunk["repo_id"], chunk["file_path"], chunk["chunk_type"],
             chunk.get("title", ""), chunk.get("content", ""),
             chunk.get("language", ""), chunk.get("start_line", 0),
             chunk.get("end_line", 0), chunk.get("symbols", ""),
             chunk.get("tags", ""),
             content_domain or inferred_domain,
             visibility,
             stability,
             feature_tags,
             self._metadata_text(chunk.get("build_commit", "")),
             self._metadata_now(chunk.get("build_time", ""))),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore

    def get_chunks(self, repo_id: Optional[str] = None,
                   chunk_type: Optional[str] = None,
                   limit: int = 500) -> list[dict]:
        conn = self.connect()
        sql = "SELECT * FROM source_chunks WHERE 1=1"
        params: list = []
        if repo_id:
            sql += " AND repo_id = ?"
            params.append(repo_id)
        if chunk_type:
            sql += " AND chunk_type = ?"
            params.append(chunk_type)
        sql += " LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    # ── Candidate Card methods ─────────────────────────────────────

    def clear_candidates(self) -> None:
        conn = self.connect()
        conn.execute("DELETE FROM candidate_cards")
        conn.commit()

    def upsert_candidate(self, card: dict) -> int:
        conn = self.connect()
        content_domain = self._metadata_text(card.get("content_domain", ""))
        inferred_domain = self._infer_content_domain(
            rel_path=str(card.get("source_path", "") or ""),
            title=str(card.get("title", "") or ""),
            source_type="knowledge",
        )
        visibility = self._infer_visibility(card.get("visibility", ""), source_type="candidate")
        stability = self._infer_stability(
            card.get("stability", ""),
            rel_path=str(card.get("source_path", "") or ""),
            title=str(card.get("title", "") or ""),
            source_type="candidate",
        )
        feature_tags = self._merge_metadata_tags(card.get("tags", ""), card.get("keywords", ""), card.get("feature_tags", ""))
        build_commit = self._metadata_text(card.get("build_commit", ""))
        build_time = self._metadata_now(card.get("build_time", ""))
        columns = [
            "card_id", "title", "slug", "category", "summary", "content", "tags", "keywords",
            "aliases", "related_symbols", "related_docs", "related_examples", "source_type",
            "source_id", "content_domain", "visibility", "stability", "feature_tags", "build_commit",
            "build_time", "confidence", "status", "owner", "reviewed_by", "review_notes",
            "change_summary", "evidence_quality", "evidence", "applicable_versions",
            "lifecycle_status", "last_evidence_at", "last_seen_commit", "stale_score", "evidence_count",
        ]
        values = [
            card["card_id"],
            card["title"],
            card["slug"],
            card.get("category", ""),
            card.get("summary", ""),
            card.get("content", ""),
            card.get("tags", ""),
            card.get("keywords", ""),
            card.get("aliases", ""),
            card.get("related_symbols", ""),
            card.get("related_docs", ""),
            card.get("related_examples", ""),
            card.get("source_type", "generated"),
            card.get("source_id", ""),
            content_domain or inferred_domain,
            visibility,
            stability,
            feature_tags,
            build_commit,
            build_time,
            card.get("confidence", 0.0),
            card.get("status", "candidate"),
            card.get("owner", ""),
            card.get("reviewed_by", ""),
            card.get("review_notes", ""),
            card.get("change_summary", ""),
            card.get("evidence_quality", ""),
            card.get("evidence", ""),
            card.get("applicable_versions", ""),
            card.get("lifecycle_status", "active"),
            card.get("last_evidence_at", ""),
            card.get("last_seen_commit", ""),
            card.get("stale_score", 0.0),
            card.get("evidence_count", 0),
        ]
        cur = conn.execute(
            f"""INSERT OR REPLACE INTO candidate_cards
            ({', '.join(columns)})
            VALUES ({', '.join('?' for _ in columns)})""",
            values,
        )
        conn.commit()
        return cur.lastrowid  # type: ignore

    def get_candidates(self, status: Optional[str] = None,
                       category: Optional[str] = None,
                       source_id: Optional[str] = None,
                       limit: int = 100) -> list[dict]:
        conn = self.connect()
        sql = "SELECT * FROM candidate_cards WHERE 1=1"
        params: list = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if category:
            sql += " AND category = ?"
            params.append(category)
        if source_id:
            sql += " AND source_id = ?"
            params.append(source_id)
        sql += " ORDER BY confidence DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def get_candidate(self, slug_or_id: str) -> Optional[dict]:
        conn = self.connect()
        row = conn.execute(
            "SELECT * FROM candidate_cards WHERE slug = ? OR card_id = ?",
            (slug_or_id, slug_or_id),
        ).fetchone()
        return dict(row) if row else None

    def update_candidate_status(self, card_id: str, status: str) -> bool:
        conn = self.connect()
        ts_field = "reviewed_at" if status == "reviewed" else "published_at" if status == "published" else ""
        if ts_field:
            conn.execute(
                f"UPDATE candidate_cards SET status = ?, {ts_field} = datetime('now') WHERE card_id = ?",
                (status, card_id))
        else:
            conn.execute("UPDATE candidate_cards SET status = ? WHERE card_id = ?", (status, card_id))
        conn.commit()
        return conn.total_changes > 0

    def update_candidate_review(self, card_id: str,
                                approve: bool = True,
                                reviewer: str = "",
                                review_notes: str = "",
                                owner: str = "",
                                change_summary: str = "",
                                evidence_quality: str = "") -> bool:
        """Update review metadata while transitioning candidate state."""
        status = "reviewed" if approve else "rejected"
        conn = self.connect()
        ts_field = "reviewed_at" if approve else ""
        assignments = ["status = ?"]
        params: list = [status]
        if reviewer:
            assignments.append("reviewed_by = ?")
            params.append(reviewer)
        if review_notes:
            assignments.append("review_notes = ?")
            params.append(review_notes)
        if owner:
            assignments.append("owner = ?")
            params.append(owner)
        if change_summary:
            assignments.append("change_summary = ?")
            params.append(change_summary)
        if evidence_quality:
            assignments.append("evidence_quality = ?")
            params.append(evidence_quality)
        if ts_field:
            assignments.append(f"{ts_field} = datetime('now')")
        params.append(card_id)
        conn.execute(
            f"UPDATE candidate_cards SET {', '.join(assignments)} WHERE card_id = ? OR slug = ?",
            (*params, card_id),
        )
        conn.commit()
        return conn.total_changes > 0

    def search_candidates(self, query: str, limit: int = 20) -> list[dict]:
        conn = self.connect()
        fts_q = build_fts_or_query(query)
        terms = self._expanded_terms(query)
        try:
            rows = conn.execute(
                """SELECT cc.*, bm25(cc_fts) as bm25_rank
                FROM cc_fts fts JOIN candidate_cards cc ON cc.id = fts.rowid
                WHERE cc_fts MATCH ? ORDER BY bm25(cc_fts), cc.confidence DESC LIMIT ?""",
                (fts_q, limit)).fetchall()
            if rows:
                return self._rank_rows(
                    rows,
                    terms,
                    {
                        "title": 4.0,
                        "summary": 2.5,
                        "tags": 2.0,
                        "keywords": 1.5,
                        "aliases": 1.5,
                        "category": 1.0,
                    },
                    limit=limit,
                    priority_field="confidence",
                    priority_scale=0.35,
                    graph_field="slug",
                    stale_field="stale_score",
                    bm25_field="bm25_rank",
                    query_text=query,
                )
        except Exception:
            pass
        like = f"%{query}%"
        rows = conn.execute(
            """SELECT * FROM candidate_cards
            WHERE title LIKE ? OR summary LIKE ? OR tags LIKE ? OR keywords LIKE ?
            OR aliases LIKE ? OR content LIKE ?
            ORDER BY confidence DESC LIMIT ?""",
            (like, like, like, like, like, like, limit)).fetchall()
        return self._rank_rows(
            rows,
            terms,
            {
                "title": 4.0,
                "summary": 2.5,
                "tags": 2.0,
                "keywords": 1.5,
                "aliases": 1.5,
                "category": 1.0,
            },
            limit=limit,
            priority_field="confidence",
            priority_scale=0.35,
            graph_field="slug",
            stale_field="stale_score",
            query_text=query,
        )

    # ── Card Relations methods ─────────────────────────────────────

    def clear_relations(self) -> None:
        conn = self.connect()
        conn.execute("DELETE FROM card_relations")
        conn.commit()

    def upsert_relation(self, source: str, target: str,
                        relation_type: str, weight: float = 1.0,
                        evidence: str = "") -> int:
        conn = self.connect()
        cur = conn.execute(
            """INSERT OR REPLACE INTO card_relations
            (source_slug, target_slug, relation_type, weight, evidence)
            VALUES (?,?,?,?,?)""",
            (source, target, relation_type, weight, evidence))
        conn.commit()
        return cur.lastrowid  # type: ignore

    def get_related_cards(self, slug: str, limit: int = 20) -> list[dict]:
        """Get all cards related to a given slug (both directions)."""
        conn = self.connect()
        rows = conn.execute(
            """SELECT cr.*, 'outgoing' as direction
            FROM card_relations cr WHERE cr.source_slug = ?
            UNION ALL
            SELECT cr.*, 'incoming' as direction
            FROM card_relations cr WHERE cr.target_slug = ?
            ORDER BY weight DESC LIMIT ?""",
            (slug, slug, limit)).fetchall()
        return [dict(r) for r in rows]

    def get_all_relations(self) -> list[dict]:
        conn = self.connect()
        rows = conn.execute(
            "SELECT * FROM card_relations ORDER BY weight DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def list_knowledge_cards(self, category: Optional[str] = None,
                             lifecycle_status: Optional[str] = None,
                             source_id: Optional[str] = None,
                             limit: int = 100) -> list[dict]:
        """List published knowledge cards with optional filters."""
        conn = self.connect()
        sql = """SELECT id, card_id, title, slug, category, summary, tags, keywords,
                        aliases, priority, source_id, source_path, content_domain, visibility, stability,
                        feature_tags, build_commit, build_time, applicable_versions, lifecycle_status,
                        stale_score, last_evidence_at, last_seen_commit, evidence_count,
                        related_docs, related_examples, owner, reviewed_by, review_notes,
                        change_summary, evidence_quality, content
                 FROM knowledge_cards WHERE 1=1"""
        params: list = []
        if category:
            sql += " AND category = ?"
            params.append(category)
        if lifecycle_status:
            sql += " AND lifecycle_status = ?"
            params.append(lifecycle_status)
        if source_id:
            sql += " AND source_id = ?"
            params.append(source_id)
        sql += " ORDER BY stale_score ASC, priority DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def update_card_health(self, slug: str, table: str = "knowledge_cards",
                           stale_score: float = 0.0,
                           lifecycle_status: str = "active",
                           last_evidence_at: str = "",
                           last_seen_commit: str = "",
                           applicable_versions: str = "",
                           evidence_count: int = 0) -> bool:
        """Update freshness/version metadata for knowledge or candidate cards."""
        if table not in {"knowledge_cards", "candidate_cards"}:
            raise ValueError(f"Unsupported table: {table}")
        conn = self.connect()
        conn.execute(
            f"""UPDATE {table}
                SET stale_score = ?,
                    lifecycle_status = ?,
                    last_evidence_at = ?,
                    last_seen_commit = ?,
                    applicable_versions = ?,
                    evidence_count = ?
                WHERE slug = ?""",
            (
                stale_score,
                lifecycle_status,
                last_evidence_at,
                last_seen_commit,
                applicable_versions,
                evidence_count,
                slug,
            ),
        )
        conn.commit()
        return conn.total_changes > 0

    def upsert_repo_head(self, repo_id: str, commit_hash: str) -> None:
        """Record the latest observed git HEAD for a repository."""
        conn = self.connect()
        conn.execute(
            """INSERT OR REPLACE INTO repo_heads (repo_id, commit_hash, recorded_at)
               VALUES (?, ?, datetime('now'))""",
            (repo_id, commit_hash),
        )
        conn.commit()

    def get_repo_heads(self) -> dict[str, str]:
        """Return the most recent recorded HEAD hash per repo."""
        conn = self.connect()
        rows = conn.execute("SELECT repo_id, commit_hash FROM repo_heads").fetchall()
        return {row["repo_id"]: row["commit_hash"] for row in rows}

    def snapshot_repo_heads(self) -> dict[str, str]:
        """Refresh git HEAD hashes for all configured repositories when possible."""
        current: dict[str, str] = {}
        for repo in self.get_all_repos():
            repo_path = repo.get("path", "")
            if not repo_path:
                continue
            try:
                result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
            if result.returncode != 0:
                continue
            commit_hash = result.stdout.strip()
            if commit_hash:
                current[repo["id"]] = commit_hash
                self.upsert_repo_head(repo["id"], commit_hash)
        return current

    def log_tool_telemetry(self, tool_name: str, status: str,
                           latency_ms: float,
                           query_text: str = "",
                           details: Optional[dict] = None) -> None:
        """Persist a tool invocation for later feedback analysis."""
        conn = self.connect()
        details_json = ""
        if details:
            details_json = json.dumps(details, ensure_ascii=False)[:2000]
        conn.execute(
            """INSERT INTO tool_telemetry (tool_name, status, latency_ms, query_text, details)
               VALUES (?, ?, ?, ?, ?)""",
            (tool_name, status, latency_ms, query_text[:500], details_json),
        )
        conn.commit()

    def get_tool_telemetry(self, limit: int = 500,
                           tool_name: Optional[str] = None) -> list[dict]:
        """Return recent telemetry rows with parsed detail payloads."""
        conn = self.connect()
        sql = """SELECT id, tool_name, status, latency_ms, query_text, details, created_at
                 FROM tool_telemetry WHERE 1=1"""
        params: list = []
        if tool_name:
            sql += " AND tool_name = ?"
            params.append(tool_name)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        parsed = []
        for row in rows:
            item = dict(row)
            raw_details = item.get("details", "") or ""
            try:
                item["details"] = json.loads(raw_details) if raw_details else {}
            except json.JSONDecodeError:
                item["details"] = {}
            parsed.append(item)
        return parsed

    def get_tool_feedback_summary(self, limit: int = 10) -> dict:
        """Summarize telemetry with focus on no-match and error signals."""
        conn = self.connect()
        tool_rows = conn.execute(
            """
            SELECT tool_name,
                   COUNT(*) AS total_calls,
                   ROUND(AVG(latency_ms), 2) AS avg_latency_ms,
                   SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok_calls,
                   SUM(CASE WHEN status IN ('no_match', 'no_results', 'partial_match') THEN 1 ELSE 0 END) AS weak_calls,
                   SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_calls
            FROM tool_telemetry
            GROUP BY tool_name
            ORDER BY total_calls DESC, error_calls DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        weak_rows = conn.execute(
            """
            SELECT tool_name, status, query_text, created_at
            FROM tool_telemetry
            WHERE status IN ('no_match', 'no_results', 'partial_match', 'error')
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        telemetry = self.get_tool_telemetry(limit=500)
        false_positive_by_tool: dict[str, int] = {}
        false_positive_by_reason: dict[str, int] = {}
        false_positive_by_category: dict[str, int] = {}
        for record in telemetry:
            details = record.get("details", {}) or {}
            if record.get("status") == "partial_match" or details.get("false_positive_signal"):
                tool_name = str(record.get("tool_name", "") or "unknown")
                false_positive_by_tool[tool_name] = false_positive_by_tool.get(tool_name, 0) + 1
            for reason in details.get("false_positive_reasons", []):
                false_positive_by_reason[reason] = false_positive_by_reason.get(reason, 0) + 1
            for category in details.get("selected_categories", []):
                if details.get("false_positive_signal"):
                    key = category or "unknown"
                    false_positive_by_category[key] = false_positive_by_category.get(key, 0) + 1
        return {
            "tools": [dict(r) for r in tool_rows],
            "recent_weak_signals": [dict(r) for r in weak_rows],
            "false_positive_by_tool": sorted(
                ({"name": key, "count": value} for key, value in false_positive_by_tool.items()),
                key=lambda item: (-item["count"], item["name"]),
            )[:limit],
            "false_positive_by_reason": sorted(
                ({"name": key, "count": value} for key, value in false_positive_by_reason.items()),
                key=lambda item: (-item["count"], item["name"]),
            )[:limit],
            "false_positive_by_category": sorted(
                ({"name": key, "count": value} for key, value in false_positive_by_category.items()),
                key=lambda item: (-item["count"], item["name"]),
            )[:limit],
        }

    def trace_knowledge_path(self, source_slug: str, target_slug: str,
                             max_depth: int = 3) -> list[dict]:
        """Trace a short undirected path between two cards via the relation graph."""
        if source_slug == target_slug:
            return []

        adjacency: dict[str, list[dict]] = {}
        for relation in self.get_all_relations():
            adjacency.setdefault(relation["source_slug"], []).append({
                "neighbor": relation["target_slug"],
                "relation_type": relation["relation_type"],
                "weight": relation["weight"],
            })
            adjacency.setdefault(relation["target_slug"], []).append({
                "neighbor": relation["source_slug"],
                "relation_type": relation["relation_type"],
                "weight": relation["weight"],
            })

        queue: deque[tuple[str, list[dict]]]= deque([(source_slug, [])])
        visited = {source_slug}
        while queue:
            slug, path = queue.popleft()
            if len(path) >= max_depth:
                continue
            for edge in adjacency.get(slug, []):
                neighbor = edge["neighbor"]
                next_path = path + [{
                    "source_slug": slug,
                    "target_slug": neighbor,
                    "relation_type": edge["relation_type"],
                    "weight": edge["weight"],
                }]
                if neighbor == target_slug:
                    return next_path
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, next_path))
        return []
