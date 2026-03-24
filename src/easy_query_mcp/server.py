"""MCP Server main entry point for Minecraft Plugin Development assistant."""

import json
import logging
import sys
import time
from contextlib import AsyncExitStack
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    TextContent,
    Tool,
    Resource,
    ResourceTemplate,
    Prompt,
    PromptArgument,
    PromptMessage,
    GetPromptResult,
)

from .config import load_source_registry
from .database import Database
from . import tools as t1
from . import tools_advanced as t2
from . import tools_docs as t3
from . import tools_knowledge as t4
from . import error_miner as t5
from . import tools_governance as t6
from . import resources as res
from . import prompts as pr

logger = logging.getLogger("easy-query-mcp")


def create_server(config_path: str | None = None) -> Server:
    """Create and configure the MCP server."""
    registry = load_source_registry(config_path)
    logger.info("Using source registry: %s", registry.config_path or config_path or "<auto>")
    logger.info("Using database path: %s", registry.db_path)
    logger.info("Sources loaded: %s", len(registry.sources))
    important_sources = [source.id for source in registry.sources if source.id in {"easyquery", "easyquery-docs", "knowledge-easyquery"}]
    logger.info("Important sources present: %s", ", ".join(important_sources) if important_sources else "<none>")
    db = Database(registry.db_path)
    db.connect()

    # Ensure tables exist (read-only mode still needs schema)
    try:
        db.initialize()
    except Exception:
        pass  # Tables may already exist

    for source in registry.sources:
        try:
            db.upsert_source(
                source.id,
                source.name,
                str(source.resolved_path()),
                ",".join(source.tags),
                source_type=source.type,
                git_url=source.git_url,
                linked_source_id=source.linked_source_id,
                enabled=source.enabled,
            )
        except Exception:
            logger.exception("Failed to register source %s", source.id)

    for source_id in ("easyquery", "easyquery-docs", "knowledge-easyquery"):
        source_row = db.get_source(source_id)
        if source_row is None:
            logger.warning("Source not registered in database: %s", source_id)
        else:
            logger.info(
                "Source state %s: type=%s enabled=%s indexed_at=%s indexed_rev=%s",
                source_id,
                source_row.get("source_type", ""),
                source_row.get("enabled", ""),
                source_row.get("last_indexed_at", ""),
                source_row.get("last_indexed_rev", ""),
            )

    server = Server("easy-query-mcp")

    # ── Tool definitions ───────────────────────────────────────────

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="list_sources",
                description="List configured multi-source knowledge inputs with type, tags, sync state, and index state.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source_type": {"type": "string", "description": "Optional source type filter: code, docs, wiki, knowledge"},
                        "enabled_only": {"type": "boolean", "description": "Only show enabled sources", "default": False},
                    },
                },
            ),
            Tool(
                name="sync_sources",
                description="Clone or pull all git-backed sources from the source registry.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source_type": {"type": "string", "description": "Optional source type filter: code, docs, wiki"},
                    },
                },
            ),
            Tool(
                name="sync_source",
                description="Clone or pull one git-backed source from the source registry.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string", "description": "Source identifier from config/sources.yaml"},
                    },
                    "required": ["source_id"],
                },
            ),
            Tool(
                name="purge_source",
                description="Purge indexed rows for a specific source and clear its index metadata.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string", "description": "Source identifier from config/sources.yaml"}
                    },
                    "required": ["source_id"]
                },
            ),
            Tool(
                name="reindex_source",
                description="Rebuild index data for a specific source, optionally using changed-only mode and sync-first behavior.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string", "description": "Source identifier from config/sources.yaml"},
                        "changed_only": {"type": "boolean", "description": "Only re-index changed files", "default": False},
                        "sync_first": {"type": "boolean", "description": "Sync the source before indexing", "default": False}
                    },
                    "required": ["source_id"]
                },
            ),
            Tool(
                name="validate_sources",
                description="Validate configured sources for path issues, linked-source integrity, sync state, and index coverage.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string", "description": "Optional source identifier to validate"},
                        "include_disabled": {"type": "boolean", "description": "Include disabled sources", "default": False},
                        "check_index_state": {"type": "boolean", "description": "Check index metadata and coverage", "default": True},
                        "check_linked_sources": {"type": "boolean", "description": "Check linked_source_id references", "default": True},
                        "check_paths": {"type": "boolean", "description": "Check whether configured source paths exist and are directories", "default": True}
                    }
                },
            ),
            Tool(
                name="check_index_health",
                description="Inspect index coverage, unindexed sources, and source-level health issues across configured sources.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string", "description": "Optional source identifier to inspect"},
                        "include_disabled": {"type": "boolean", "description": "Include disabled sources", "default": False},
                    },
                },
            ),
            Tool(
                name="index_overview",
                description="Show a compact index coverage overview for each source including files, symbols, docs, examples, cards, and index status.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string", "description": "Optional source identifier to inspect"},
                        "include_disabled": {"type": "boolean", "description": "Include disabled sources", "default": False},
                    },
                },
            ),

            Tool(
                name="summarize_repo_map",
                description="Summarize a repository into entrypoints, modules, domain terms, docs, and risk signals for agent navigation.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "repo_id": {"type": "string", "description": "Repository identifier"},
                        "source_id": {"type": "string", "description": "Optional source family filter"},
                        "include_modules": {"type": "boolean", "description": "Include module grouping output", "default": True},
                        "include_risks": {"type": "boolean", "description": "Include risk signal output", "default": True},
                        "include_docs": {"type": "boolean", "description": "Include linked docs/wiki summary", "default": True}
                    },
                    "required": ["repo_id"]
                },
            ),
            Tool(
                name="trace_call_chain",
                description="Trace a usage-based static call chain for a symbol, including candidate entrypoints and related files.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Symbol name or qualified name"},
                        "repo_id": {"type": "string", "description": "Optional repository filter"},
                        "source_id": {"type": "string", "description": "Optional source family filter"},
                        "max_depth": {"type": "integer", "description": "Maximum traversal depth", "default": 3},
                        "include_snippets": {"type": "boolean", "description": "Include matched usage lines", "default": True},
                        "include_entrypoints": {"type": "boolean", "description": "Include inferred command/listener/packet/controller entrypoints", "default": True}
                    },
                    "required": ["symbol"]
                },
            ),
            Tool(
                name="search_symbol",
                description="Search for classes, functions, methods, annotations by name, signature, or qualified name. Supports source-aware code family filtering and kind filters.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search term (name, partial name, or signature fragment)"},
                        "repo_id": {"type": "string", "description": "Optional: filter by repository (e.g. easyquery, easyquery-docs)"},
                        "source_id": {"type": "string", "description": "Optional: source family filter from sources.yaml"},
                        "kind": {"type": "string", "description": "Optional: filter by kind (class, interface, function, method, enum, annotation, object)"},
                        "limit": {"type": "integer", "description": "Max results (default 30)", "default": 30},
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="read_source",
                description="Read source code from a code source. Supports source-aware fallback to linked code sources and line range selection.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "repo_id": {"type": "string", "description": "Repository identifier"},
                        "source_id": {"type": "string", "description": "Optional source id from sources.yaml"},
                        "path": {"type": "string", "description": "Relative file path within the repo"},
                        "start_line": {"type": "integer", "description": "Optional: start line (1-based)"},
                        "end_line": {"type": "integer", "description": "Optional: end line (1-based, inclusive)"},
                    },
                    "required": ["repo_id", "path"],
                },
            ),
            Tool(
                name="get_examples",
                description="Search for code examples, demos, tests, and samples related to a query.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search term (API name, concept, or keyword)"},
                        "repo_id": {"type": "string", "description": "Optional: filter by repository"},
                        "source_id": {"type": "string", "description": "Optional: source family filter from sources.yaml"},
                        "limit": {"type": "integer", "description": "Max results (default 20)", "default": 20},
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="get_api_summary",
                description="Get a summary of an API symbol including type, location, signature, and suggested next steps.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Symbol name to look up"},
                    },
                    "required": ["symbol"],
                },
            ),
            Tool(
                name="find_usages",
                description="Find where a symbol is used across indexed repositories. Returns file locations and matching lines.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Symbol name to search for"},
                        "repo_id": {"type": "string", "description": "Optional: filter by repository"},
                        "source_id": {"type": "string", "description": "Optional: source family filter from sources.yaml"},
                        "limit": {"type": "integer", "description": "Max results (default 30)", "default": 30},
                    },
                    "required": ["symbol"],
                },
            ),
            Tool(
                name="search_by_concept",
                description="Search by concept or natural language description. Finds relevant knowledge cards, docs, wiki, examples, symbols, and source with source-aware retrieval priority.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "concept": {"type": "string", "description": "Natural language description (e.g. 'how to paginate a joined query')"},
                        "repo_id": {"type": "string", "description": "Optional: filter by repository"},
                        "source_id": {"type": "string", "description": "Optional: source family filter from sources.yaml"},
                        "limit": {"type": "integer", "description": "Max results per category (default 20)", "default": 20},
                    },
                    "required": ["concept"],
                },
            ),
            Tool(
                name="explain_error",
                description="Analyze an EasyQuery-related error log or exception and provide diagnosis with fix suggestions.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "error_text": {"type": "string", "description": "Error message, stack trace, or exception text"},
                    },
                    "required": ["error_text"],
                },
            ),
            Tool(
                name="generate_agent_prompt",
                description="Generate a structured prompt for a code agent. Supports bugfix, feature, architecture_review, test_generation.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task_type": {"type": "string", "description": "One of: bugfix, feature, architecture_review, test_generation"},
                        "context": {"type": "string", "description": "Additional context (error message, requirement, etc.)"},
                        "target": {"type": "string", "description": "Target class/module/file"},
                    },
                    "required": ["task_type"],
                },
            ),
            Tool(
                name="search_docs",
                description="Search documentation (wiki, markdown, guides) by keyword, title, or content.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search term (keyword, title, or content fragment)"},
                        "repo_id": {"type": "string", "description": "Optional: filter by repository"},
                        "source_id": {"type": "string", "description": "Optional: source family or docs source id"},
                        "limit": {"type": "integer", "description": "Max results (default 20)", "default": 20},
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="search_wiki",
                description="Search wiki sources by keyword, title, or content. Supports GitHub wiki and local wiki sources.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search term (keyword, title, or content fragment)"},
                        "repo_id": {"type": "string", "description": "Optional: filter by repository"},
                        "source_id": {"type": "string", "description": "Optional: source family or wiki source id"},
                        "limit": {"type": "integer", "description": "Max results (default 20)", "default": 20},
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="read_doc",
                description="Read a documentation or wiki file from a source. Supports linked docs/wiki source fallback and line range selection.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "repo_id": {"type": "string", "description": "Repository identifier"},
                        "source_id": {"type": "string", "description": "Optional source id from sources.yaml"},
                        "path": {"type": "string", "description": "Relative path to the doc file"},
                        "start_line": {"type": "integer", "description": "Optional: start line (1-based)"},
                        "end_line": {"type": "integer", "description": "Optional: end line (1-based, inclusive)"},
                    },
                    "required": ["repo_id", "path"],
                },
            ),
            Tool(
                name="get_doc_summary",
                description="Get a summary of a documentation page including title, tags, summary text, and related symbols.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Doc path, title fragment, or doc_id"},
                        "source_id": {"type": "string", "description": "Optional: source family or docs/wiki source id"},
                    },
                    "required": ["symbol"],
                },
            ),
            Tool(
                name="search_knowledge_cards",
                description="Search knowledge cards (structured knowledge about lifecycle, threading, transactions, testing patterns). Supports category filter.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search term (keyword, concept, or topic)"},
                        "category": {"type": "string", "description": "Optional: filter by category (lifecycle, threading, packet, transaction, testing, architecture)"},
                        "source_id": {"type": "string", "description": "Optional: knowledge source id filter"},
                        "limit": {"type": "integer", "description": "Max results (default 20)", "default": 20},
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="read_knowledge_card",
                description="Read a complete knowledge card by slug or card_id. Returns full content with best practices, pitfalls, and related APIs.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "slug": {"type": "string", "description": "Card slug (e.g. 'easyquery-transaction') or card_id"},
                    },
                    "required": ["slug"],
                },
            ),
            Tool(
                name="get_knowledge_card_summary",
                description="Get a brief summary of a knowledge card with title, tags, when to use, and what to avoid.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "slug": {"type": "string", "description": "Card slug or card_id"},
                    },
                    "required": ["slug"],
                },
            ),
            Tool(
                name="explain_knowledge_decision",
                description="Explain why a knowledge card was recommended, including score breakdown, freshness reasons, relations, and evidence.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "slug": {"type": "string", "description": "Knowledge card slug or card_id"},
                        "query": {"type": "string", "description": "Optional original query for scoring explanation"},
                    },
                    "required": ["slug"],
                },
            ),
            Tool(
                name="suggest_knowledge_cards",
                description="Suggest relevant knowledge cards based on a task description. Useful before starting a bugfix or feature.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task": {"type": "string", "description": "Task description (e.g. 'fix packet registration issue')"},
                        "limit": {"type": "integer", "description": "Max suggestions (default 5)", "default": 5},
                    },
                    "required": ["task"],
                },
            ),
            Tool(
                name="diagnose_from_log",
                description="Analyze a server log or error output. Matches error patterns, finds related knowledge cards, suggests debugging steps. More powerful than explain_error for full log analysis.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "log_text": {"type": "string", "description": "Server log, error output, or stack trace text"},
                        "repo_hint": {"type": "string", "description": "Optional: hint which repo to focus search on"},
                    },
                    "required": ["log_text"],
                },
            ),
            Tool(
                name="list_knowledge_cards",
                description="List published knowledge cards with optional category or freshness filters.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "description": "Optional category filter"},
                        "lifecycle_status": {"type": "string", "description": "Optional freshness filter: active, review, stale"},
                        "limit": {"type": "integer", "description": "Max cards (default 50)", "default": 50},
                    },
                },
            ),
            Tool(
                name="get_card_relations",
                description="Inspect graph relations connected to a knowledge card slug.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "slug": {"type": "string", "description": "Knowledge card slug"},
                        "limit": {"type": "integer", "description": "Max relations (default 20)", "default": 20},
                    },
                    "required": ["slug"],
                },
            ),
            Tool(
                name="trace_knowledge_path",
                description="Trace a short relation path between two knowledge cards.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source_slug": {"type": "string", "description": "Start card slug"},
                        "target_slug": {"type": "string", "description": "Target card slug"},
                        "max_depth": {"type": "integer", "description": "Max traversal depth (default 3)", "default": 3},
                    },
                    "required": ["source_slug", "target_slug"],
                },
            ),
            Tool(
                name="list_candidate_cards",
                description="List candidate/reviewed/published cards in the mining pipeline.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "description": "Optional status filter"},
                        "category": {"type": "string", "description": "Optional category filter"},
                        "limit": {"type": "integer", "description": "Max cards (default 50)", "default": 50},
                    },
                },
            ),
            Tool(
                name="review_candidate_card",
                description="Approve or reject a candidate card in the mining pipeline.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "card_id": {"type": "string", "description": "Candidate card_id or slug"},
                        "approve": {"type": "boolean", "description": "True to approve, false to reject", "default": True},
                        "reviewer": {"type": "string", "description": "Reviewer name or handle"},
                        "owner": {"type": "string", "description": "Owner or maintainer of the card"},
                        "notes": {"type": "string", "description": "Review notes and rationale"},
                        "change_summary": {"type": "string", "description": "Summary of what changed and why"},
                        "evidence_quality": {"type": "string", "description": "Evidence quality label: low, medium, high"},
                    },
                    "required": ["card_id"],
                },
            ),
            Tool(
                name="publish_candidate_card",
                description="Publish a candidate card into the main knowledge card store after release-gate checks.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "card_id": {"type": "string", "description": "Candidate card_id or slug"},
                        "fixture_dir": {"type": "string", "description": "Optional evaluation fixture directory override"},
                        "require_reviewed": {"type": "boolean", "description": "Require candidate status=reviewed before publish", "default": True},
                        "min_evidence_count": {"type": "integer", "description": "Minimum evidence count required by the publish gate", "default": 1},
                    },
                    "required": ["card_id"],
                },
            ),
            Tool(
                name="scan_architecture_risks",
                description="Detect architecture anti-patterns from indexed code and optionally persist them as candidate cards.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "repo_id": {"type": "string", "description": "Optional repo filter"},
                        "persist": {"type": "boolean", "description": "Persist findings as candidate cards", "default": False},
                    },
                },
            ),
            Tool(
                name="mine_fix_patterns",
                description="Mine reusable fix patterns from local git history and optionally persist them as candidate cards.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "repo_id": {"type": "string", "description": "Optional repo filter"},
                        "max_commits": {"type": "integer", "description": "How many commits to inspect per repo (default 120)", "default": 120},
                        "persist": {"type": "boolean", "description": "Persist findings as candidate cards", "default": False},
                    },
                },
            ),
            Tool(
                name="scan_architecture_drift",
                description="Detect architecture drift, missing pattern evidence, and anti-pattern pressure against published architecture knowledge.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "repo_id": {"type": "string", "description": "Optional repo filter"},
                        "limit": {"type": "integer", "description": "Max findings to return (default 30)", "default": 30},
                        "anti_pattern_threshold": {"type": "integer", "description": "Minimum anti-pattern hits before surfacing drift", "default": 2},
                    },
                },
            ),
            Tool(
                name="get_feedback_summary",
                description="Summarize tool telemetry, weak-signal queries, and stale knowledge cards.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Max rows per section (default 10)", "default": 10},
                    },
                },
            ),
            Tool(
                name="get_knowledge_dashboard",
                description="Summarize knowledge health, usage, publish-gate failures, top cards, and bilingual retrieval quality in one dashboard.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "refresh": {"type": "boolean", "description": "Refresh freshness state before generating the dashboard", "default": False},
                        "limit": {"type": "integer", "description": "Max rows per section (default 10)", "default": 10},
                        "telemetry_limit": {"type": "integer", "description": "How many telemetry rows to inspect (default 2000)", "default": 2000},
                    },
                },
            ),
            Tool(
                name="run_drift_audit",
                description="Audit potentially stale or drifting knowledge, including pattern regressions from telemetry.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "changed_only": {"type": "boolean", "description": "Only report items affected by repos whose HEAD changed", "default": False},
                        "limit": {"type": "integer", "description": "Max findings to return (default 50)", "default": 50},
                        "telemetry_limit": {"type": "integer", "description": "How many recent telemetry rows to inspect (default 400)", "default": 400},
                        "regression_threshold": {"type": "integer", "description": "How many repeated pattern hits trigger a regression finding", "default": 3},
                    },
                },
            ),
            Tool(
                name="get_knowledge_health",
                description="Inspect card freshness and optionally refresh stale scores against current repo heads.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "refresh": {"type": "boolean", "description": "Refresh repo head and stale audit before reporting", "default": False},
                        "limit": {"type": "integer", "description": "Max stale cards to return (default 20)", "default": 20},
                    },
                },
            ),
            Tool(
                name="run_evaluation",
                description="Run regression fixtures, compare against baseline, or execute the release gate.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "fixture_dir": {"type": "string", "description": "Optional fixture directory override"},
                        "gate": {"type": "boolean", "description": "Run release gate checks instead of evaluation only", "default": False},
                        "candidate_id": {"type": "string", "description": "Optional candidate card to gate"},
                        "report_path": {"type": "string", "description": "Optional JSON or Markdown report path"},
                    },
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        start = time.perf_counter()
        status = "error"
        query_text = _extract_query_text(arguments)
        try:
            result = _dispatch_tool(db, name, arguments, config_path=config_path)
            status = _extract_status(result)
        except Exception as e:
            logger.exception(f"Tool {name} failed")
            result = json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)
            status = "error"
        finally:
            latency_ms = round((time.perf_counter() - start) * 1000.0, 2)
            try:
                db.log_tool_telemetry(
                    name,
                    status,
                    latency_ms,
                    query_text=query_text,
                    details=_extract_telemetry_details(name, arguments, result),
                )
            except Exception:
                logger.exception("Failed to log telemetry for tool %s", name)
        return [TextContent(type="text", text=result)]

    # ── Resource definitions ───────────────────────────────────────

    @server.list_resources()
    async def list_resources() -> list[Resource]:
        items = res.list_resources(db=db)
        return [
            Resource(
                uri=r["uri"],
                name=r["name"],
                description=r["description"],
                mimeType=r.get("mimeType", "text/markdown"),
            )
            for r in items
        ]

    @server.read_resource()
    async def read_resource(uri: str) -> str:
        uri_str = str(uri)
        content = res.get_resource(uri_str, db=db)
        if content is None:
            return f"Resource not found: {uri_str}"
        return content

    # ── Prompt definitions ─────────────────────────────────────────

    @server.list_prompts()
    async def list_prompts() -> list[Prompt]:
        items = pr.list_prompts()
        return [
            Prompt(
                name=p["name"],
                description=p["description"],
                arguments=[
                    PromptArgument(
                        name=a["name"],
                        description=a["description"],
                        required=a.get("required", False),
                    )
                    for a in p["arguments"]
                ],
            )
            for p in items
        ]

    @server.get_prompt()
    async def get_prompt(name: str, arguments: dict[str, str] | None = None) -> GetPromptResult:
        args = arguments or {}
        content = pr.get_prompt(name, args, db=db)
        if content is None:
            return GetPromptResult(
                description=f"Unknown prompt: {name}",
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(type="text", text=f"Unknown prompt: {name}"),
                    )
                ],
            )
        return GetPromptResult(
            description=f"Prompt: {name}",
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(type="text", text=content),
                )
            ],
        )

    return server


def _dispatch_tool(db: Database, name: str, args: dict[str, Any],
                   config_path: str | None = None) -> str:
    """Route tool calls to the appropriate handler."""
    if name == "list_sources":
        return t6.list_sources_tool(db,
                                    config_path=config_path,
                                    source_type=args.get("source_type"),
                                    enabled_only=args.get("enabled_only", False))
    elif name == "sync_sources":
        return t6.sync_sources_tool(db,
                                    config_path=config_path,
                                    source_type=args.get("source_type"))
    elif name == "sync_source":
        return t6.sync_source_tool(db,
                                   args["source_id"],
                                   config_path=config_path)
    elif name == "purge_source":
        return t6.purge_source_tool(db,
                                    args["source_id"],
                                    config_path=config_path)
    elif name == "reindex_source":
        return t6.reindex_source_tool(db,
                                      args["source_id"],
                                      config_path=config_path,
                                      changed_only=args.get("changed_only", False),
                                      sync_first=args.get("sync_first", False))
    elif name == "validate_sources":
        return t6.validate_sources_tool(
            db,
            config_path=config_path,
            source_id=args.get("source_id"),
            include_disabled=args.get("include_disabled", False),
            check_index_state=args.get("check_index_state", True),
            check_linked_sources=args.get("check_linked_sources", True),
            check_paths=args.get("check_paths", True),
        )
    elif name == "check_index_health":
        return t6.check_index_health_tool(db,
                                          config_path=config_path,
                                          source_id=args.get("source_id"),
                                          include_disabled=args.get("include_disabled", False))
    elif name == "index_overview":
        return t6.index_overview_tool(db,
                                      config_path=config_path,
                                      source_id=args.get("source_id"),
                                      include_disabled=args.get("include_disabled", False))
    elif name == "summarize_repo_map":

        return t6.summarize_repo_map_tool(
            db,
            repo_id=args["repo_id"],
            source_id=args.get("source_id"),
            include_modules=args.get("include_modules", True),
            include_risks=args.get("include_risks", True),
            include_docs=args.get("include_docs", True),
        )
    elif name == "trace_call_chain":
        return t6.trace_call_chain_tool(
            db,
            symbol=args["symbol"],
            repo_id=args.get("repo_id"),
            source_id=args.get("source_id"),
            max_depth=args.get("max_depth", 3),
            include_snippets=args.get("include_snippets", True),
            include_entrypoints=args.get("include_entrypoints", True),
        )
    elif name == "search_symbol":
        return t1.search_symbol(db, args["query"],
                                repo_id=args.get("repo_id"),
                                source_id=args.get("source_id"),
                                kind=args.get("kind"),
                                limit=args.get("limit", 30),
                                offset=args.get("offset", 0))
    elif name == "read_source":
        return t1.read_source(db, args["repo_id"], args["path"],
                              source_id=args.get("source_id"),
                              start_line=args.get("start_line"),
                              end_line=args.get("end_line"))
    elif name == "get_examples":
        return t1.get_examples(db, args["query"],
                               repo_id=args.get("repo_id"),
                               source_id=args.get("source_id"),
                               limit=args.get("limit", 20))
    elif name == "get_api_summary":
        return t1.get_api_summary(db, args["symbol"])
    elif name == "find_usages":
        return t2.find_usages(db, args["symbol"],
                              repo_id=args.get("repo_id"),
                              source_id=args.get("source_id"),
                              limit=args.get("limit", 30))
    elif name == "search_by_concept":
        return t2.search_by_concept(db, args["concept"],
                                    repo_id=args.get("repo_id"),
                                    source_id=args.get("source_id"),
                                    limit=args.get("limit", 20))
    elif name == "explain_error":
        return t2.explain_error(db, args["error_text"])
    elif name == "generate_agent_prompt":
        return t2.generate_agent_prompt(db, args["task_type"],
                                        context=args.get("context", ""),
                                        target=args.get("target", ""))
    elif name == "search_docs":
        return t3.search_docs(db, args["query"],
                              repo_id=args.get("repo_id"),
                              source_id=args.get("source_id"),
                              limit=args.get("limit", 20))
    elif name == "search_wiki":
        return t3.search_wiki(db, args["query"],
                              repo_id=args.get("repo_id"),
                              source_id=args.get("source_id"),
                              limit=args.get("limit", 20))
    elif name == "read_doc":
        return t3.read_doc(db, args["repo_id"], args["path"],
                           source_id=args.get("source_id"),
                           start_line=args.get("start_line"),
                           end_line=args.get("end_line"))
    elif name == "get_doc_summary":
        return t3.get_doc_summary(db, args["symbol"],
                                  source_id=args.get("source_id"))
    elif name == "search_knowledge_cards":
        return t4.search_knowledge_cards(db, args["query"],
                                         category=args.get("category"),
                                         source_id=args.get("source_id"),
                                         limit=args.get("limit", 20),
                                         offset=args.get("offset", 0))
    elif name == "read_knowledge_card":
        return t4.read_knowledge_card(db, args["slug"])
    elif name == "get_knowledge_card_summary":
        return t4.get_knowledge_card_summary(db, args["slug"])
    elif name == "explain_knowledge_decision":
        return t4.explain_knowledge_decision(db, args["slug"], query=args.get("query", ""))
    elif name == "suggest_knowledge_cards":
        return t4.suggest_knowledge_cards(db, args["task"],
                                          limit=args.get("limit", 5))
    elif name == "diagnose_from_log":
        from .error_miner import diagnose_from_log
        result = diagnose_from_log(db, args["log_text"],
                                   repo_hint=args.get("repo_hint"))
        return json.dumps(result, ensure_ascii=False, indent=2)
    elif name == "list_knowledge_cards":
        return t6.list_knowledge_cards(db,
                                       category=args.get("category"),
                                       lifecycle_status=args.get("lifecycle_status"),
                                       limit=args.get("limit", 50))
    elif name == "get_card_relations":
        return t6.get_card_relations(db, args["slug"],
                                     limit=args.get("limit", 20))
    elif name == "trace_knowledge_path":
        return t6.trace_knowledge_path(db, args["source_slug"], args["target_slug"],
                                       max_depth=args.get("max_depth", 3))
    elif name == "list_candidate_cards":
        return t6.list_candidate_cards(db,
                                       status=args.get("status"),
                                       category=args.get("category"),
                                       limit=args.get("limit", 50))
    elif name == "review_candidate_card":
        return t6.review_candidate_card(db, args["card_id"],
                                        approve=args.get("approve", True),
                                        reviewer=args.get("reviewer", ""),
                                        owner=args.get("owner", ""),
                                        notes=args.get("notes", ""),
                                        change_summary=args.get("change_summary", ""),
                                        evidence_quality=args.get("evidence_quality", ""))
    elif name == "publish_candidate_card":
        return t6.publish_candidate_card(db,
                                         args["card_id"],
                                         fixture_dir=args.get("fixture_dir"),
                                         require_reviewed=args.get("require_reviewed", True),
                                         min_evidence_count=args.get("min_evidence_count", 1))
    elif name == "scan_architecture_risks":
        return t6.scan_architecture_risks(db,
                                          repo_id=args.get("repo_id"),
                                          persist=args.get("persist", False))
    elif name == "mine_fix_patterns":
        return t6.mine_fix_patterns(db,
                                    repo_id=args.get("repo_id"),
                                    max_commits=args.get("max_commits", 120),
                                    persist=args.get("persist", False))
    elif name == "scan_architecture_drift":
        return t6.scan_architecture_drift_tool(db,
                                               repo_id=args.get("repo_id"),
                                               limit=args.get("limit", 30),
                                               anti_pattern_threshold=args.get("anti_pattern_threshold", 2))
    elif name == "get_feedback_summary":
        return t6.get_feedback_summary(db, limit=args.get("limit", 10))
    elif name == "get_knowledge_dashboard":
        return t6.get_knowledge_dashboard(db,
                                          limit=args.get("limit", 10),
                                          refresh=args.get("refresh", False),
                                          telemetry_limit=args.get("telemetry_limit", 2000))
    elif name == "run_drift_audit":
        return t6.run_drift_audit_tool(db,
                                       changed_only=args.get("changed_only", False),
                                       limit=args.get("limit", 50),
                                       telemetry_limit=args.get("telemetry_limit", 400),
                                       regression_threshold=args.get("regression_threshold", 3))
    elif name == "get_knowledge_health":
        return t6.get_knowledge_health(db,
                                       refresh=args.get("refresh", False),
                                       limit=args.get("limit", 20))
    elif name == "run_evaluation":
        return t6.run_evaluation_suite(db,
                                       fixture_dir=args.get("fixture_dir"),
                                       gate=args.get("gate", False),
                                       candidate_id=args.get("candidate_id"),
                                       report_path=args.get("report_path"))
    else:
        return json.dumps({"status": "error", "message": f"Unknown tool: {name}"})


def _extract_status(result: str) -> str:
    try:
        payload = json.loads(result)
    except Exception:
        return "ok"
    if isinstance(payload, dict):
        return str(payload.get("status", "ok"))
    return "ok"


def _extract_query_text(arguments: dict[str, Any]) -> str:
    for key in (
        "query", "concept", "task", "error_text", "log_text", "symbol",
        "slug", "source_slug", "target_slug", "context", "problem",
    ):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:500]
    return ""


def _extract_telemetry_details(name: str, arguments: dict[str, Any], result: str) -> dict[str, Any]:
    details: dict[str, Any] = {"argument_keys": sorted(arguments.keys())}
    try:
        payload = json.loads(result)
    except Exception:
        return details
    if not isinstance(payload, dict):
        return details

    if name in {"read_knowledge_card", "get_knowledge_card_summary"}:
        card = payload.get("card", {})
        details["accessed_slug"] = card.get("slug") if isinstance(card, dict) else arguments.get("slug")

    if name == "explain_knowledge_decision":
        card = payload.get("card", {})
        if isinstance(card, dict):
            details["accessed_slug"] = card.get("slug", arguments.get("slug", ""))
        breakdown = payload.get("score_breakdown", {})
        if isinstance(breakdown, dict):
            details["score_breakdown_fields"] = sorted(breakdown.keys())

    if name in {"search_knowledge_cards", "suggest_knowledge_cards"}:
        rows = payload.get("results") or payload.get("suggestions") or []
        if isinstance(rows, list):
            details["selected_slugs"] = [row.get("slug", "") for row in rows[:5] if isinstance(row, dict) and row.get("slug")]
            details["selected_categories"] = [row.get("category", "") for row in rows[:5] if isinstance(row, dict)]
            details["selected_fix_patterns"] = [
                row.get("slug", "")
                for row in rows[:5]
                if isinstance(row, dict) and row.get("category") == "fix-pattern" and row.get("slug")
            ]
        intent = payload.get("intent", {})
        if isinstance(intent, dict):
            details["query_intent"] = intent.get("query_mode", "")
            details["normalized_issue_terms"] = intent.get("normalized_terms", [])[:8]
        if details.get("query_intent") in {"fix_first", "repair_path"} and not details.get("selected_fix_patterns"):
            details["false_positive_signal"] = True
            details.setdefault("false_positive_reasons", []).append("issue_query_without_fix_pattern")

    if name == "search_by_concept":
        details["source_hits"] = {
            key: bool(payload.get(key))
            for key in ("knowledge_cards", "fix_patterns", "docs", "wiki", "examples", "symbols", "files")
        }
        rows = payload.get("knowledge_cards", []) or []
        details["selected_slugs"] = [row.get("slug", "") for row in rows[:5] if isinstance(row, dict) and row.get("slug")]
        details["selected_fix_patterns"] = [row.get("slug", "") for row in payload.get("fix_patterns", [])[:5] if isinstance(row, dict) and row.get("slug")]
        details["selected_categories"] = [row.get("category", "") for row in rows[:5] if isinstance(row, dict)]
        intent = payload.get("intent", {})
        if isinstance(intent, dict):
            details["query_intent"] = intent.get("query_mode", "")
            details["normalized_issue_terms"] = intent.get("normalized_terms", [])[:8]
        if details.get("query_intent") in {"fix_first", "repair_path"} and not details.get("selected_fix_patterns"):
            details["false_positive_signal"] = True
            details.setdefault("false_positive_reasons", []).append("concept_issue_query_without_fix_pattern")

    if name in {"diagnose_from_log", "explain_error"}:
        details["matched_patterns"] = [
            row.get("pattern", "")
            for row in payload.get("matched_patterns", [])
            if isinstance(row, dict) and row.get("pattern")
        ]
        details["proven_fix_patterns"] = [
            row.get("slug", "")
            for row in payload.get("proven_fix_patterns", [])
            if isinstance(row, dict) and row.get("slug")
        ]
        details["selected_slugs"] = [
            row.get("slug", "")
            for row in payload.get("related_knowledge_cards", [])
            if isinstance(row, dict) and row.get("slug")
        ]
        if payload.get("status") == "partial_match":
            details["false_positive_signal"] = True
            details.setdefault("false_positive_reasons", []).append("partial_match_diagnosis")

    if name in {"publish_candidate_card", "run_evaluation"}:
        gate = payload.get("gate")
        if gate is None and isinstance(payload.get("summary"), dict):
            gate = payload["summary"].get("gate")
        if isinstance(gate, dict):
            failures = []
            for check in gate.get("checks", []):
                if not isinstance(check, dict) or check.get("passed", True):
                    continue
                failures.append(str(check.get("name", "unknown")))
            details["gate_failures"] = failures

    if name == "run_drift_audit" and isinstance(payload.get("audit"), dict):
        details["drift_statuses"] = list((payload["audit"].get("summary", {}) or {}).get("by_status", {}).keys())

    if name == "scan_architecture_drift" and isinstance(payload.get("architecture_drift"), dict):
        details["architecture_drift_types"] = list((payload["architecture_drift"].get("summary", {}) or {}).get("by_type", {}).keys())

    if name == "get_knowledge_dashboard" and isinstance(payload.get("dashboard"), dict):
        details["dashboard_sections"] = sorted(payload["dashboard"].keys())

    if name == "scan_architecture_risks":
        details["finding_count"] = payload.get("findings", 0)

    if name == "mine_fix_patterns":
        details["selected_fix_patterns"] = [
            item.get("slug", "")
            for item in payload.get("top_patterns", [])[:5]
            if isinstance(item, dict) and item.get("slug")
        ]

    return details


async def _run_server(config_path: str | None = None) -> None:
    """Run the MCP server over stdio."""
    server = create_server(config_path)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


async def _run_streamable_http_server(config_path: str | None = None,
                                      host: str = "0.0.0.0", port: int = 8080) -> None:
    """Run the MCP server over Streamable HTTP."""
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    import uvicorn

    server = create_server(config_path)
    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=False,
        stateless=False,
    )
    lifespan_stack = AsyncExitStack()

    async def app(scope, receive, send):
        """Minimal ASGI app routing Streamable HTTP transport and health."""
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    try:
                        await lifespan_stack.enter_async_context(session_manager.run())
                        await send({"type": "lifespan.startup.complete"})
                    except Exception as exc:
                        logger.exception("Streamable HTTP startup failed")
                        await send({
                            "type": "lifespan.startup.failed",
                            "message": str(exc),
                        })
                        return
                elif message["type"] == "lifespan.shutdown":
                    await lifespan_stack.aclose()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return

        if scope["type"] != "http":
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")

        if path == "/health" and method == "GET":
            await _send_json(send, 200, {"status": "ok", "server": "easy-query-mcp"})
        elif path.rstrip("/") == "/mcp":
            query_string = (scope.get("query_string") or b"").decode(errors="ignore")
            headers = {k.decode(errors="ignore"): v.decode(errors="ignore") for k, v in (scope.get("headers") or [])}
            logger.info("Streamable HTTP request: method=%s path=%s query=%s client=%s", method, path, query_string, scope.get("client"))
            try:
                await session_manager.handle_request(scope, receive, send)
            except Exception as exc:
                logger.exception("Streamable HTTP request failed: path=%s query=%s headers=%s", path, query_string, headers)
                await _send_json(send, 500, {
                    "error": "streamable_http_failed",
                    "message": str(exc),
                    "path": path,
                    "query": query_string,
                })
        elif path == "/sse" or path.startswith("/messages"):
            await _send_json(send, 410, {
                "error": "deprecated_transport",
                "message": "SSE transport has been deprecated.",
                "hint": "Use Streamable HTTP on /mcp instead.",
                "path": path,
            })
        else:
            await _send_json(send, 404, {"error": "not found"})

    logger.info(f"Starting Streamable HTTP server on http://{host}:{port}")
    logger.info(f"  MCP endpoint:  http://{host}:{port}/mcp")
    logger.info(f"  Health check:  http://{host}:{port}/health")
    logger.info("  Deprecated:    /sse and /messages* now return HTTP 410")

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    uv_server = uvicorn.Server(config)
    await uv_server.serve()


async def _send_json(send, status: int, body: dict) -> None:
    """Send a simple JSON response via raw ASGI."""
    import json as _json
    payload = _json.dumps(body).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            [b"content-type", b"application/json"],
            [b"content-length", str(len(payload)).encode()],
        ],
    })
    await send({"type": "http.response.body", "body": payload})


def main() -> None:
    """Entry point for easy-query-mcp-server command."""
    import asyncio
    import argparse

    parser = argparse.ArgumentParser(description="EasyQuery MCP Server")
    parser.add_argument("--config", "-c", default=None,
                        help="Path to config/sources.yaml (legacy repos.yaml still supported)")
    parser.add_argument("--streamable-http", "--http", dest="streamable_http", action="store_true",
                        help="Run as Streamable HTTP server instead of stdio")
    parser.add_argument("--sse", action="store_true",
                        help="Deprecated alias for --streamable-http")
    parser.add_argument("--host", default="0.0.0.0",
                        help="HTTP server host (default: 0.0.0.0)")
    parser.add_argument("--port", "-p", type=int, default=8080,
                        help="HTTP server port (default: 8080)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    if args.sse:
        logger.warning("--sse is deprecated; switching to Streamable HTTP on /mcp")
        args.streamable_http = True

    if args.streamable_http:
        asyncio.run(_run_streamable_http_server(args.config, args.host, args.port))
    else:
        asyncio.run(_run_server(args.config))


if __name__ == "__main__":
    main()
