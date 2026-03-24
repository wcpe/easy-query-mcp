"""Source code indexer for Kotlin/Java repositories."""

import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import AppConfig, RepoConfig
from .database import Database
from .knowledge_miner import classify_content_metadata, materialize_metadata_tags, render_metadata_block

# ── Language detection ──────────────────────────────────────────────

EXTENSION_LANG_MAP = {
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".java": "java",
    ".md": "markdown",
    ".mdx": "markdown",
    ".adoc": "asciidoc",
    ".txt": "text",
    ".yml": "yaml",
    ".yaml": "yaml",
}

# ── Document file patterns ─────────────────────────────────────────

DOC_EXTENSIONS = {".md", ".mdx", ".adoc", ".txt"}

# Well-known doc files at repo root
ROOT_DOC_FILES = {
    "readme.md", "readme.txt",
    "architecture.md", "design.md",
    "contributing.md", "changelog.md",
    "api.md", "guide.md", "faq.md",
}

# ── Example path patterns ──────────────────────────────────────────

EXAMPLE_PATTERNS = re.compile(
    r"(example|examples|demo|sample|samples|test|tests|testing)",
    re.IGNORECASE,
)

EASYQUERY_EXAMPLE_MARKERS = (
    "/example/",
    "/examples/",
    "/demo/",
    "/samples/",
    "/test/",
    "/tests/",
    "/benchmark/",
)

EASYQUERY_EXAMPLE_FILES = {
    "example.java",
    "example.kt",
    "demo.java",
    "demo.kt",
    "sample.java",
    "sample.kt",
}

TEST_PATH_PATTERNS = re.compile(
    r"(^|/)(src/test|src/integration-test|test|tests|testing|it)(/|$)|(?:test|tests)\.(?:java|kt|kts)$",
    re.IGNORECASE,
)

API_PATH_PATTERNS = re.compile(
    r"(^|/)(api|apis|public|contracts?|interfaces?)(/|$)",
    re.IGNORECASE,
)

MIGRATION_PATTERNS = re.compile(
    r"\b(migration|migrate|upgrade|changelog|release[-_ ]notes?|breaking[-_ ]changes?)\b",
    re.IGNORECASE,
)

FAQ_PATTERNS = re.compile(
    r"\b(faq|frequently asked|common questions?|troubleshooting)\b|常见问题",
    re.IGNORECASE,
)

PRIVATE_PATTERNS = re.compile(r"\bprivate\b", re.IGNORECASE)
PROTECTED_PATTERNS = re.compile(r"\bprotected\b", re.IGNORECASE)
INTERNAL_PATTERNS = re.compile(r"\b(internal|impl|implementation|package-private)\b|/(internal|impl)/", re.IGNORECASE)
PUBLIC_PATTERNS = re.compile(r"\bpublic\b", re.IGNORECASE)

DEPRECATED_PATTERNS = re.compile(r"\b(deprecated|obsolete|legacy|to be removed|scheduled for removal)\b", re.IGNORECASE)
EXPERIMENTAL_PATTERNS = re.compile(r"\b(experimental|preview|incubating|alpha|beta|wip)\b", re.IGNORECASE)

CANONICAL_TOPIC_RULES: dict[str, tuple[str, ...]] = {
    "pagination": (
        "toPageResult",
        "PageResult",
        "Pager",
        "pageIndex",
        "pageSize",
        "分页",
        "paginate",
        "pagination",
        "paging",
    ),
    "transaction": (
        "transaction",
        "Transactional",
        "beginTransaction",
        "rollback",
        "commit",
        "事务",
    ),
    "dynamic-condition": (
        "dynamic condition",
        "dynamic where",
        "whereObject",
        "whereColumns",
        "filterConfigure",
        "predicate",
        "dynamic filter",
        "动态条件",
        "动态查询",
    ),
    "logic-delete": (
        "logicDelete",
        "LogicDelete",
        "logic delete",
        "soft delete",
        "isDeleted",
        "deleted",
        "逻辑删除",
    ),
    "sharding": (
        "Sharding",
        "TableRoute",
        "DataSourceRoute",
        "partition",
        "route",
        "分片",
        "分库分表",
    ),
    "code-first": (
        "CodeFirst",
        "autoMigrate",
        "Migration",
        "schema",
        "DDL",
        "code first",
        "Code First",
    ),
}

CANONICAL_TOPIC_LABELS = {
    "pagination": "分页",
    "transaction": "事务",
    "dynamic-condition": "动态条件",
    "logic-delete": "逻辑删除",
    "sharding": "分片",
    "code-first": "CodeFirst",
}

# ── Symbol extraction regexes ──────────────────────────────────────

# Kotlin patterns
KT_CLASS = re.compile(
    r"^\s*(?:(?:public|private|protected|internal|abstract|open|sealed|data|inner|value|inline)\s+)*"
    r"(class|interface|object|enum\s+class|annotation\s+class)\s+"
    r"(\w+)"
    r"((?:\s*<[^>]*>)?(?:\s*\([^)]*\))?(?:\s*:\s*[^\{]+)?)",
    re.MULTILINE,
)

KT_FUN = re.compile(
    r"^\s*(?:(?:public|private|protected|internal|abstract|open|override|inline|suspend|operator|infix|tailrec)\s+)*"
    r"fun\s+(?:<[^>]*>\s*)?"
    r"(?:(\w+)\.)?(\w+)\s*"
    r"(\([^)]*\))"
    r"(?:\s*:\s*([^\{=]+))?",
    re.MULTILINE,
)

KT_PROPERTY = re.compile(
    r"^\s*(?:(?:public|private|protected|internal|abstract|open|override|const|lateinit)\s+)*"
    r"(val|var)\s+(\w+)\s*(?::\s*([^\=\n]+))?",
    re.MULTILINE,
)

# Java patterns
JAVA_CLASS = re.compile(
    r"^\s*(?:(?:public|private|protected|abstract|static|final|sealed)\s+)*"
    r"(class|interface|enum|@interface)\s+"
    r"(\w+)"
    r"((?:\s*<[^>]*>)?(?:\s+extends\s+[^\{]+)?(?:\s+implements\s+[^\{]+)?)",
    re.MULTILINE,
)

JAVA_METHOD = re.compile(
    r"^\s*(?:(?:public|private|protected|abstract|static|final|synchronized|native|default|override)\s+)*"
    r"(?:<[^>]*>\s*)?"
    r"(\w[\w<>\[\],\s]*?)\s+"
    r"(\w+)\s*"
    r"(\([^)]*\))",
    re.MULTILINE,
)

# Map kind strings
KIND_MAP = {
    "class": "class",
    "interface": "interface",
    "object": "object",
    "enum class": "enum",
    "annotation class": "annotation",
    "enum": "enum",
    "@interface": "annotation",
}


def detect_language(path: str) -> str:
    """Detect language from file extension."""
    ext = Path(path).suffix.lower()
    return EXTENSION_LANG_MAP.get(ext, "unknown")


def is_example_path(rel_path: str) -> bool:
    """Check if a file path looks like an example/test file."""
    normalized = rel_path.replace("\\", "/").lower()
    if EXAMPLE_PATTERNS.search(normalized):
        return True
    if any(marker in normalized for marker in EASYQUERY_EXAMPLE_MARKERS):
        return True
    filename = Path(normalized).name
    if filename in EASYQUERY_EXAMPLE_FILES:
        return True
    return False


def is_test_path(rel_path: str) -> bool:
    """Check if a file path looks like a test file."""
    normalized = rel_path.replace("\\", "/")
    return bool(TEST_PATH_PATTERNS.search(normalized))


def extract_canonical_topics(*parts: str) -> list[str]:
    """Extract stable canonical topic slugs from mixed text."""
    haystack = "\n".join(str(part or "") for part in parts if part)
    if not haystack:
        return []
    topics: list[str] = []
    lowered = haystack.lower()
    for slug, terms in CANONICAL_TOPIC_RULES.items():
        if any(term.lower() in lowered for term in terms):
            topics.append(slug)
    return topics


def classify_content_domain(
    rel_path: str,
    source_type: str,
    *,
    title: str = "",
    content: str = "",
    is_example: bool = False,
    prefer_api: bool = False,
) -> str:
    """Classify content into a stable first-class domain."""
    normalized = rel_path.replace("\\", "/").lower()
    title_lower = title.lower()
    text = f"{normalized}\n{title_lower}\n{content[:3000].lower()}"

    if source_type == "knowledge":
        return "knowledge"
    if source_type in {"docs", "wiki"}:
        if FAQ_PATTERNS.search(text):
            return "faq"
        if prefer_api or API_PATH_PATTERNS.search(text) or any(marker in text for marker in ("api", "reference", "contract", "endpoint")):
            return "api"
        if MIGRATION_PATTERNS.search(text):
            return "migration"
        return "doc"
    if is_test_path(rel_path):
        return "test"
    if is_example or is_example_path(rel_path):
        return "example"
    if prefer_api or API_PATH_PATTERNS.search(normalized):
        return "api"
    if MIGRATION_PATTERNS.search(text):
        return "migration"
    if FAQ_PATTERNS.search(text):
        return "faq"
    return "source"


def infer_visibility(
    rel_path: str,
    *,
    signature: str = "",
    source_type: str = "",
    content_domain: str = "",
) -> str:
    """Infer visibility without requiring a full parser."""
    signature_lower = signature.lower()
    path_lower = rel_path.replace("\\", "/").lower()
    joined = f"{signature_lower}\n{path_lower}"

    if content_domain in {"doc", "faq", "migration", "knowledge", "example", "test"}:
        return "public"
    if PRIVATE_PATTERNS.search(signature_lower):
        return "private"
    if PROTECTED_PATTERNS.search(signature_lower):
        return "protected"
    if INTERNAL_PATTERNS.search(joined):
        return "internal"
    if PUBLIC_PATTERNS.search(signature_lower) or content_domain == "api" or source_type in {"docs", "wiki", "knowledge"}:
        return "public"
    return "internal" if content_domain == "source" else "public"


def infer_stability(
    rel_path: str,
    *,
    title: str = "",
    signature: str = "",
    content: str = "",
    visibility: str = "",
) -> str:
    """Infer a coarse stability class for retrieval and ranking."""
    text = "\n".join(
        part for part in [
            rel_path.replace("\\", "/"),
            title,
            signature,
            content[:4000],
        ] if part
    )
    lowered = text.lower()
    if DEPRECATED_PATTERNS.search(lowered):
        return "deprecated"
    if EXPERIMENTAL_PATTERNS.search(lowered):
        return "experimental"
    if visibility == "internal" or "/internal/" in lowered or "/impl/" in lowered:
        return "internal"
    return "stable"


def build_feature_tags(
    rel_path: str,
    *,
    title: str = "",
    content: str = "",
    repo_tags: Optional[list[str]] = None,
    content_domain: str = "",
) -> list[str]:
    """Build compact, explainable feature tags for retrieval."""
    tags: list[str] = []
    seen = set()

    def _add(tag: str) -> None:
        normalized = str(tag or "").strip().lower()
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        tags.append(normalized)

    for topic in extract_canonical_topics(rel_path, title, content):
        _add(topic)
    if content_domain:
        _add(content_domain)
    for tag in repo_tags or []:
        _add(tag)
    lowered = f"{rel_path}\n{title}\n{content[:2000]}".lower()
    for marker, tag in (
        ("queryable", "queryable"),
        ("join", "join"),
        ("entity", "entity"),
        ("converter", "converter"),
        ("h2", "h2"),
    ):
        if marker in lowered:
            _add(tag)
    return tags[:12]


def _merge_keyword_lists(*parts) -> list[str]:
    """Merge comma-separated or list-like keyword inputs into stable tags."""
    merged: list[str] = []
    seen: set[str] = set()

    def _add(value: str) -> None:
        normalized = str(value or "").strip().lower()
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        merged.append(normalized)

    for part in parts:
        if not part:
            continue
        if isinstance(part, str):
            items = re.split(r"[,\|;/]+", part)
        else:
            items = list(part)
        for item in items:
            _add(str(item))
    return merged[:20]


def extract_symbols_kotlin(content: str, rel_path: str) -> list[dict]:
    """Extract symbols from Kotlin source code."""
    symbols = []

    for m in KT_CLASS.finditer(content):
        kind_raw = m.group(1).strip()
        name = m.group(2)
        rest = (m.group(3) or "").strip()
        line = content[:m.start()].count("\n") + 1
        kind = KIND_MAP.get(kind_raw, "class")
        sig = f"{kind_raw} {name}{rest}"
        symbols.append({
            "name": name,
            "qualified_name": f"{_package_from_path(rel_path)}.{name}",
            "kind": kind,
            "signature": sig.strip(),
            "line_number": line,
        })

    for m in KT_FUN.finditer(content):
        receiver = m.group(1) or ""
        name = m.group(2)
        params = m.group(3) or "()"
        ret = (m.group(4) or "").strip()
        line = content[:m.start()].count("\n") + 1
        prefix = f"{receiver}." if receiver else ""
        sig = f"fun {prefix}{name}{params}"
        if ret:
            sig += f": {ret}"
        symbols.append({
            "name": name,
            "qualified_name": f"{_package_from_path(rel_path)}.{prefix}{name}",
            "kind": "function",
            "signature": sig.strip(),
            "line_number": line,
        })

    return symbols


def extract_symbols_java(content: str, rel_path: str) -> list[dict]:
    """Extract symbols from Java source code."""
    symbols = []

    for m in JAVA_CLASS.finditer(content):
        kind_raw = m.group(1).strip()
        name = m.group(2)
        rest = (m.group(3) or "").strip()
        line = content[:m.start()].count("\n") + 1
        kind = KIND_MAP.get(kind_raw, "class")
        sig = f"{kind_raw} {name}{rest}"
        symbols.append({
            "name": name,
            "qualified_name": f"{_package_from_path(rel_path)}.{name}",
            "kind": kind,
            "signature": sig.strip(),
            "line_number": line,
        })

    for m in JAVA_METHOD.finditer(content):
        ret_type = m.group(1).strip()
        name = m.group(2)
        params = m.group(3) or "()"
        line = content[:m.start()].count("\n") + 1
        # Skip common false positives
        if name in ("if", "for", "while", "switch", "catch", "return", "new", "throw"):
            continue
        sig = f"{ret_type} {name}{params}"
        symbols.append({
            "name": name,
            "qualified_name": f"{_package_from_path(rel_path)}.{name}",
            "kind": "method",
            "signature": sig.strip(),
            "line_number": line,
        })

    return symbols


def _package_from_path(rel_path: str) -> str:
    """Derive a rough package name from file path."""
    p = rel_path.replace("\\", "/")
    # Try to find src/main/kotlin or src/main/java prefix
    for marker in ("src/main/kotlin/", "src/main/java/", "src/"):
        idx = p.find(marker)
        if idx >= 0:
            p = p[idx + len(marker):]
            break
    # Remove file extension and convert slashes to dots
    p = p.rsplit(".", 1)[0]
    return p.replace("/", ".")


def _compute_end_lines(symbols: list[dict], total_lines: int) -> list[dict]:
    """Estimate end_line for each symbol based on next symbol's start."""
    sorted_syms = sorted(symbols, key=lambda s: s["line_number"])
    for i, sym in enumerate(sorted_syms):
        if i + 1 < len(sorted_syms):
            sym["end_line"] = sorted_syms[i + 1]["line_number"] - 1
        else:
            sym["end_line"] = total_lines
    return sorted_syms


# ── Document indexing helpers ──────────────────────────────────────

_HEADING_RE = re.compile(r"^#\s+(.+)", re.MULTILINE)
_ADOC_TITLE_RE = re.compile(r"^=\s+(.+)", re.MULTILINE)
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|[-_./\\]")


def extract_doc_title(content: str, filename: str) -> str:
    """Extract title from document content. Prefers first H1 heading."""
    # Markdown H1
    m = _HEADING_RE.search(content)
    if m:
        return m.group(1).strip()
    # AsciiDoc title
    m = _ADOC_TITLE_RE.search(content)
    if m:
        return m.group(1).strip()
    # Fallback: filename without extension
    return Path(filename).stem.replace("-", " ").replace("_", " ").title()


def generate_doc_tags(rel_path: str, repo_tags: list[str], title: str) -> list[str]:
    """Auto-generate tags from path, repo tags, and title."""
    tags = set()

    # From repo tags
    for t in repo_tags:
        tags.add(t.lower())

    # From path segments
    parts = Path(rel_path).parts
    for part in parts:
        name = Path(part).stem.lower()
        if name and len(name) > 2 and name not in ("src", "main", "kotlin", "java", "resources"):
            # Split camelCase and kebab-case
            sub_parts = _CAMEL_SPLIT_RE.split(name)
            for sp in sub_parts:
                sp = sp.strip().lower()
                if sp and len(sp) > 2:
                    tags.add(sp)

    # From title words
    title_words = re.findall(r'\w+', title.lower())
    stop = {"the", "a", "an", "is", "are", "and", "or", "for", "to", "of", "in", "with"}
    for w in title_words:
        if w not in stop and len(w) > 2:
            tags.add(w)

    return sorted(tags)


def is_doc_file(rel_path: str, doc_extensions: set[str]) -> bool:
    """Check if a file is a documentation file."""
    ext = Path(rel_path).suffix.lower()
    return ext in doc_extensions


def build_content_metadata(path: str, title: str, content: str,
                           source_type: str, is_example: bool = False) -> tuple[dict, list[str]]:
    """Classify content and render searchable metadata tags."""
    metadata = classify_content_metadata(
        path=path,
        title=title,
        content=content,
        source_type=source_type,
        is_example=is_example,
    )
    tags = materialize_metadata_tags(metadata)
    return metadata, tags


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_head(path: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


class Indexer:
    """Builds the search index from source repositories."""

    def __init__(self, config: AppConfig, db: Database):
        self.config = config
        self.db = db
        self.stats = {"repos": 0, "files": 0, "symbols": 0, "examples": 0, "docs": 0, "cards": 0}

    def index_all(self) -> dict:
        """Index all configured repositories and knowledge cards."""
        self.db.initialize()
        for repo_cfg in self.config.repos:
            try:
                repo_cfg.validate()
                self._index_repo(repo_cfg)
                self.stats["repos"] += 1
            except (FileNotFoundError, NotADirectoryError) as e:
                print(f"[SKIP] {repo_cfg.id}: {e}", file=sys.stderr)

        # Index knowledge cards
        card_count = self._index_knowledge_cards()
        self.stats["cards"] = card_count
        return self.stats

    def _index_repo(self, repo: RepoConfig) -> None:
        """Index a single repository (code + docs)."""
        print(f"[INDEX] Indexing {repo.id} at {repo.path} ...")
        self.db.clear_repo(repo.id)
        self.db.upsert_repo(
            repo.id, repo.name,
            str(repo.resolved_path()),
            ",".join(repo.tags),
        )

        root = repo.resolved_path()
        build_commit = _git_head(root)
        build_time = _utc_now()
        file_count = 0
        sym_count = 0
        ex_count = 0
        doc_count = 0
        doc_exts = set(self.config.doc_extensions)

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in self.config.skip_dirs
            ]

            for fname in filenames:
                ext = Path(fname).suffix.lower()
                if ext not in self.config.index_extensions and ext not in doc_exts:
                    continue

                full_path = Path(dirpath) / fname
                rel_path = str(full_path.relative_to(root)).replace("\\", "/")
                lang = detect_language(fname)

                try:
                    content = full_path.read_text(encoding="utf-8", errors="replace")
                except Exception as e:
                    print(f"  [WARN] Cannot read {rel_path}: {e}", file=sys.stderr)
                    continue

                # ── Index as code file ─────────────────────────────
                if ext in self.config.index_extensions:
                    is_example = is_example_path(rel_path)
                    content_domain = classify_content_domain(
                        rel_path,
                        "code",
                        content=content,
                        is_example=is_example,
                    )
                    visibility = infer_visibility(
                        rel_path,
                        source_type="code",
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
                        repo_tags=repo.tags,
                        content_domain=content_domain,
                    )
                    file_id = self.db.insert_file(
                        repo.id, rel_path, lang, content, is_example,
                    )
                    file_count += 1

                    symbols = []
                    if lang == "kotlin":
                        symbols = extract_symbols_kotlin(content, rel_path)
                    elif lang == "java":
                        symbols = extract_symbols_java(content, rel_path)

                    total_lines = content.count("\n") + 1
                    symbols = _compute_end_lines(symbols, total_lines)

                    for sym in symbols:
                        sym_domain = "api" if content_domain == "source" and sym["kind"] in {"class", "interface", "annotation", "enum", "object"} else content_domain
                        sym_visibility = infer_visibility(
                            rel_path,
                            signature=sym["signature"],
                            source_type="code",
                            content_domain=sym_domain,
                        )
                        sym_stability = infer_stability(
                            rel_path,
                            signature=sym["signature"],
                            content=content,
                            visibility=sym_visibility,
                        )
                        self.db.insert_symbol(
                            repo.id, file_id,
                            sym["name"], sym["qualified_name"],
                            sym["kind"], sym["signature"],
                            sym["line_number"], sym["end_line"],
                        )
                        sym_count += 1

                    if is_example:
                        snippet = content[:2000] if len(content) > 2000 else content
                        title = fname
                        if content_domain != "source" or stability != "stable":
                            title = f"{fname} [{content_domain}/{stability}]"
                            snippet = render_metadata_block({
                                "content_domain": content_domain,
                                "visibility": visibility,
                                "stability": stability,
                                "build_commit": build_commit[:12] if build_commit else "",
                                "build_time": build_time,
                                "feature_tags": feature_tags,
                                "canonical_topics": extract_canonical_topics(rel_path, fname, content),
                            }) + "\n\n" + snippet
                        self.db.insert_example(
                            repo.id,
                            file_id,
                            title,
                            snippet,
                        )
                        ex_count += 1

                # ── Index as documentation ─────────────────────────
                if is_doc_file(rel_path, doc_exts):
                    doc_count += self._index_doc_file(
                        repo, rel_path, content, fname,
                    )

        # ── Index wiki if configured ───────────────────────────
        wiki_path = repo.resolved_wiki_path()
        if wiki_path and wiki_path.is_dir():
            doc_count += self._index_wiki(repo, wiki_path, doc_exts)

        self.stats["files"] += file_count
        self.stats["symbols"] += sym_count
        self.stats["examples"] += ex_count
        self.stats["docs"] += doc_count
        print(f"  Done: {file_count} files, {sym_count} symbols, "
              f"{ex_count} examples, {doc_count} docs")

    def _index_doc_file(self, repo: RepoConfig, rel_path: str,
                        content: str, fname: str) -> int:
        """Index a single documentation file. Returns 1 on success, 0 on skip."""
        if not content.strip():
            return 0

        title = extract_doc_title(content, fname)
        lang = detect_language(fname)
        content_domain = classify_content_domain(rel_path, "docs", title=title, content=content)
        visibility = infer_visibility(rel_path, source_type="docs", content_domain=content_domain)
        stability = infer_stability(rel_path, title=title, content=content, visibility=visibility)
        feature_tags = build_feature_tags(
            rel_path,
            title=title,
            content=content,
            repo_tags=repo.tags,
            content_domain=content_domain,
        )
        tags = materialize_metadata_tags(
            {
                "content_domain": content_domain,
                "visibility": visibility,
                "stability": stability,
                "feature_tags": feature_tags,
                "canonical_topics": extract_canonical_topics(rel_path, title, content),
            },
            generate_doc_tags(rel_path, repo.tags, title),
        )
        build_commit = _git_head(repo.resolved_path())
        build_time = _utc_now()
        tags.extend([f"build_commit:{build_commit[:12]}" if build_commit else "build_commit:", f"build_time:{build_time}"])

        self.db.insert_doc(
            repo.id, rel_path, title,
            ",".join(tags), content, lang,
        )
        return 1

    def _index_wiki(self, repo: RepoConfig, wiki_path: Path,
                    doc_exts: set[str]) -> int:
        """Index a wiki directory. Returns number of docs indexed."""
        count = 0
        for dirpath, dirnames, filenames in os.walk(wiki_path):
            dirnames[:] = [d for d in dirnames if d not in self.config.skip_dirs]
            for fname in filenames:
                ext = Path(fname).suffix.lower()
                if ext not in doc_exts:
                    continue
                full_path = Path(dirpath) / fname
                rel_path = "wiki/" + str(full_path.relative_to(wiki_path)).replace("\\", "/")
                try:
                    content = full_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                count += self._index_doc_file(repo, rel_path, content, fname)
        if count:
            print(f"  Wiki: {count} docs from {wiki_path}")
        return count

    def _index_knowledge_cards(self) -> int:
        """Scan knowledge_cards/ directory and index all cards."""
        from .config import _find_project_root
        kc_dir = _find_project_root() / "knowledge_cards"
        if not kc_dir.is_dir():
            return 0

        print(f"[INDEX] Indexing knowledge cards from {kc_dir} ...")
        self.db.clear_knowledge_cards()
        count = 0

        for dirpath, _, filenames in os.walk(kc_dir):
            for fname in filenames:
                ext = Path(fname).suffix.lower()
                if ext not in (".md", ".mdx", ".yaml", ".yml"):
                    continue
                full_path = Path(dirpath) / fname
                try:
                    raw = full_path.read_text(encoding="utf-8", errors="replace")
                except Exception as e:
                    print(f"  [WARN] Cannot read {fname}: {e}", file=sys.stderr)
                    continue

                card = _parse_knowledge_card(raw, fname)
                if card is None:
                    print(f"  [WARN] No valid front matter in {fname}", file=sys.stderr)
                    continue

                content_domain = card.get("content_domain") or classify_content_domain(
                    fname,
                    "knowledge",
                    title=card.get("title", ""),
                    content=card.get("content", ""),
                )
                visibility = card.get("visibility") or infer_visibility(
                    fname,
                    source_type="knowledge",
                    content_domain=content_domain,
                )
                stability = card.get("stability") or infer_stability(
                    fname,
                    title=card.get("title", ""),
                    content=card.get("content", ""),
                    visibility=visibility,
                )
                feature_tags = _merge_keyword_lists(
                    card.get("feature_tags", ""),
                    build_feature_tags(
                        fname,
                        title=card.get("title", ""),
                        content=card.get("content", ""),
                        content_domain=content_domain,
                    ),
                )
                metadata = {
                    "content_domain": content_domain,
                    "visibility": visibility,
                    "stability": stability,
                    "feature_tags": feature_tags,
                    "canonical_topics": _merge_keyword_lists(
                        card.get("canonical_topic", ""),
                        extract_canonical_topics(fname, card.get("title", ""), card.get("content", "")),
                    ),
                    "build_commit": _git_head(full_path.parent),
                    "build_time": _utc_now(),
                }
                card["tags"] = ",".join(materialize_metadata_tags(metadata, (card.get("tags", "") or "").split(",")))
                card["keywords"] = ",".join(_merge_keyword_lists(card.get("keywords", ""), feature_tags, metadata["canonical_topics"]))
                card["content"] = render_metadata_block(metadata) + "\n\n" + (card.get("content", "") or "")
                self.db.upsert_knowledge_card(card)
                count += 1

        print(f"  Knowledge cards: {count}")
        return count


def _parse_knowledge_card(raw_content: str, filename: str) -> Optional[dict]:
    """Parse a knowledge card file with YAML front matter."""
    # Split front matter from content
    fm, body = _split_front_matter(raw_content)
    if fm is None:
        # Try to generate minimal card from pure markdown
        title = extract_doc_title(raw_content, filename)
        slug = Path(filename).stem
        return {
            "card_id": f"card-{slug}",
            "title": title,
            "slug": slug,
            "content": raw_content,
            "source_type": "auto",
            "content_domain": "knowledge",
            "visibility": "public",
            "stability": "stable",
            "feature_tags": "",
            "priority": 30,
        }

    # fm is a dict from YAML
    card_id = fm.get("card_id", f"card-{Path(filename).stem}")
    title = fm.get("title", extract_doc_title(body, filename))
    slug = fm.get("slug", Path(filename).stem)

    def _join(val) -> str:
        """Convert list or string to comma-separated string."""
        if isinstance(val, list):
            return ", ".join(str(v) for v in val)
        return str(val) if val else ""

    return {
        "card_id": card_id,
        "title": title,
        "slug": slug,
        "category": fm.get("category", ""),
        "summary": fm.get("summary", _extract_first_paragraph(body)),
        "content": body,
        "tags": _join(fm.get("tags", "")),
        "keywords": _join(fm.get("keywords", "")),
        "aliases": _join(fm.get("aliases", "")),
        "content_domain": fm.get("content_domain", "knowledge"),
        "visibility": fm.get("visibility", "public"),
        "stability": fm.get("stability", "stable"),
        "feature_tags": _join(fm.get("feature_tags", "")),
        "canonical_topic": fm.get("canonical_topic", ""),
        "when_to_use": fm.get("when_to_use", ""),
        "avoid": fm.get("avoid", ""),
        "best_practices": fm.get("best_practices", ""),
        "related_symbols": _join(fm.get("related_symbols", "")),
        "related_docs": _join(fm.get("related_docs", "")),
        "related_examples": _join(fm.get("related_examples", "")),
        "related_errors": _join(fm.get("related_errors", "")),
        "source_type": fm.get("source_type", "manual"),
        "build_commit": fm.get("build_commit", ""),
        "build_time": fm.get("build_time", ""),
        "priority": int(fm.get("priority", 50)),
    }


def _split_front_matter(text: str) -> tuple[Optional[dict], str]:
    """Split YAML front matter from markdown body."""
    import yaml
    text = text.strip()
    if not text.startswith("---"):
        return None, text

    end = text.find("---", 3)
    if end < 0:
        return None, text

    fm_raw = text[3:end].strip()
    body = text[end + 3:].strip()

    try:
        fm = yaml.safe_load(fm_raw)
        if not isinstance(fm, dict):
            return None, text
        return fm, body
    except Exception:
        return None, text


def _extract_first_paragraph(text: str) -> str:
    """Extract first non-heading paragraph as summary."""
    lines = text.split("\n")
    para = []
    started = False
    for line in lines:
        s = line.strip()
        if not started:
            if s and not s.startswith("#") and not s.startswith("---"):
                started = True
                para.append(s)
        else:
            if not s:
                break
            para.append(s)
    result = " ".join(para)
    return result[:300] if len(result) > 300 else result
