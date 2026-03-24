"""CLI entry point for building the multi-source search index."""

from __future__ import annotations

import argparse
import sys
import time

from .config import load_source_registry
from .database import Database
from .index_pipeline import IndexPipeline
from .source_manager import SourceManager


def main() -> None:
    """Main entry point for the easy-query-mcp-index command."""
    parser = argparse.ArgumentParser(
        description="Build search index for EasyQuery source, docs, and knowledge"
    )
    parser.add_argument(
        "--config", "-c", default=None,
        help="Path to config/sources.yaml (default: auto-discover sources.yaml, then repos.yaml)",
    )
    parser.add_argument(
        "--db", "-d", default=None,
        help="Override database path (default: from source registry)",
    )
    parser.add_argument(
        "--source-id", default=None,
        help="Index only one source by id",
    )
    parser.add_argument(
        "--repo", "-r", default=None,
        help="Legacy alias for --source-id",
    )
    parser.add_argument(
        "--changed-only", action="store_true",
        help="Only re-index files changed since the last indexed revision/timestamp",
    )
    parser.add_argument(
        "--sync-first", action="store_true",
        help="Sync git-backed sources before indexing",
    )
    args = parser.parse_args()

    target_source = args.source_id or args.repo
    registry = load_source_registry(args.config)
    if args.db:
        registry.db_path = args.db

    if not registry.sources:
        print("No sources configured. Please create config/sources.yaml.", file=sys.stderr)
        sys.exit(1)

    if target_source and registry.get(target_source) is None:
        print(f"Source '{target_source}' not found in registry.", file=sys.stderr)
        sys.exit(1)

    db = Database(registry.db_path)
    db.initialize()

    if args.sync_first:
        manager = SourceManager(args.config, db=db)
        if target_source:
            sync_result = manager.sync_source(target_source)
            print(f"Synced {target_source}: {sync_result['status']}")
        else:
            sync_result = manager.sync_all_sources()
            print(f"Synced git sources: {sync_result['count']}")

    print(f"Database: {registry.db_path}")
    print(f"Sources to index: {1 if target_source else len(registry.enabled_sources())}")
    print()

    start = time.time()
    pipeline = IndexPipeline(registry, db)
    stats = pipeline.index(source_id=target_source, changed_only=args.changed_only)
    elapsed = time.time() - start

    print()
    print(f"Indexing complete in {elapsed:.1f}s")
    print(f"  Sources:  {stats['sources']}")
    print(f"  Files:    {stats['files']}")
    print(f"  Symbols:  {stats['symbols']}")
    print(f"  Examples: {stats['examples']}")
    print(f"  Docs:     {stats['docs']}")
    print(f"  Cards:    {stats['cards']}")
    print(f"  Skipped:  {stats['skipped_sources']}")

    db.close()


if __name__ == "__main__":
    main()
