"""Static MCP resources for EasyQuery-focused guidance."""

from pathlib import Path

from .architecture_drift import run_architecture_drift
from .config import _find_project_root
from .knowledge_dashboard import build_knowledge_dashboard
from .knowledge_health import get_knowledge_health_summary
from .publishing_policy import POLICY_MARKDOWN

FRAMEWORK_MAP = """# EasyQuery Resource Map

## Indexed Source Types

- **Code**: EasyQuery runtime and examples
- **Docs**: Official EasyQuery documentation
- **Knowledge**: Project-specific EasyQuery cards

## Recommended Query Strategies

1. **Find an API**: `search_symbol`
2. **Find real usage**: `get_examples`
3. **Read exact implementation**: `read_source`
4. **Read docs**: `search_docs` then `read_doc`
5. **Investigate a behavior**: `search_by_concept`
6. **Debug an error**: `explain_error`
"""

PERSISTENCE_GUIDE = """# EasyQuery Persistence Guide

## Recommended Layers

- **Entity Layer**: table/column metadata, converters, logical delete fields
- **Repository Layer**: reusable query and write operations
- **Service Layer**: transactions, orchestration, and business rules

## Common Review Questions

1. Are entities aligned with the live schema?
2. Are repositories readable and typed?
3. Are transactions scoped tightly?
4. Are count and page queries consistent?
5. Are tests validating the target dialect when needed?
"""

TESTING_BEST_PRACTICES = """# EasyQuery Testing Best Practices

## Testing Stack

- **JUnit 5**: test framework
- **H2**: fast repository and transaction tests
- **Target-like DB**: verify dialect-sensitive cases before release

## Key Principles

1. Test repository behavior with real queries, not only mocks
2. Verify pagination and count results together
3. Cover rollback and retry paths for transactional code
4. Watch for H2-only behavior differences
"""

PROJECT_ARCHITECTURE = """# EasyQuery Project Architecture

## Suggested Structure

```text
project/
  entity/
  repository/
  service/
  docs/
  tests/
```

## Principles

- Keep entity metadata close to persistence types
- Keep repositories focused on query composition and writes
- Keep services responsible for orchestration and transactions
- Avoid leaking persistence entities directly into external transport contracts
"""


def get_resource(uri: str, db=None) -> str | None:
    static = {
        "repo://framework-map": FRAMEWORK_MAP,
        "repo://persistence-guide": PERSISTENCE_GUIDE,
        "repo://best-practices/testing": TESTING_BEST_PRACTICES,
        "repo://docs/project-architecture": PROJECT_ARCHITECTURE,
    }
    if uri in static:
        return static[uri]

    if uri == "repo://todo":
        todo_path = _find_project_root() / "TODO.md"
        if todo_path.exists():
            return todo_path.read_text(encoding="utf-8")

    if uri == "repo://knowledge-health" and db is not None:
        summary = get_knowledge_health_summary(db, limit=20)
        lines = ["# Knowledge Health", ""]
        counts = summary.get("counts", {})
        lines.append(f"- Active: {counts.get('active', 0)}")
        lines.append(f"- Review: {counts.get('review', 0)}")
        lines.append(f"- Stale: {counts.get('stale', 0)}")
        lines.append("")
        lines.append("## Highest Stale Score")
        for card in summary.get("stale_cards", [])[:10]:
            lines.append(f"- `{card['slug']}` ({card.get('category', '')}) score={card.get('stale_score', 0)}")
        return "\n".join(lines)

    if uri == "repo://knowledge-dashboard" and db is not None:
        dashboard = build_knowledge_dashboard(db, limit=10, refresh=False, telemetry_limit=500)
        lines = ["# Knowledge Dashboard", ""]
        telemetry = dashboard.get("telemetry", {})
        lines.append(f"- Total telemetry calls: {telemetry.get('total_calls', 0)}")
        lines.append(f"- No-match rate: {telemetry.get('no_match_rate', 0)}%")
        lines.append(f"- Bilingual success rate: {telemetry.get('bilingual_query_success_rate', 0)}%")
        return "\n".join(lines)

    if uri == "repo://architecture-drift" and db is not None:
        drift = run_architecture_drift(db, limit=10)
        lines = ["# Architecture Drift", ""]
        for finding in drift.get("findings", [])[:10]:
            lines.append(f"- `{finding['slug']}` [{finding['type']}] current={finding.get('current_match_count', 0)}")
        return "\n".join(lines)

    if uri == "repo://publishing-collaboration":
        return POLICY_MARKDOWN

    if uri.startswith("repo://knowledge/") and db is not None:
        slug = uri.replace("repo://knowledge/", "")
        card = db.get_knowledge_card(slug)
        if card:
            return _format_card_as_resource(card)

    if uri.startswith("repo://knowledge-evidence/") and db is not None:
        slug = uri.replace("repo://knowledge-evidence/", "")
        explanation = db.explain_knowledge_card(slug)
        if explanation:
            return _format_card_evidence_resource(explanation)

    return None


def _format_card_as_resource(card: dict) -> str:
    parts = [f"# {card['title']}\n"]
    if card.get("summary"):
        parts.append(f"**Summary**: {card['summary']}\n")
    if card.get("category"):
        parts.append(f"**Category**: {card['category']}\n")
    if card.get("tags"):
        parts.append(f"**Tags**: {card['tags']}\n")
    if card.get("content"):
        parts.append(f"\n---\n\n{card['content']}")
    if card.get("related_symbols"):
        parts.append(f"\n## Related Symbols\n{card['related_symbols']}\n")
    return "\n".join(parts)


def _format_card_evidence_resource(explanation: dict) -> str:
    card = explanation["card"]
    parts = [f"# Evidence: {card['title']}", ""]
    breakdown = explanation.get("score_breakdown", {})
    parts.append("## Score Breakdown")
    for key, value in breakdown.items():
        parts.append(f"- {key}: {value}")
    parts.append("")
    parts.append("## Match Details")
    for item in explanation.get("match_details", []):
        parts.append(f"- {item['field']}: {', '.join(item.get('matched_terms', []))}")
    return "\n".join(parts)


def list_resources(db=None) -> list[dict]:
    static_list = [
        {"uri": "repo://framework-map", "name": "Resource Map", "description": "Overview of EasyQuery sources and query strategies", "mimeType": "text/markdown"},
        {"uri": "repo://persistence-guide", "name": "Persistence Guide", "description": "Entity, repository, and service guidance for EasyQuery projects", "mimeType": "text/markdown"},
        {"uri": "repo://best-practices/testing", "name": "Testing Best Practices", "description": "H2, transaction, and repository testing guidance", "mimeType": "text/markdown"},
        {"uri": "repo://docs/project-architecture", "name": "Project Architecture", "description": "Suggested EasyQuery project structure and layering principles", "mimeType": "text/markdown"},
        {"uri": "repo://todo", "name": "Knowledge TODO", "description": "Project todo list for mining, evaluation, freshness, and telemetry work", "mimeType": "text/markdown"},
        {"uri": "repo://knowledge-health", "name": "Knowledge Health", "description": "Freshness summary for published knowledge cards", "mimeType": "text/markdown"},
        {"uri": "repo://knowledge-dashboard", "name": "Knowledge Dashboard", "description": "Operational dashboard for telemetry and stale cards", "mimeType": "text/markdown"},
        {"uri": "repo://architecture-drift", "name": "Architecture Drift", "description": "Current architecture drift findings against published pattern knowledge", "mimeType": "text/markdown"},
        {"uri": "repo://publishing-collaboration", "name": "Publishing Collaboration", "description": "Collaboration policy for candidate review and publish rules", "mimeType": "text/markdown"},
    ]

    if db is not None:
        try:
            cards = db.get_all_knowledge_cards()
            for c in cards:
                static_list.append({
                    "uri": f"repo://knowledge/{c['slug']}",
                    "name": f"Knowledge: {c['title']}",
                    "description": c.get("summary", c["title"])[:200],
                    "mimeType": "text/markdown",
                })
                static_list.append({
                    "uri": f"repo://knowledge-evidence/{c['slug']}",
                    "name": f"Evidence: {c['title']}",
                    "description": f"Explainability and evidence breakdown for {c['title']}"[:200],
                    "mimeType": "text/markdown",
                })
        except Exception:
            pass

    return static_list
