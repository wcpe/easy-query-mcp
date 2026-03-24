"""Knowledge mining: chunk splitting, topic mining, candidate card generation."""

import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from .database import Database
from .knowledge_health import format_commit_map


# ── Topic pattern loading ─────────────────────────────────────────

CONTENT_DOMAINS = ("api", "example", "test", "doc", "migration", "faq", "source", "knowledge")
VISIBILITY_LEVELS = ("public", "internal", "private", "experimental")
STABILITY_LEVELS = ("stable", "experimental", "legacy", "deprecated")

_TOPIC_RULES_CACHE: list[dict] | None = None

_FEATURE_STOPWORDS = {
    "src", "main", "test", "tests", "testing", "java", "kotlin", "kt", "kts",
    "md", "mdx", "txt", "markdown", "docs", "doc", "wiki", "resources",
    "build", "out", "target", "impl", "internal", "sample", "samples", "demo",
}

_DEFAULT_TOPIC_RULES: list[dict] = [
    {
        "slug": "entity-mapping",
        "category": "mapping",
        "confidence": 0.72,
        "canonical_topic": "entity-mapping",
        "content_domains": ["source", "example", "doc", "knowledge"],
        "visibility": "public",
        "stability": "stable",
        "feature_tags": ["entity-mapping", "table", "column", "entity-proxy"],
        "pattern": re.compile(r"@Table|@Column|EntityConfigure|TableAvailable|EntityProxy", re.I),
    },
    {
        "slug": "queryable-api",
        "category": "query",
        "confidence": 0.75,
        "canonical_topic": "queryable-api",
        "content_domains": ["api", "example", "doc", "knowledge"],
        "visibility": "public",
        "stability": "stable",
        "feature_tags": ["queryable", "where", "select", "join", "group-by", "order-by"],
        "pattern": re.compile(r"queryable|where\s*\(|select\s*\(|leftJoin\s*\(|groupBy\s*\(|orderBy\s*\(", re.I),
    },
    {
        "slug": "repository-pattern",
        "category": "architecture",
        "confidence": 0.68,
        "canonical_topic": "repository-pattern",
        "content_domains": ["source", "knowledge", "doc"],
        "visibility": "public",
        "stability": "stable",
        "feature_tags": ["repository", "query-repository", "jdbc-repository", "easy-entity-query"],
        "pattern": re.compile(r"Repository|QueryRepository|JdbcRepository|EasyEntityQuery|DbContext", re.I),
    },
    {
        "slug": "transaction-boundary",
        "category": "transaction",
        "confidence": 0.74,
        "canonical_topic": "transaction",
        "content_domains": ["source", "example", "doc", "knowledge"],
        "visibility": "public",
        "stability": "stable",
        "feature_tags": ["transaction", "rollback", "commit", "transactional"],
        "pattern": re.compile(r"transaction|beginTransaction|commit|rollback|Transactional", re.I),
    },
    {
        "slug": "dynamic-condition",
        "category": "query",
        "confidence": 0.72,
        "canonical_topic": "dynamic-condition",
        "content_domains": ["api", "example", "doc", "knowledge"],
        "visibility": "public",
        "stability": "stable",
        "feature_tags": ["dynamic-condition", "conditional-filter", "predicate", "where-if"],
        "pattern": re.compile(r"whereIf|filterIf|dynamic[ -]?condition|动态条件|条件构造|predicate|conditional query", re.I),
    },
    {
        "slug": "h2-concurrency-testing",
        "category": "testing",
        "confidence": 0.7,
        "canonical_topic": "test-fixture",
        "content_domains": ["test", "example", "doc", "knowledge"],
        "visibility": "public",
        "stability": "stable",
        "feature_tags": ["h2", "concurrency-test", "transaction-test", "in-memory"],
        "pattern": re.compile(r"H2|in.?memory.*database|DB_CLOSE_DELAY|concurrent.*test.*db", re.I),
    },
    {
        "slug": "paging-query",
        "category": "paging",
        "confidence": 0.7,
        "canonical_topic": "pagination",
        "content_domains": ["api", "example", "doc", "knowledge"],
        "visibility": "public",
        "stability": "stable",
        "feature_tags": ["pagination", "paging", "page-result", "page-size"],
        "pattern": re.compile(r"toPageResult|PageResult|Pager|pageResult|pageIndex|pageSize|分页", re.I),
    },
    {
        "slug": "code-first",
        "category": "schema",
        "confidence": 0.7,
        "canonical_topic": "code-first",
        "content_domains": ["source", "doc", "knowledge"],
        "visibility": "public",
        "stability": "stable",
        "feature_tags": ["code-first", "migration", "schema", "ddl", "auto-migrate"],
        "pattern": re.compile(r"CodeFirst|autoMigrate|Migration|schema|DDL|Code First|代码优先", re.I),
    },
    {
        "slug": "logic-delete",
        "category": "mapping",
        "confidence": 0.68,
        "canonical_topic": "logic-delete",
        "content_domains": ["source", "example", "doc", "knowledge"],
        "visibility": "public",
        "stability": "stable",
        "feature_tags": ["logic-delete", "soft-delete", "deleted", "is-deleted"],
        "pattern": re.compile(r"logicDelete|LogicDelete|deleted|isDeleted|逻辑删除", re.I),
    },
    {
        "slug": "value-converter",
        "category": "mapping",
        "confidence": 0.66,
        "canonical_topic": "value-converter",
        "content_domains": ["source", "example", "doc", "knowledge"],
        "visibility": "public",
        "stability": "stable",
        "feature_tags": ["value-converter", "property-converter", "json-column"],
        "pattern": re.compile(r"ValueConverter|PropertyConverter|JSONColumn|serialize|deserialize", re.I),
    },
    {
        "slug": "sharding-routing",
        "category": "sharding",
        "confidence": 0.7,
        "canonical_topic": "sharding",
        "content_domains": ["source", "doc", "knowledge"],
        "visibility": "public",
        "stability": "stable",
        "feature_tags": ["sharding", "table-route", "data-source-route", "partition"],
        "pattern": re.compile(r"Sharding|TableRoute|DataSourceRoute|route|partition|分片", re.I),
    },
]


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _normalize_tag(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "-", text)
    return text.strip("-")


def _split_terms(text: str) -> list[str]:
    raw_terms = re.findall(r"[A-Za-z][A-Za-z0-9]*|[\u4e00-\u9fff]{2,}", text or "")
    normalized = []
    for term in raw_terms:
        token = _normalize_tag(term)
        if not token or token in _FEATURE_STOPWORDS:
            continue
        if len(token) <= 2 and not re.fullmatch(r"[\u4e00-\u9fff]{2,}", token):
            continue
        normalized.append(token)
    return normalized


def _compile_topic_rule(slug: str, cfg: dict) -> dict:
    keyword_parts: list[str] = []
    for key in ("keywords", "aliases", "title_keywords", "path_keywords"):
        values = cfg.get(key, [])
        if isinstance(values, str):
            values = [values]
        for value in values or []:
            text = str(value).strip()
            if text:
                keyword_parts.append(text)

    if not keyword_parts:
        return {}

    content_domains = cfg.get("content_domains", [])
    if isinstance(content_domains, str):
        content_domains = [content_domains]

    feature_tags = cfg.get("feature_tags", [])
    if isinstance(feature_tags, str):
        feature_tags = [feature_tags]

    canonical_topic = str(cfg.get("canonical_topic") or slug).strip() or slug
    visibility = str(cfg.get("visibility", "public")).strip().lower() or "public"
    stability = str(cfg.get("stability", "stable")).strip().lower() or "stable"

    return {
        "slug": slug,
        "category": str(cfg.get("category", "general")).strip() or "general",
        "confidence": float(cfg.get("confidence", 0.5)),
        "canonical_topic": canonical_topic,
        "content_domains": [str(item).strip().lower() for item in content_domains if str(item).strip()],
        "visibility": visibility,
        "stability": stability,
        "feature_tags": _unique([
            _normalize_tag(item) for item in feature_tags if _normalize_tag(item)
        ]),
        "pattern": re.compile("|".join(keyword_parts), re.IGNORECASE),
    }


def _load_topic_patterns() -> list[dict]:
    """Load topic patterns from config/topic_patterns.yaml, fallback to built-in."""
    global _TOPIC_RULES_CACHE
    if _TOPIC_RULES_CACHE is not None:
        return _TOPIC_RULES_CACHE

    from .config import _find_project_root

    yaml_path = _find_project_root() / "config" / "topic_patterns.yaml"
    if yaml_path.exists():
        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            patterns: list[dict] = []
            for slug, cfg in raw.get("patterns", {}).items():
                rule = _compile_topic_rule(slug, cfg or {})
                if rule:
                    patterns.append(rule)
            if patterns:
                print(f"[MINE] Loaded {len(patterns)} topic patterns from {yaml_path}",
                      file=sys.stderr)
                _TOPIC_RULES_CACHE = patterns
                return patterns
        except Exception as e:
            print(f"[WARN] Failed to load topic_patterns.yaml: {e}", file=sys.stderr)

    # Fallback to built-in patterns
    _TOPIC_RULES_CACHE = _BUILTIN_PATTERNS
    return _BUILTIN_PATTERNS


_BUILTIN_PATTERNS: list[dict] = [
    {
        "slug": "entity-mapping",
        "category": "mapping",
        "confidence": 0.72,
        "canonical_topic": "entity-mapping",
        "content_domains": ["source", "example", "doc", "knowledge"],
        "visibility": "public",
        "stability": "stable",
        "feature_tags": ["entity-mapping", "table", "column", "entity-proxy"],
        "pattern": re.compile(r"@Table|@Column|EntityConfigure|TableAvailable|EntityProxy", re.I),
    },
    {
        "slug": "queryable-api",
        "category": "query",
        "confidence": 0.75,
        "canonical_topic": "queryable-api",
        "content_domains": ["api", "example", "doc", "knowledge"],
        "visibility": "public",
        "stability": "stable",
        "feature_tags": ["queryable", "where", "select", "join", "group-by", "order-by"],
        "pattern": re.compile(r"queryable|where\s*\(|select\s*\(|leftJoin\s*\(|groupBy\s*\(", re.I),
    },
    {
        "slug": "repository-pattern",
        "category": "architecture",
        "confidence": 0.68,
        "canonical_topic": "repository-pattern",
        "content_domains": ["source", "knowledge", "doc"],
        "visibility": "public",
        "stability": "stable",
        "feature_tags": ["repository", "query-repository", "jdbc-repository", "easy-entity-query"],
        "pattern": re.compile(r"Repository|QueryRepository|EasyEntityQuery|DbContext", re.I),
    },
    {
        "slug": "transaction-boundary",
        "category": "transaction",
        "confidence": 0.74,
        "canonical_topic": "transaction",
        "content_domains": ["source", "example", "doc", "knowledge"],
        "visibility": "public",
        "stability": "stable",
        "feature_tags": ["transaction", "rollback", "commit", "transactional"],
        "pattern": re.compile(r"transaction|beginTransaction|commit|rollback|Transactional", re.I),
    },
    {
        "slug": "dynamic-condition",
        "category": "query",
        "confidence": 0.72,
        "canonical_topic": "dynamic-condition",
        "content_domains": ["api", "example", "doc", "knowledge"],
        "visibility": "public",
        "stability": "stable",
        "feature_tags": ["dynamic-condition", "conditional-filter", "predicate", "where-if"],
        "pattern": re.compile(r"whereIf|filterIf|dynamic[ -]?condition|动态条件|条件构造|predicate|conditional query", re.I),
    },
    {
        "slug": "h2-concurrency-testing",
        "category": "testing",
        "confidence": 0.65,
        "canonical_topic": "test-fixture",
        "content_domains": ["test", "example", "doc", "knowledge"],
        "visibility": "public",
        "stability": "stable",
        "feature_tags": ["h2", "concurrency-test", "transaction-test", "in-memory"],
        "pattern": re.compile(r"H2|in.?memory.*database|DB_CLOSE_DELAY|concurrent.*test.*db", re.I),
    },
    {
        "slug": "paging-query",
        "category": "paging",
        "confidence": 0.7,
        "canonical_topic": "pagination",
        "content_domains": ["api", "example", "doc", "knowledge"],
        "visibility": "public",
        "stability": "stable",
        "feature_tags": ["pagination", "paging", "page-result", "page-size"],
        "pattern": re.compile(r"toPageResult|PageResult|Pager|pageIndex|pageSize|分页", re.I),
    },
    {
        "slug": "code-first",
        "category": "schema",
        "confidence": 0.7,
        "canonical_topic": "code-first",
        "content_domains": ["source", "doc", "knowledge"],
        "visibility": "public",
        "stability": "stable",
        "feature_tags": ["code-first", "migration", "schema", "ddl", "auto-migrate"],
        "pattern": re.compile(r"CodeFirst|autoMigrate|Migration|schema|DDL|Code First|代码优先", re.I),
    },
    {
        "slug": "logic-delete",
        "category": "mapping",
        "confidence": 0.68,
        "canonical_topic": "logic-delete",
        "content_domains": ["source", "example", "doc", "knowledge"],
        "visibility": "public",
        "stability": "stable",
        "feature_tags": ["logic-delete", "soft-delete", "deleted", "is-deleted"],
        "pattern": re.compile(r"logicDelete|LogicDelete|deleted|isDeleted|逻辑删除", re.I),
    },
    {
        "slug": "value-converter",
        "category": "mapping",
        "confidence": 0.66,
        "canonical_topic": "value-converter",
        "content_domains": ["source", "example", "doc", "knowledge"],
        "visibility": "public",
        "stability": "stable",
        "feature_tags": ["value-converter", "property-converter", "json-column"],
        "pattern": re.compile(r"ValueConverter|PropertyConverter|JSONColumn|serialize|deserialize", re.I),
    },
    {
        "slug": "sharding-routing",
        "category": "sharding",
        "confidence": 0.7,
        "canonical_topic": "sharding",
        "content_domains": ["source", "doc", "knowledge"],
        "visibility": "public",
        "stability": "stable",
        "feature_tags": ["sharding", "table-route", "data-source-route", "partition"],
        "pattern": re.compile(r"Sharding|TableRoute|DataSourceRoute|partition|route", re.I),
    },
]


def _match_topic_rules(text: str, path: str = "", title: str = "") -> list[dict]:
    """Return topic rule matches ordered by confidence and evidence."""
    haystacks = [
        ("title", title or "", 2.0),
        ("path", path or "", 1.6),
        ("content", text or "", 1.0),
    ]
    matched: list[dict] = []
    for rule in _load_topic_patterns():
        pattern = rule.get("pattern")
        if pattern is None:
            continue
        evidence_sources: list[str] = []
        score = 0.0
        hit_count = 0.0
        for source_name, haystack, weight in haystacks:
            if not haystack:
                continue
            hits = pattern.findall(haystack)
            if not hits:
                continue
            evidence_sources.append(source_name)
            hit_count += float(len(hits)) * weight
        if not evidence_sources:
            continue
        score = float(rule.get("confidence", 0.5))
        score += min(hit_count / 10.0, 0.25)
        if "title" in evidence_sources:
            score += 0.08
        if "path" in evidence_sources:
            score += 0.05
        matched.append({
            **rule,
            "score": round(score, 4),
            "hit_count": round(hit_count, 4),
            "evidence_sources": evidence_sources,
        })
    matched.sort(
        key=lambda item: (
            float(item.get("score", 0.0)),
            float(item.get("hit_count", 0.0)),
            str(item.get("slug", "")),
        ),
        reverse=True,
    )
    return matched


def _infer_content_domain(path: str = "", title: str = "", content: str = "",
                          source_type: str = "source", is_example: bool = False) -> str:
    """Infer the primary content domain from path, title and source type."""
    normalized_path = (path or "").replace("\\", "/").lower()
    normalized_title = (title or "").lower()
    combined = f"{normalized_path} {normalized_title}"
    content_lower = (content or "").lower()

    if source_type == "knowledge":
        return "knowledge"
    if source_type in {"docs", "wiki"}:
        if any(token in combined for token in ("faq", "question", "qna", "common-questions")):
            return "faq"
        if any(token in combined for token in ("api", "reference", "contract", "endpoint")):
            return "api"
        if any(token in combined for token in ("migration", "migrate", "upgrade", "schema", "ddl")):
            return "migration"
        return "doc"

    if is_example or any(token in combined for token in ("example", "examples", "demo", "sample", "samples")):
        return "example"
    if any(token in combined for token in ("test", "tests", "spec", "specs", "it")):
        return "test"
    if any(token in combined for token in ("migration", "migrate", "upgrade", "schema", "ddl", "codefirst", "code-first")):
        return "migration"
    if any(token in combined for token in ("faq", "question", "qna")):
        return "faq"
    if any(token in combined for token in ("api", "reference", "contract", "endpoint")):
        return "api"
    if any(token in content_lower for token in ("codefirst", "migration", "migrate", "schema", "ddl")):
        return "migration"
    return "source"


def _infer_visibility(path: str = "", title: str = "", content: str = "") -> str:
    """Infer whether content is public, internal, private, or experimental."""
    path_title = f"{path} {title}".lower()
    normalized = f"{path_title} {content}".lower()
    if any(token in path_title for token in ("knowledge_cards", "docs", "wiki", "faq", "guide", "reference")):
        return "public"
    if any(token in normalized for token in ("experimental", "beta", "preview", "draft", "wip")):
        return "experimental"
    if any(token in path_title for token in ("private", "/private/", "private ", "private:")):
        return "private"
    if any(token in path_title for token in ("internal", "/internal/", "internal ", "impl", "implementation detail")):
        return "internal"
    return "public"


def _infer_stability(path: str = "", title: str = "", content: str = "") -> str:
    """Infer the stability level of a piece of content."""
    normalized = f"{path} {title} {content}".lower()
    if any(token in normalized for token in ("deprecated", "obsolete", "removed", "retired")):
        return "deprecated"
    if any(token in normalized for token in ("legacy", "old api", "old-api", "compatibility mode")):
        return "legacy"
    if any(token in normalized for token in ("experimental", "beta", "preview", "wip", "draft")):
        return "experimental"
    return "stable"


def classify_content_metadata(path: str = "", title: str = "", content: str = "",
                              source_type: str = "source",
                              is_example: bool = False) -> dict:
    """Return the searchable metadata envelope for a chunk or file."""
    domain = _infer_content_domain(path, title, content, source_type=source_type, is_example=is_example)
    visibility = _infer_visibility(path, title, content)
    stability = _infer_stability(path, title, content)
    topic_matches = _match_topic_rules(content or "", path=path, title=title)
    canonical_topics: list[str] = []
    feature_tags: list[str] = []

    for match in topic_matches:
        canonical_topic = str(match.get("canonical_topic") or match.get("slug") or "").strip()
        if canonical_topic:
            canonical_topics.append(_normalize_tag(canonical_topic))
        feature_tags.extend(str(tag) for tag in match.get("feature_tags", []) if str(tag).strip())
        feature_tags.append(str(match.get("slug") or "").strip())
        feature_tags.append(canonical_topic)

    feature_tags.extend(_split_terms(path))
    feature_tags.extend(_split_terms(title))
    if domain:
        feature_tags.append(domain)

    metadata = {
        "content_domain": domain,
        "visibility": visibility,
        "stability": stability,
        "feature_tags": _unique([
            _normalize_tag(tag) for tag in feature_tags if _normalize_tag(tag)
        ]),
        "canonical_topics": _unique([
            _normalize_tag(topic) for topic in canonical_topics if _normalize_tag(topic)
        ]),
        "topic_matches": topic_matches,
    }
    return metadata


def materialize_metadata_tags(metadata: dict, base_tags: list[str] | None = None) -> list[str]:
    """Render a metadata envelope into tag strings for indexed text fields."""
    tags: list[str] = list(base_tags or [])
    content_domain = str(metadata.get("content_domain", "")).strip()
    visibility = str(metadata.get("visibility", "")).strip()
    stability = str(metadata.get("stability", "")).strip()
    if content_domain:
        tags.append(f"domain:{content_domain}")
    if visibility:
        tags.append(f"visibility:{visibility}")
    if stability:
        tags.append(f"stability:{stability}")
    for tag in metadata.get("feature_tags", []) or []:
        normalized = _normalize_tag(str(tag))
        if normalized:
            tags.append(normalized)
    for topic in metadata.get("canonical_topics", []) or []:
        normalized = _normalize_tag(str(topic))
        if normalized:
            tags.append(f"topic:{normalized}")
    return _unique(tags)


def render_metadata_block(metadata: dict) -> str:
    """Render a compact markdown metadata block for searchable content."""
    parts = ["## Metadata"]
    parts.append(f"- content_domain: {metadata.get('content_domain', '')}")
    parts.append(f"- visibility: {metadata.get('visibility', '')}")
    parts.append(f"- stability: {metadata.get('stability', '')}")
    if metadata.get("build_commit"):
        parts.append(f"- build_commit: {metadata.get('build_commit', '')}")
    if metadata.get("build_time"):
        parts.append(f"- build_time: {metadata.get('build_time', '')}")
    if metadata.get("canonical_topics"):
        parts.append("- canonical_topics: " + ", ".join(metadata.get("canonical_topics", [])))
    if metadata.get("feature_tags"):
        parts.append("- feature_tags: " + ", ".join(metadata.get("feature_tags", [])[:12]))
    return "\n".join(parts)


def _dominant_value(counter: object, fallback: str, preferred_order: tuple[str, ...] = ()) -> str:
    """Pick the strongest value from a counter-like object, with optional tie-breaking order."""
    if not counter:
        return fallback
    if isinstance(counter, set):
        values = [(str(item), 1) for item in counter if str(item).strip()]
    elif isinstance(counter, (list, tuple)):
        temp = Counter(str(item) for item in counter if str(item).strip())
        values = list(temp.items())
    else:
        values = list(counter.items())
    preferred_rank = {value: index for index, value in enumerate(preferred_order)}
    ranked = sorted(
        values,
        key=lambda item: (
            item[1],
            -preferred_rank.get(str(item[0]), len(preferred_rank)),
            str(item[0]),
        ),
        reverse=True,
    )
    return str(ranked[0][0]) if ranked else fallback

# Chunk size limits
MAX_CHUNK_LINES = 120
MIN_CHUNK_LINES = 5


class KnowledgeMiner:
    """Mines source chunks and generates candidate knowledge cards."""

    def __init__(self, db: Database):
        self.db = db
        self.topic_patterns = _load_topic_patterns()

    def mine_all(self, repo_id: Optional[str] = None,
                 changed_only: bool = False,
                 since_commit: Optional[str] = None) -> dict:
        """Full mining pipeline: chunk -> topic -> candidate.

        Args:
            repo_id: Only mine from this repo
            changed_only: Only process files changed since last commit
            since_commit: Only process files changed since this commit hash
        """
        stats = {"chunks": 0, "topics": 0, "candidates": 0, "relations": 0}

        # Determine changed file filter
        changed_files: Optional[set[str]] = None
        if changed_only or since_commit:
            changed_files = self._get_changed_files(repo_id, since_commit)
            if changed_files is not None:
                print(f"[MINE] Incremental mode: {len(changed_files)} changed files",
                      file=sys.stderr)

        # Step 1: Build source chunks
        print("[MINE] Step 1: Building source chunks...", file=sys.stderr)
        chunks = self._build_chunks(repo_id, changed_files=changed_files)
        stats["chunks"] = len(chunks)
        print(f"  {len(chunks)} chunks extracted", file=sys.stderr)

        print("[MINE] Step 1.5: Persisting source chunks...", file=sys.stderr)
        self._persist_chunks(chunks, repo_id=repo_id, changed_files=changed_files)

        # Step 2: Mine topics from chunks
        print("[MINE] Step 2: Mining topics...", file=sys.stderr)
        topics = self._mine_topics(chunks)
        stats["topics"] = len(topics)
        print(f"  {len(topics)} topics identified", file=sys.stderr)

        # Step 3: Generate candidate cards
        print("[MINE] Step 3: Generating candidate cards...", file=sys.stderr)
        repo_heads = self.db.snapshot_repo_heads()
        candidates = self._generate_candidates(topics, repo_heads)
        stats["candidates"] = len(candidates)
        print(f"  {len(candidates)} candidate cards generated", file=sys.stderr)

        # Step 4: Persist
        for c in candidates:
            self.db.upsert_candidate(c)

        # Step 5: Mine relations between cards
        print("[MINE] Step 4: Mining card relations...", file=sys.stderr)
        rel_count = self.mine_relations()
        stats["relations"] = rel_count

        return stats

    def _persist_chunks(self, chunks: list[dict],
                        repo_id: Optional[str] = None,
                        changed_files: Optional[set[str]] = None) -> None:
        """Persist chunk metadata for later retrieval and audits."""
        if changed_files is None:
            self.db.clear_chunks(repo_id)
        else:
            paths_by_repo: dict[str, set[str]] = defaultdict(set)
            for key in changed_files:
                if ":" not in key:
                    continue
                changed_repo, rel_path = key.split(":", 1)
                paths_by_repo[changed_repo].add(rel_path)
            for changed_repo, rel_paths in paths_by_repo.items():
                self.db.clear_chunks_by_paths(changed_repo, sorted(rel_paths))

        repo_heads = self.db.snapshot_repo_heads()
        build_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for chunk in chunks:
            payload = dict(chunk)
            payload["build_commit"] = repo_heads.get(str(chunk.get("repo_id", "") or ""), "")
            payload["build_time"] = build_time
            self.db.insert_chunk(payload)

    def _get_changed_files(self, repo_id: Optional[str] = None,
                           since_commit: Optional[str] = None) -> Optional[set[str]]:
        """Get changed files from git diff across repos."""
        changed = set()
        conn = self.db.connect()

        sql = "SELECT id, path FROM repositories"
        params: list = []
        if repo_id:
            sql += " WHERE id = ?"
            params.append(repo_id)

        repos = conn.execute(sql, params).fetchall()

        for repo in repos:
            r = dict(repo)
            root = Path(r["path"])
            if not root.is_dir():
                continue

            # Build git diff command
            ref = since_commit or "HEAD~1"
            try:
                result = subprocess.run(
                    ["git", "diff", "--name-only", ref],
                    cwd=str(root), capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0:
                    for line in result.stdout.strip().split("\n"):
                        line = line.strip()
                        if line:
                            # Store as repo_id:rel_path for matching
                            changed.add(f"{r['id']}:{line.replace(chr(92), '/')}")
                else:
                    # Fallback: git status for uncommitted changes
                    result2 = subprocess.run(
                        ["git", "status", "--porcelain", "--short"],
                        cwd=str(root), capture_output=True, text=True, timeout=30,
                    )
                    if result2.returncode == 0:
                        for line in result2.stdout.strip().split("\n"):
                            line = line.strip()
                            if len(line) > 3:
                                path = line[3:].strip().replace("\\", "/")
                                changed.add(f"{r['id']}:{path}")
            except (subprocess.TimeoutExpired, FileNotFoundError):
                print(f"  [WARN] git not available for {r['id']}, falling back to full scan",
                      file=sys.stderr)
                return None

        return changed if changed else None

    def _build_chunks(self, repo_id: Optional[str] = None,
                       changed_files: Optional[set[str]] = None) -> list[dict]:
        """Extract source chunks from indexed files."""
        conn = self.db.connect()

        sql = """SELECT f.repo_id, f.rel_path, f.language, f.content, f.is_example
                 FROM files f WHERE f.content != ''"""
        params: list = []
        if repo_id:
            sql += " AND f.repo_id = ?"
            params.append(repo_id)

        rows = conn.execute(sql, params).fetchall()
        chunks = []

        for row in rows:
            r = dict(row)
            content = r["content"]
            if not content or len(content) < 50:
                continue

            if changed_files is not None:
                key = f"{r['repo_id']}:{r['rel_path']}"
                if key not in changed_files:
                    continue

            lang = r["language"]
            lines = content.split("\n")
            repo = r["repo_id"]
            path = r["rel_path"]
            is_example = r["is_example"]

            # Determine chunk type
            if is_example:
                chunk_type = "example"
            elif lang in ("kotlin", "java"):
                chunk_type = "code"
            elif lang == "markdown":
                chunk_type = "doc"
            elif lang in ("yaml",):
                chunk_type = "config"
            else:
                chunk_type = "other"

            # For code files: chunk by class/function boundaries
            if chunk_type == "code" and len(lines) > MAX_CHUNK_LINES:
                sub_chunks = self._chunk_code(lines, repo, path, lang)
                chunks.extend(sub_chunks)
            else:
                metadata = classify_content_metadata(
                    path=path,
                    title=Path(path).stem,
                    content=content,
                    source_type="example" if chunk_type == "example" else "source",
                    is_example=chunk_type == "example",
                )
                # Single chunk for small files or docs
                chunk = {
                    "repo_id": repo,
                    "file_path": path,
                    "chunk_type": chunk_type,
                    "title": Path(path).stem,
                    "content": content[:5000],
                    "language": lang,
                    "start_line": 1,
                    "end_line": len(lines),
                    "symbols": "",
                    "tags": "",
                    "content_domain": metadata["content_domain"],
                    "visibility": metadata["visibility"],
                    "stability": metadata["stability"],
                    "feature_tags": metadata["feature_tags"],
                    "canonical_topics": metadata["canonical_topics"],
                }
                chunks.append(chunk)

        # Also include docs
        doc_rows = conn.execute(
            "SELECT repo_id, path, title, content, tags FROM docs" +
            (" WHERE repo_id = ?" if repo_id else ""),
            ([repo_id] if repo_id else [])
        ).fetchall()
        for dr in doc_rows:
            d = dict(dr)
            metadata = classify_content_metadata(
                path=d["path"],
                title=d.get("title", ""),
                content=d.get("content", ""),
                source_type="docs",
            )
            chunks.append({
                "repo_id": d["repo_id"],
                "file_path": d["path"],
                "chunk_type": "doc",
                "title": d.get("title", ""),
                "content": d.get("content", "")[:5000],
                "language": "markdown",
                "start_line": 0,
                "end_line": 0,
                "symbols": "",
                "tags": d.get("tags", ""),
                "content_domain": metadata["content_domain"],
                "visibility": metadata["visibility"],
                "stability": metadata["stability"],
                "feature_tags": metadata["feature_tags"],
                "canonical_topics": metadata["canonical_topics"],
            })

        return chunks

    def _chunk_code(self, lines: list[str], repo: str, path: str,
                    lang: str) -> list[dict]:
        """Split a large code file into logical chunks."""
        chunks = []
        # Find class/function boundaries
        boundary_re = re.compile(
            r"^\s*(?:(?:public|private|protected|internal|abstract|open|sealed|data|override|suspend|inline)\s+)*"
            r"(?:class|interface|object|enum|fun|annotation)\s+",
            re.MULTILINE
        )

        boundaries = [0]
        for m in boundary_re.finditer("\n".join(lines)):
            line_no = "\n".join(lines)[:m.start()].count("\n")
            if line_no > boundaries[-1] + MIN_CHUNK_LINES:
                boundaries.append(line_no)
        boundaries.append(len(lines))

        for i in range(len(boundaries) - 1):
            start = boundaries[i]
            end = min(boundaries[i] + MAX_CHUNK_LINES, boundaries[i + 1] if i + 1 < len(boundaries) else len(lines))
            chunk_lines = lines[start:end]
            if len(chunk_lines) < MIN_CHUNK_LINES:
                continue

            content = "\n".join(chunk_lines)
            # Extract a title from first class/fun declaration
            title_match = re.search(r"(?:class|interface|object|fun)\s+(\w+)", content)
            title = title_match.group(1) if title_match else Path(path).stem
            metadata = classify_content_metadata(
                path=path,
                title=title,
                content=content,
                source_type="source",
            )

            chunks.append({
                "repo_id": repo,
                "file_path": path,
                "chunk_type": "code",
                "title": title,
                "content": content[:5000],
                "language": lang,
                "start_line": start + 1,
                "end_line": end,
                "symbols": "",
                "tags": "",
                "content_domain": metadata["content_domain"],
                "visibility": metadata["visibility"],
                "stability": metadata["stability"],
                "feature_tags": metadata["feature_tags"],
                "canonical_topics": metadata["canonical_topics"],
            })

        if chunks:
            return chunks
        fallback_content = "\n".join(lines[:MAX_CHUNK_LINES])
        fallback_metadata = classify_content_metadata(
            path=path,
            title=Path(path).stem,
            content=fallback_content,
            source_type="source",
        )
        return [{
            "repo_id": repo,
            "file_path": path,
            "chunk_type": "code",
            "title": Path(path).stem,
            "content": fallback_content,
            "language": lang,
            "start_line": 1,
            "end_line": min(len(lines), MAX_CHUNK_LINES),
            "symbols": "",
            "tags": "",
            "content_domain": fallback_metadata["content_domain"],
            "visibility": fallback_metadata["visibility"],
            "stability": fallback_metadata["stability"],
            "feature_tags": fallback_metadata["feature_tags"],
            "canonical_topics": fallback_metadata["canonical_topics"],
        }]

    def _mine_topics(self, chunks: list[dict]) -> dict[str, dict]:
        """Identify topics from chunks using pattern matching."""
        topics: dict[str, dict] = {}

        for chunk in chunks:
            content = chunk.get("content", "")
            if not content:
                continue

            for rule in self.topic_patterns:
                pattern = rule.get("pattern")
                if pattern is None:
                    continue
                slug = str(rule.get("slug", "")).strip()
                category = str(rule.get("category", "general")).strip() or "general"
                canonical_topic = str(rule.get("canonical_topic", slug)).strip() or slug
                base_conf = float(rule.get("confidence", 0.5))
                matches = pattern.findall(content)
                if not matches:
                    continue

                if slug not in topics:
                    topics[slug] = {
                        "slug": slug,
                        "category": category,
                        "canonical_topic": canonical_topic,
                        "evidence": [],
                        "match_count": 0,
                        "repos": set(),
                        "symbols": set(),
                        "files": set(),
                        "example_chunks": [],
                        "doc_chunks": [],
                        "code_chunks": [],
                        "content_domains": Counter(),
                        "visibilities": Counter(),
                        "stabilities": Counter(),
                        "feature_tags": set(),
                        "canonical_topics": set(),
                        "rule_slugs": set(),
                        "topic_score": 0.0,
                        "base_confidence": base_conf,
                    }

                t = topics[slug]
                t["match_count"] += len(matches)
                t["repos"].add(chunk["repo_id"])
                t["files"].add(f"{chunk['repo_id']}:{chunk['file_path']}")
                t["content_domains"][chunk.get("content_domain", "source")] += 1
                t["visibilities"][chunk.get("visibility", "public")] += 1
                t["stabilities"][chunk.get("stability", "stable")] += 1
                t["feature_tags"].update(chunk.get("feature_tags", []))
                t["feature_tags"].update(rule.get("feature_tags", []))
                t["canonical_topics"].add(_normalize_tag(canonical_topic))
                t["rule_slugs"].add(slug)
                t["topic_score"] += base_conf

                evidence_entry = {
                    "repo": chunk["repo_id"],
                    "file": chunk["file_path"],
                    "matches": matches[:3],
                    "chunk_type": chunk["chunk_type"],
                    "lines": f"{chunk['start_line']}-{chunk['end_line']}",
                    "content_domain": chunk.get("content_domain", ""),
                    "visibility": chunk.get("visibility", ""),
                    "stability": chunk.get("stability", ""),
                }
                t["evidence"].append(evidence_entry)

                if chunk["chunk_type"] == "example":
                    t["example_chunks"].append(chunk)
                elif chunk["chunk_type"] == "doc":
                    t["doc_chunks"].append(chunk)
                else:
                    t["code_chunks"].append(chunk)

                # Extract symbol names from chunk
                sym_matches = re.findall(r'\b([A-Z]\w{2,})\b', content)
                for s in sym_matches[:10]:
                    t["symbols"].add(s)

        return topics

    def _generate_candidates(self, topics: dict[str, dict],
                             repo_heads: dict[str, str]) -> list[dict]:
        """Generate candidate knowledge cards from mined topics."""
        candidates = []

        for slug, topic in topics.items():
            # Calculate confidence
            confidence = self._calc_confidence(topic)
            if confidence < 0.3:
                continue

            # Build card content
            canonical_topic = _dominant_value(topic.get("canonical_topics", Counter()), topic.get("canonical_topic", slug))
            evidence_domain = _dominant_value(topic.get("content_domains", Counter()), "knowledge")
            content_domain = "knowledge"
            visibility_counter = topic.get("visibilities", Counter())
            if topic.get("doc_chunks") or topic.get("example_chunks") or visibility_counter.get("public", 0) > 0:
                visibility = "public"
            else:
                visibility = _dominant_value(visibility_counter, "public", tuple(VISIBILITY_LEVELS))
            stability = _dominant_value(topic.get("stabilities", Counter()), "stable", tuple(STABILITY_LEVELS))
            feature_tags = _unique([
                _normalize_tag(tag) for tag in sorted(topic.get("feature_tags", set())) if _normalize_tag(tag)
            ])
            title = slug.replace("-", " ").title()
            if canonical_topic and _normalize_tag(canonical_topic) != _normalize_tag(slug):
                title = f"{title} ({canonical_topic.replace('-', ' ').title()})"
            category = topic["category"]
            repos = sorted(topic["repos"])
            files = sorted(list(topic["files"])[:20])
            symbols = sorted(list(topic["symbols"])[:30])

            # Build summary from evidence
            summary = self._build_summary(slug, topic, canonical_topic, evidence_domain, visibility, stability)
            content = self._build_content(slug, topic, canonical_topic, evidence_domain, visibility, stability, feature_tags)
            evidence_str = self._format_evidence(topic["evidence"][:10])

            # Related items
            related_docs = []
            for dc in topic["doc_chunks"][:5]:
                related_docs.append(f"{dc['repo_id']}:{dc['file_path']}")

            related_examples = []
            for ec in topic["example_chunks"][:5]:
                related_examples.append(f"{ec['repo_id']}:{ec['file_path']}")

            card = {
                "card_id": f"gen-{slug}",
                "title": title,
                "slug": slug,
                "category": category,
                "summary": summary,
                "content": content,
                "tags": ", ".join(_unique([
                    category,
                    f"domain:{content_domain}",
                    f"visibility:{visibility}",
                    f"stability:{stability}",
                    canonical_topic,
                    slug,
                    *feature_tags,
                    *repos,
                ])),
                "keywords": ", ".join(_unique([
                    *symbols[:15],
                    canonical_topic,
                    slug,
                    *feature_tags,
                ])),
                "aliases": slug.replace("-", " "),
                "related_symbols": ", ".join(symbols[:20]),
                "related_docs": ", ".join(related_docs),
                "related_examples": ", ".join(related_examples),
                "source_type": "generated",
                "content_domain": content_domain,
                "visibility": visibility,
                "stability": stability,
                "feature_tags": ", ".join(feature_tags),
                "canonical_topic": canonical_topic,
                "confidence": round(confidence, 3),
                "status": "candidate",
                "evidence": evidence_str,
                "last_seen_commit": format_commit_map(repos, repo_heads),
                "stale_score": 0.0,
                "evidence_count": topic["match_count"],
            }
            candidates.append(card)

        return candidates

    def _calc_confidence(self, topic: dict) -> float:
        """Calculate confidence score for a topic."""
        base = topic["base_confidence"]
        # Boost for more matches
        match_boost = min(topic["match_count"] / 50.0, 0.15)
        # Boost for cross-repo evidence
        repo_boost = min(len(topic["repos"]) * 0.05, 0.1)
        # Boost for having examples
        example_boost = 0.05 if topic["example_chunks"] else 0
        # Boost for having docs
        doc_boost = 0.05 if topic["doc_chunks"] else 0
        return min(base + match_boost + repo_boost + example_boost + doc_boost, 1.0)

    def _build_summary(self, slug: str, topic: dict,
                      canonical_topic: str, content_domain: str,
                      visibility: str, stability: str) -> str:
        """Build a summary for the candidate card."""
        n_files = len(topic["files"])
        n_repos = len(topic["repos"])
        category = topic["category"]
        return (f"Auto-generated knowledge card about '{slug.replace('-', ' ')}' "
                f"({category}). Canonical topic: {canonical_topic}. "
                f"Domain: {content_domain}. Visibility: {visibility}. Stability: {stability}. "
                f"Found in {n_files} files across {n_repos} repos. Total {topic['match_count']} pattern matches.")

    def _build_content(self, slug: str, topic: dict,
                       canonical_topic: str, content_domain: str,
                       visibility: str, stability: str,
                       feature_tags: list[str]) -> str:
        """Build structured content for the candidate card."""
        parts = [f"# {slug.replace('-', ' ').title()}\n"]
        parts.append(f"**Category**: {topic['category']}")
        parts.append(f"**Canonical topic**: {canonical_topic}")
        parts.append(f"**Content domain**: {content_domain}")
        parts.append(f"**Visibility**: {visibility}")
        parts.append(f"**Stability**: {stability}")
        if feature_tags:
            parts.append(f"**Feature tags**: {', '.join(feature_tags[:12])}")
        parts.append(f"**Evidence**: {topic['match_count']} matches in {len(topic['files'])} files\n")

        # Key code snippets
        if topic["code_chunks"]:
            parts.append("## Key Code References\n")
            for cc in topic["code_chunks"][:3]:
                parts.append(f"### {cc['repo_id']}:{cc['file_path']} (L{cc['start_line']}-{cc['end_line']})")
                snippet = cc["content"][:500]
                parts.append(f"```{cc['language']}\n{snippet}\n```\n")

        # Example references
        if topic["example_chunks"]:
            parts.append("## Example References\n")
            for ec in topic["example_chunks"][:3]:
                parts.append(f"- `{ec['repo_id']}:{ec['file_path']}`")

        # Doc references
        if topic["doc_chunks"]:
            parts.append("\n## Documentation References\n")
            for dc in topic["doc_chunks"][:3]:
                parts.append(f"- `{dc['repo_id']}:{dc['file_path']}` — {dc.get('title', '')}")

        # Symbols
        if topic["symbols"]:
            parts.append(f"\n## Related Symbols\n")
            parts.append(", ".join(sorted(list(topic["symbols"])[:20])))

        parts.append("\n" + render_metadata_block({
            "content_domain": content_domain,
            "visibility": visibility,
            "stability": stability,
            "canonical_topics": [canonical_topic],
            "feature_tags": feature_tags,
        }))

        return "\n".join(parts)

    def _format_evidence(self, evidence: list[dict]) -> str:
        """Format evidence list as a compact string."""
        lines = []
        for e in evidence:
            lines.append(f"{e['repo']}:{e['file']} L{e['lines']} [{e['chunk_type']}] matches={e['matches'][:2]}")
        return "\n".join(lines)

    # ── Knowledge Graph: relation mining ───────────────────────────

    def mine_relations(self) -> int:
        """Discover relationships between knowledge cards based on co-occurrence and content overlap."""
        print("[MINE] Mining card relations...", file=sys.stderr)
        self.db.clear_relations()

        # Get all published knowledge cards
        cards = self.db.get_all_knowledge_cards()
        if len(cards) < 2:
            print("  Not enough cards for relation mining", file=sys.stderr)
            return 0

        # Also include high-confidence candidates
        candidates = [
            c for c in self.db.get_candidates(limit=500)
            if c.get("status") in ("reviewed", "published") or c.get("confidence", 0) >= 0.75
        ]
        all_slugs = {c["slug"] for c in cards}
        for cc in candidates:
            if cc["slug"] not in all_slugs:
                cards.append(cc)
                all_slugs.add(cc["slug"])

        # Build keyword sets for each card
        card_keywords: dict[str, set[str]] = {}
        card_categories: dict[str, str] = {}
        for c in cards:
            slug = c["slug"]
            kw_text = f"{c.get('tags', '')} {c.get('keywords', '')} {c.get('title', '')}".lower()
            card_keywords[slug] = set(re.findall(r'\w{3,}', kw_text))
            card_categories[slug] = c.get("category", "")

        # Predefined strong relations
        KNOWN_RELATIONS = [
            ("repository-pattern", "queryable-api", "related", 0.9),
            ("transaction-boundary", "easyquery-transaction", "related", 0.92),
            ("paging-query", "queryable-api", "related", 0.82),
            ("code-first", "entity-mapping", "depends_on", 0.8),
            ("logic-delete", "entity-mapping", "related", 0.85),
            ("value-converter", "entity-mapping", "related", 0.8),
            ("h2-concurrency-testing", "easyquery-transaction", "related", 0.78),
        ]

        count = 0

        # Insert known relations (only if both slugs exist)
        for src, tgt, rtype, weight in KNOWN_RELATIONS:
            if src in all_slugs and tgt in all_slugs:
                self.db.upsert_relation(src, tgt, rtype, weight, "predefined")
                count += 1

        # Auto-discover relations via keyword overlap
        slugs = sorted(all_slugs)
        for i in range(len(slugs)):
            for j in range(i + 1, len(slugs)):
                s1, s2 = slugs[i], slugs[j]
                kw1, kw2 = card_keywords.get(s1, set()), card_keywords.get(s2, set())
                if not kw1 or not kw2:
                    continue

                overlap = kw1 & kw2
                # Remove very common words
                overlap -= {"the", "and", "for", "with", "from", "this", "that",
                            "plugin", "test", "testing", "framework", "class",
                            "function", "method", "code", "file", "use"}

                if len(overlap) < 3:
                    continue

                # Calculate Jaccard similarity
                jaccard = len(overlap) / len(kw1 | kw2)
                if jaccard < 0.1:
                    continue

                # Determine relation type
                cat1 = card_categories.get(s1, "")
                cat2 = card_categories.get(s2, "")
                if cat1 == cat2 and cat1:
                    rtype = "similar"
                else:
                    rtype = "related"

                weight = round(min(jaccard * 3, 1.0), 3)
                evidence = f"keyword_overlap={len(overlap)}, jaccard={jaccard:.3f}, shared={sorted(list(overlap))[:5]}"
                self.db.upsert_relation(s1, s2, rtype, weight, evidence)
                count += 1

        print(f"  {count} relations discovered", file=sys.stderr)
        return count
