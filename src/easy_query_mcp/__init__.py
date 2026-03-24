from __future__ import annotations

import os
import sys
from pathlib import Path


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _apply_default_env() -> None:
    root = _project_root()
    os.environ.setdefault("EASY_QUERY_MCP_HOME", str(root))

    config_path = root / "config" / "sources.yaml"
    if config_path.exists():
        os.environ.setdefault("EASY_QUERY_MCP_CONFIG", str(config_path))

    aliases_path = root / "config" / "query_aliases.yaml"
    if aliases_path.exists():
        os.environ.setdefault("EASY_QUERY_MCP_QUERY_ALIASES", str(aliases_path))


def main() -> None:
    _apply_default_env()

    mode = sys.argv[1] if len(sys.argv) > 1 else "server"
    if mode == "index":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        from .cli import main as cli_main

        cli_main()
        return

    if mode == "sync-sources":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        from .source_manager import main_sync

        main_sync()
        return

    if mode == "server":
        sys.argv = [sys.argv[0]] + sys.argv[2:]

    from .server import main as server_main

    server_main()
