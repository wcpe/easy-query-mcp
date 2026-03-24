"""Configuration loaders for legacy repo configs and multi-source registry."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - fallback for minimal runtime environments
    yaml = None


SUPPORTED_SOURCE_TYPES = {"code", "docs", "wiki", "knowledge"}


@dataclass
class RepoConfig:
    """Legacy repository configuration kept for backwards compatibility."""

    id: str
    name: str
    path: str
    tags: list[str] = field(default_factory=list)
    doc_dirs: list[str] = field(default_factory=list)
    wiki_path: str = ""

    def resolved_path(self) -> Path:
        return Path(self.path).resolve()

    def resolved_doc_dirs(self) -> list[Path]:
        root = self.resolved_path()
        if self.doc_dirs:
            return [root / d for d in self.doc_dirs]
        candidates = ["docs", "doc", "wiki", "plugin-docs", "framework-docs"]
        return [root / item for item in candidates if (root / item).is_dir()]

    def resolved_wiki_path(self) -> Optional[Path]:
        if self.wiki_path:
            path = Path(self.wiki_path).resolve()
            return path if path.is_dir() else None
        return None

    def validate(self) -> None:
        path = self.resolved_path()
        if not path.exists():
            raise FileNotFoundError(f"Repository path does not exist: {path}")
        if not path.is_dir():
            raise NotADirectoryError(f"Repository path is not a directory: {path}")


@dataclass
class SourceConfig:
    """Unified source registry entry."""

    id: str
    type: str
    name: str
    path: str
    tags: list[str] = field(default_factory=list)
    git_url: str = ""
    linked_source_id: str = ""
    enabled: bool = True

    def resolved_path(self, base_dir: Optional[Path] = None) -> Path:
        path = Path(self.path)
        if path.is_absolute():
            return path.resolve()
        base = base_dir.resolve() if base_dir else _find_project_root()
        return (base / path).resolve()

    def validate_type(self) -> None:
        if self.type not in SUPPORTED_SOURCE_TYPES:
            raise ValueError(f"Unsupported source type '{self.type}' for source '{self.id}'")

    def validate_local_path(self, base_dir: Optional[Path] = None) -> None:
        path = self.resolved_path(base_dir)
        if not path.exists():
            raise FileNotFoundError(f"Source path does not exist: {path}")
        if not path.is_dir():
            raise NotADirectoryError(f"Source path is not a directory: {path}")

    def as_dict(self, base_dir: Optional[Path] = None) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "path": str(self.resolved_path(base_dir)),
            "git_url": self.git_url,
            "tags": list(self.tags),
            "linked_source_id": self.linked_source_id,
            "enabled": self.enabled,
        }


@dataclass
class AppConfig:
    """Application-level configuration for legacy flows."""

    repos: list[RepoConfig] = field(default_factory=list)
    db_path: str = "index.db"
    skip_dirs: list[str] = field(default_factory=lambda: [
        "build", "out", ".gradle", ".git", "target",
        "node_modules", ".idea", ".vscode", "__pycache__",
        "bin", "obj", ".settings", ".metadata",
    ])
    index_extensions: list[str] = field(default_factory=lambda: [
        ".kt", ".kts", ".java", ".md", ".yml", ".yaml",
    ])
    doc_extensions: list[str] = field(default_factory=lambda: [
        ".md", ".mdx", ".adoc", ".txt",
    ])


@dataclass
class SourceRegistry:
    """Application-level multi-source configuration."""

    sources: list[SourceConfig] = field(default_factory=list)
    db_path: str = "index.db"
    skip_dirs: list[str] = field(default_factory=lambda: AppConfig().skip_dirs.copy())
    index_extensions: list[str] = field(default_factory=lambda: AppConfig().index_extensions.copy())
    doc_extensions: list[str] = field(default_factory=lambda: AppConfig().doc_extensions.copy())
    config_path: str = ""

    def get(self, source_id: str) -> Optional[SourceConfig]:
        for source in self.sources:
            if source.id == source_id:
                return source
        return None

    def enabled_sources(self, source_type: Optional[str] = None) -> list[SourceConfig]:
        results = [source for source in self.sources if source.enabled]
        if source_type:
            results = [source for source in results if source.type == source_type]
        return results


def _find_project_root() -> Path:
    env_home = os.environ.get("EASY_QUERY_MCP_HOME", "").strip()
    if env_home:
        return Path(env_home).resolve()
    return Path(__file__).resolve().parent.parent.parent


def _resolve_relative(path_value: str, config_dir: Path) -> str:
    path = Path(path_value)
    if path.is_absolute():
        return str(path.resolve())
    return str((config_dir / path).resolve())


def _search_config_file(explicit: Optional[str] = None) -> Optional[Path]:
    candidates: list[Path] = []

    if explicit is not None:
        candidates.append(Path(explicit))
    else:
        env_val = os.environ.get("EASY_QUERY_MCP_CONFIG")
        if env_val:
            candidates.append(Path(env_val))

        cwd = Path.cwd()
        project_root = _find_project_root()
        candidates.extend([
            cwd / "config" / "sources.yaml",
            cwd / "sources.yaml",
            cwd / "repos.yaml",
            project_root / "config" / "sources.yaml",
            project_root / "sources.yaml",
            project_root / "repos.yaml",
        ])

    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.exists():
            return resolved
    return None


def _strip_quotes(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1]
    return text


def _parse_scalar(value: str):
    text = _strip_quotes(value)
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return text


def _parse_inline_list(value: str) -> list[str]:
    text = value.strip()
    if not (text.startswith("[") and text.endswith("]")):
        return [_parse_scalar(text)] if text else []
    inner = text[1:-1].strip()
    if not inner:
        return []
    parts: list[str] = []
    current: list[str] = []
    in_quote: str | None = None
    for ch in inner:
        if ch in {'"', "'"}:
            if in_quote == ch:
                in_quote = None
            elif in_quote is None:
                in_quote = ch
        if ch == "," and in_quote is None:
            part = "".join(current).strip()
            if part:
                parts.append(_strip_quotes(part))
            current = []
            continue
        current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(_strip_quotes(tail))
    return parts


def _simple_yaml_load(text: str) -> dict:
    data: dict = {}
    current_key: str | None = None
    current_list: list | None = None
    current_item: dict | None = None
    pending_list_key: str | None = None

    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()

        if indent == 0 and line.endswith(":"):
            key = line[:-1].strip()
            if key == "sources":
                data[key] = []
                current_key = key
                current_list = data[key]
                current_item = None
                pending_list_key = None
            else:
                data[key] = []
                current_key = key
                current_list = data[key]
                current_item = None
                pending_list_key = key
            continue

        if indent == 0 and ":" in line:
            key, value = line.split(":", 1)
            data[key.strip()] = _parse_scalar(value.strip())
            current_key = None
            current_list = None
            current_item = None
            pending_list_key = None
            continue

        if current_key == "sources" and line.startswith("- "):
            item: dict = {}
            body = line[2:].strip()
            if body:
                key, value = body.split(":", 1)
                parsed = value.strip()
                item[key.strip()] = _parse_inline_list(parsed) if parsed.startswith("[") else _parse_scalar(parsed)
            current_list.append(item)
            current_item = item
            pending_list_key = None
            continue

        if current_key == "sources" and current_item is not None and ":" in line:
            key, value = line.split(":", 1)
            parsed = value.strip()
            current_item[key.strip()] = _parse_inline_list(parsed) if parsed.startswith("[") else _parse_scalar(parsed)
            continue

        if current_list is not None and line.startswith("- "):
            current_list.append(_parse_scalar(line[2:].strip()))
            continue

    return data


def _load_raw_config(path: Path) -> tuple[dict, Path]:
    with open(path, "r", encoding="utf-8") as handle:
        content = handle.read()
    if yaml is not None:
        raw = yaml.safe_load(content) or {}
    else:
        raw = _simple_yaml_load(content)
    return raw, path.parent


def _legacy_repo_to_sources(repo: RepoConfig, config_dir: Path) -> list[SourceConfig]:
    sources = [
        SourceConfig(
            id=repo.id,
            type="code",
            name=repo.name,
            path=_resolve_relative(repo.path, config_dir),
            tags=list(repo.tags),
        )
    ]

    if repo.doc_dirs:
        for index, doc_dir in enumerate(repo.doc_dirs, start=1):
            sources.append(
                SourceConfig(
                    id=f"{repo.id}-docs-{index}",
                    type="docs",
                    name=f"{repo.name} Docs {index}",
                    path=str((Path(_resolve_relative(repo.path, config_dir)) / doc_dir).resolve()),
                    tags=list(dict.fromkeys(list(repo.tags) + ["docs"])),
                    linked_source_id=repo.id,
                )
            )

    if repo.wiki_path:
        sources.append(
            SourceConfig(
                id=f"{repo.id}-wiki",
                type="wiki",
                name=f"{repo.name} Wiki",
                path=_resolve_relative(repo.wiki_path, config_dir),
                tags=list(dict.fromkeys(list(repo.tags) + ["wiki"])),
                linked_source_id=repo.id,
            )
        )

    return sources


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """Load legacy config, but also understand sources.yaml for db/settings access."""
    path = _search_config_file(config_path)
    if path is None:
        searched = config_path or "config/sources.yaml / repos.yaml"
        print(
            f"[WARN] Config file not found: {searched}. "
            f"Searched cwd ({Path.cwd()}) and project root ({_find_project_root()}). "
            f"Using empty config.",
            file=sys.stderr,
        )
        return AppConfig()

    raw, config_dir = _load_raw_config(path)
    raw_db = raw.get("db_path", "index.db")
    db_path = _resolve_relative(raw_db, config_dir)

    repos: list[RepoConfig] = []
    if "repos" in raw:
        for item in raw.get("repos", []):
            repos.append(
                RepoConfig(
                    id=item["id"],
                    name=item.get("name", item["id"]),
                    path=_resolve_relative(item["path"], config_dir),
                    tags=item.get("tags", []),
                    doc_dirs=item.get("doc_dirs", []),
                    wiki_path=_resolve_relative(item["wiki_path"], config_dir) if item.get("wiki_path") else "",
                )
            )
    elif "sources" in raw:
        for item in raw.get("sources", []):
            if item.get("type") != "code":
                continue
            repos.append(
                RepoConfig(
                    id=item["id"],
                    name=item.get("name", item["id"]),
                    path=_resolve_relative(item["path"], config_dir),
                    tags=item.get("tags", []),
                )
            )

    cfg = AppConfig(
        repos=repos,
        db_path=db_path,
        skip_dirs=raw.get("skip_dirs", AppConfig().skip_dirs),
        index_extensions=raw.get("index_extensions", AppConfig().index_extensions),
        doc_extensions=raw.get("doc_extensions", AppConfig().doc_extensions),
    )
    print(f"[INFO] Loaded config from {path}", file=sys.stderr)
    print(f"[INFO] Database path: {cfg.db_path}", file=sys.stderr)
    return cfg


def load_source_registry(config_path: Optional[str] = None) -> SourceRegistry:
    """Load the multi-source registry from sources.yaml or legacy repos.yaml."""
    path = _search_config_file(config_path)
    if path is None:
        searched = config_path or "config/sources.yaml / repos.yaml"
        print(
            f"[WARN] Source registry not found: {searched}. "
            f"Searched cwd ({Path.cwd()}) and project root ({_find_project_root()}). "
            f"Using empty source registry.",
            file=sys.stderr,
        )
        return SourceRegistry()

    raw, config_dir = _load_raw_config(path)
    raw_db = raw.get("db_path", "index.db")
    db_path = _resolve_relative(raw_db, config_dir)

    sources: list[SourceConfig] = []
    if "sources" in raw:
        for item in raw.get("sources", []):
            source = SourceConfig(
                id=item["id"],
                type=item.get("type", "code"),
                name=item.get("name", item["id"]),
                path=_resolve_relative(item["path"], config_dir),
                tags=item.get("tags", []),
                git_url=item.get("git_url", ""),
                linked_source_id=item.get("linked_source_id", ""),
                enabled=bool(item.get("enabled", True)),
            )
            source.validate_type()
            sources.append(source)
    elif "repos" in raw:
        legacy_config = load_config(str(path))
        for repo in legacy_config.repos:
            sources.extend(_legacy_repo_to_sources(repo, config_dir))

    registry = SourceRegistry(
        sources=sources,
        db_path=db_path,
        skip_dirs=raw.get("skip_dirs", AppConfig().skip_dirs),
        index_extensions=raw.get("index_extensions", AppConfig().index_extensions),
        doc_extensions=raw.get("doc_extensions", AppConfig().doc_extensions),
        config_path=str(path),
    )
    print(f"[INFO] Loaded source registry from {path}", file=sys.stderr)
    print(f"[INFO] Database path: {registry.db_path}", file=sys.stderr)
    print(f"[INFO] Sources loaded: {len(registry.sources)}", file=sys.stderr)
    return registry
