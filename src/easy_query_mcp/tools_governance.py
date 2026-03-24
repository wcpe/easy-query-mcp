"""MCP tools for governance, graph navigation, and evaluation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .anti_pattern_miner import AntiPatternMiner
from .architecture_drift import run_architecture_drift
from .database import Database
from .drift_audit import run_drift_audit
from .evaluation import run_evaluation, run_release_gate
from .fix_miner import FixPatternMiner
from .knowledge_dashboard import build_knowledge_dashboard
from .knowledge_health import get_knowledge_health_summary, refresh_knowledge_health
from .knowledge_pipeline import KnowledgePipeline
from .source_manager import SourceManager
from .source_audit import validate_sources as run_validate_sources, check_index_health as run_check_index_health, get_index_overview as run_get_index_overview
from .analysis_tools import summarize_repo_map as run_summarize_repo_map, trace_call_chain as run_trace_call_chain
from .source_maintenance import purge_source as run_purge_source, reindex_source as run_reindex_source


def list_sources_tool(db: Database, config_path: Optional[str] = None,
                      source_type: Optional[str] = None,
                      enabled_only: bool = False) -> str:
    manager = SourceManager(config_path, db=db)
    sources = manager.list_sources(source_type=source_type, enabled_only=enabled_only)
    return json.dumps({
        "status": "ok",
        "count": len(sources),
        "results": sources,
    }, ensure_ascii=False, indent=2)


def validate_sources_tool(db: Database, config_path: Optional[str] = None,
                          source_id: Optional[str] = None,
                          include_disabled: bool = False,
                          check_index_state: bool = True,
                          check_linked_sources: bool = True,
                          check_paths: bool = True) -> str:
    return run_validate_sources(
        db,
        config_path=config_path,
        source_id=source_id,
        include_disabled=include_disabled,
        check_index_state=check_index_state,
        check_linked_sources=check_linked_sources,
        check_paths=check_paths,
    )


def check_index_health_tool(db: Database, config_path: Optional[str] = None,
                            source_id: Optional[str] = None,
                            include_disabled: bool = False) -> str:
    return run_check_index_health(
        db,
        config_path=config_path,
        source_id=source_id,
        include_disabled=include_disabled,
    )


def index_overview_tool(db: Database, config_path: Optional[str] = None,
                        source_id: Optional[str] = None,
                        include_disabled: bool = False) -> str:
    return run_get_index_overview(
        db,
        config_path=config_path,
        source_id=source_id,
        include_disabled=include_disabled,
    )


def sync_source_tool(db: Database, source_id: str,
                     config_path: Optional[str] = None) -> str:
    manager = SourceManager(config_path, db=db)
    result = manager.sync_source(source_id)
    return json.dumps(result, ensure_ascii=False, indent=2)


def purge_source_tool(db: Database, source_id: str,
                      config_path: Optional[str] = None) -> str:
    return run_purge_source(db, source_id=source_id, config_path=config_path)


def reindex_source_tool(db: Database, source_id: str,
                        config_path: Optional[str] = None,
                        changed_only: bool = False,
                        sync_first: bool = False) -> str:
    return run_reindex_source(
        db,
        source_id=source_id,
        config_path=config_path,
        changed_only=changed_only,
        sync_first=sync_first,
    )


def sync_sources_tool(db: Database, config_path: Optional[str] = None,
                      source_type: Optional[str] = None) -> str:
    manager = SourceManager(config_path, db=db)
    result = manager.sync_all_sources(source_type=source_type)
    return json.dumps(result, ensure_ascii=False, indent=2)


def summarize_repo_map_tool(db: Database, repo_id: str,
                            source_id: Optional[str] = None,
                            include_modules: bool = True,
                            include_risks: bool = True,
                            include_docs: bool = True) -> str:
    return run_summarize_repo_map(
        db,
        repo_id=repo_id,
        source_id=source_id,
        include_modules=include_modules,
        include_risks=include_risks,
        include_docs=include_docs,
    )


def trace_call_chain_tool(db: Database, symbol: str,
                          repo_id: Optional[str] = None,
                          source_id: Optional[str] = None,
                          max_depth: int = 3,
                          include_snippets: bool = True,
                          include_entrypoints: bool = True) -> str:
    return run_trace_call_chain(
        db,
        symbol=symbol,
        repo_id=repo_id,
        source_id=source_id,
        max_depth=max_depth,
        include_snippets=include_snippets,
        include_entrypoints=include_entrypoints,
    )


def list_knowledge_cards(db: Database, category: Optional[str] = None,
                         lifecycle_status: Optional[str] = None,
                         limit: int = 50) -> str:
    cards = db.list_knowledge_cards(category=category, lifecycle_status=lifecycle_status, limit=limit)
    return json.dumps({
        "status": "ok",
        "count": len(cards),
        "results": cards,
    }, ensure_ascii=False, indent=2)


def get_card_relations(db: Database, slug: str, limit: int = 20) -> str:
    relations = db.get_related_cards(slug, limit=limit)
    enriched = []
    for relation in relations:
        other_slug = relation["target_slug"] if relation["source_slug"] == slug else relation["source_slug"]
        card = db.get_knowledge_card(other_slug) or db.get_candidate(other_slug)
        enriched.append({
            **relation,
            "other_slug": other_slug,
            "other_title": card.get("title", other_slug) if card else other_slug,
            "other_category": card.get("category", "") if card else "",
        })
    return json.dumps({
        "status": "ok",
        "slug": slug,
        "count": len(enriched),
        "relations": enriched,
    }, ensure_ascii=False, indent=2)


def trace_knowledge_path(db: Database, source_slug: str, target_slug: str,
                         max_depth: int = 3) -> str:
    path = db.trace_knowledge_path(source_slug, target_slug, max_depth=max_depth)
    if not path:
        return json.dumps({
            "status": "not_found",
            "source_slug": source_slug,
            "target_slug": target_slug,
            "message": "No relation path found within the requested depth.",
        }, ensure_ascii=False, indent=2)

    return json.dumps({
        "status": "ok",
        "source_slug": source_slug,
        "target_slug": target_slug,
        "path": path,
    }, ensure_ascii=False, indent=2)


def list_candidate_cards(db: Database, status: Optional[str] = None,
                         category: Optional[str] = None,
                         limit: int = 50) -> str:
    cards = db.get_candidates(status=status, category=category, limit=limit)
    return json.dumps({
        "status": "ok",
        "count": len(cards),
        "results": cards,
    }, ensure_ascii=False, indent=2)


def review_candidate_card(db: Database, card_id: str, approve: bool = True,
                          reviewer: str = "",
                          owner: str = "",
                          notes: str = "",
                          change_summary: str = "",
                          evidence_quality: str = "") -> str:
    pipeline = KnowledgePipeline(db)
    ok = pipeline.review_card(
        card_id,
        approve=approve,
        reviewer=reviewer,
        review_notes=notes,
        owner=owner,
        change_summary=change_summary,
        evidence_quality=evidence_quality,
    )
    return json.dumps({
        "status": "ok" if ok else "error",
        "card_id": card_id,
        "decision": "approved" if approve else "rejected",
        "reviewer": reviewer,
        "owner": owner,
    }, ensure_ascii=False, indent=2)


def publish_candidate_card(db: Database, card_id: str,
                           fixture_dir: Optional[str] = None,
                           require_reviewed: bool = True,
                           min_evidence_count: int = 1) -> str:
    pipeline = KnowledgePipeline(db)
    slug = pipeline.publish_card(
        card_id,
        fixture_dir=fixture_dir,
        require_reviewed=require_reviewed,
        min_evidence_count=min_evidence_count,
    )
    return json.dumps({
        "status": "ok" if slug else "error",
        "card_id": card_id,
        "published_slug": slug,
        "gate": pipeline.last_gate_result,
    }, ensure_ascii=False, indent=2)


def scan_architecture_risks(db: Database, repo_id: Optional[str] = None,
                            persist: bool = False) -> str:
    miner = AntiPatternMiner(db)
    result = miner.scan(repo_id=repo_id, persist=persist)
    return json.dumps({
        "status": "ok",
        **result,
    }, ensure_ascii=False, indent=2)


def mine_fix_patterns(db: Database, repo_id: Optional[str] = None,
                      max_commits: int = 120,
                      persist: bool = False) -> str:
    miner = FixPatternMiner(db)
    result = miner.mine(repo_id=repo_id, max_commits=max_commits, persist=persist)
    return json.dumps({
        "status": "ok",
        **result,
    }, ensure_ascii=False, indent=2)


def get_feedback_summary(db: Database, limit: int = 10) -> str:
    telemetry = db.get_tool_feedback_summary(limit=limit)
    health = get_knowledge_health_summary(db, limit=limit)
    return json.dumps({
        "status": "ok",
        "telemetry": telemetry,
        "knowledge_health": health,
    }, ensure_ascii=False, indent=2)


def get_knowledge_dashboard(db: Database, limit: int = 10,
                            refresh: bool = False,
                            telemetry_limit: int = 2000) -> str:
    dashboard = build_knowledge_dashboard(
        db,
        limit=limit,
        refresh=refresh,
        telemetry_limit=telemetry_limit,
    )
    return json.dumps({
        "status": "ok",
        "dashboard": dashboard,
    }, ensure_ascii=False, indent=2)


def run_drift_audit_tool(db: Database, changed_only: bool = False,
                         limit: int = 50,
                         telemetry_limit: int = 400,
                         regression_threshold: int = 3) -> str:
    result = run_drift_audit(
        db,
        changed_only=changed_only,
        limit=limit,
        telemetry_limit=telemetry_limit,
        regression_threshold=regression_threshold,
    )
    return json.dumps({
        "status": "ok",
        "audit": result,
    }, ensure_ascii=False, indent=2)


def scan_architecture_drift_tool(db: Database, repo_id: Optional[str] = None,
                                 limit: int = 30,
                                 anti_pattern_threshold: int = 2) -> str:
    result = run_architecture_drift(
        db,
        repo_id=repo_id,
        limit=limit,
        anti_pattern_threshold=anti_pattern_threshold,
    )
    return json.dumps({
        "status": "ok",
        "architecture_drift": result,
    }, ensure_ascii=False, indent=2)


def get_knowledge_health(db: Database, refresh: bool = False,
                         limit: int = 20) -> str:
    audit = refresh_knowledge_health(db) if refresh else None
    summary = get_knowledge_health_summary(db, limit=limit)
    return json.dumps({
        "status": "ok",
        "audit": audit,
        "summary": summary,
    }, ensure_ascii=False, indent=2)


def run_evaluation_suite(db: Database, fixture_dir: Optional[str] = None,
                         gate: bool = False,
                         candidate_id: Optional[str] = None,
                         report_path: Optional[str] = None) -> str:
    root = Path(fixture_dir) if fixture_dir else None
    report = Path(report_path) if report_path else None
    summary = run_release_gate(db, root, report_path=report, candidate_id=candidate_id) if gate else run_evaluation(db, root, report_path=report)
    return json.dumps({
        "status": "ok",
        "summary": summary,
    }, ensure_ascii=False, indent=2)
