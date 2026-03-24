"""Source registry management and git-based source synchronization."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import SourceConfig, SourceRegistry, load_source_registry
from .database import Database


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


class SourceManager:
    """Manage source registry entries and git synchronization."""

    def __init__(self, config_path: Optional[str] = None,
                 db: Optional[Database] = None) -> None:
        self.registry: SourceRegistry = load_source_registry(config_path)
        self.base_dir = Path(self.registry.config_path).resolve().parent if self.registry.config_path else Path.cwd()
        self.db = db

    def load_sources(self) -> list[dict]:
        """Load sources from registry and optionally merge DB status."""
        records: list[dict] = []
        db_status = {}
        if self.db is not None:
            self.db.initialize()
            db_status = {row["id"]: row for row in self.db.list_sources()}
        for source in self.registry.sources:
            item = source.as_dict(self.base_dir)
            if source.id in db_status:
                item.update({
                    "last_synced_at": db_status[source.id].get("last_synced_at", ""),
                    "last_synced_rev": db_status[source.id].get("last_synced_rev", ""),
                    "last_indexed_at": db_status[source.id].get("last_indexed_at", ""),
                    "last_indexed_rev": db_status[source.id].get("last_indexed_rev", ""),
                })
            records.append(item)
        return records

    def get_source(self, source_id: str) -> Optional[SourceConfig]:
        return self.registry.get(source_id)

    def list_sources(self, source_type: Optional[str] = None,
                     enabled_only: bool = False) -> list[dict]:
        items = self.load_sources()
        if source_type:
            items = [item for item in items if item["type"] == source_type]
        if enabled_only:
            items = [item for item in items if item.get("enabled", True)]
        return items

    def sync_source(self, source_id: str) -> dict:
        """Clone or pull a single git-backed source."""
        source = self.get_source(source_id)
        if source is None:
            return {"status": "error", "source_id": source_id, "message": "source not found"}

        resolved_path = source.resolved_path(self.base_dir)
        result = {
            "status": "skipped",
            "source_id": source.id,
            "type": source.type,
            "path": str(resolved_path),
            "git_url": source.git_url,
            "action": "none",
        }

        if not source.git_url:
            result["message"] = "source has no git_url; nothing to sync"
            self._record_source(source)
            return result

        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        if not resolved_path.exists():
            command = ["git", "clone", source.git_url, str(resolved_path)]
            action = "clone"
        else:
            command = ["git", "-C", str(resolved_path), "pull", "--ff-only"]
            action = "pull"

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            result["status"] = "error"
            result["action"] = action
            result["message"] = str(exc)
            return result

        result["action"] = action
        result["stdout"] = completed.stdout.strip()[-1000:]
        result["stderr"] = completed.stderr.strip()[-1000:]
        result["status"] = "ok" if completed.returncode == 0 else "error"
        if completed.returncode != 0:
            return result

        revision = _git_head(resolved_path)
        result["last_synced_at"] = _utc_now()
        result["last_synced_rev"] = revision
        self._record_source(source, synced_at=result["last_synced_at"], synced_rev=revision)
        return result

    def sync_all_sources(self, source_type: Optional[str] = None) -> dict:
        """Sync all git-backed sources, optionally filtered by type."""
        results = []
        for item in self.registry.enabled_sources(source_type=source_type):
            if not item.git_url:
                continue
            results.append(self.sync_source(item.id))
        failed = [item for item in results if item.get("status") == "error"]
        return {
            "status": "error" if failed else "ok",
            "count": len(results),
            "failed": len(failed),
            "results": results,
        }

    def _record_source(self, source: SourceConfig,
                       synced_at: str = "",
                       synced_rev: str = "") -> None:
        if self.db is None:
            return
        self.db.initialize()
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
        if synced_at or synced_rev:
            self.db.update_source_sync_state(source.id, synced_at=synced_at, synced_rev=synced_rev)


def main_sync() -> None:
    """CLI: sync all or selected git-backed sources."""
    parser = argparse.ArgumentParser(description="Clone or pull git-backed sources from config/sources.yaml")
    parser.add_argument("--config", "-c", default=None, help="Path to sources.yaml")
    parser.add_argument("--db", "-d", default=None, help="Database path override")
    parser.add_argument("--source-id", default=None, help="Sync only one source")
    parser.add_argument("--type", default=None, help="Optional source type filter")
    args = parser.parse_args()

    db = None
    if args.db:
        db = Database(args.db)
    else:
        registry = load_source_registry(args.config)
        db = Database(registry.db_path)
    db.initialize()
    try:
        manager = SourceManager(args.config, db=db)
        if args.source_id:
            payload = manager.sync_source(args.source_id)
        else:
            payload = manager.sync_all_sources(source_type=args.type)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if payload.get("status") == "error":
            raise SystemExit(1)
    finally:
        db.close()
