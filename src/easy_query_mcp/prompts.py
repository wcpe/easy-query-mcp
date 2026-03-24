"""MCP prompts for EasyQuery-focused development workflows."""

import re
from typing import Optional

from .query_expansion import expand_query_terms
from .repair_retrieval import collect_issue_cards
from .retrieval_intent import is_issue_oriented_query


PROMPT_REGISTRY = {
    "bugfix_prompt": {
        "name": "bugfix_prompt",
        "description": "Generate a bugfix analysis prompt for EasyQuery issues, with relevant knowledge card recommendations",
        "arguments": [{"name": "problem", "description": "Description of the bug or error", "required": True}],
    },
    "feature_impl_prompt": {
        "name": "feature_impl_prompt",
        "description": "Generate a feature implementation prompt for EasyQuery usage or integration work",
        "arguments": [{"name": "requirement", "description": "Feature requirement description", "required": True}],
    },
    "architecture_review_prompt": {
        "name": "architecture_review_prompt",
        "description": "Generate an architecture review prompt for EasyQuery-oriented projects",
        "arguments": [{"name": "context", "description": "What aspect of architecture to review", "required": True}],
    },
    "test_generation_prompt": {
        "name": "test_generation_prompt",
        "description": "Generate a test generation prompt for EasyQuery components",
        "arguments": [{"name": "target", "description": "Class or module to generate tests for", "required": True}],
    },
}


_CARD_RECOMMENDATIONS = {
    "bugfix": {
        "default": ["easyquery-transaction", "h2-concurrency-testing"],
        "transaction": ["easyquery-transaction", "h2-concurrency-testing"],
        "retry": ["easyquery-transaction", "h2-concurrency-testing"],
        "optimistic": ["easyquery-transaction", "h2-concurrency-testing"],
        "h2": ["h2-concurrency-testing"],
        "sql": ["h2-concurrency-testing"],
        "syntax": ["h2-concurrency-testing"],
        "page": ["h2-concurrency-testing"],
        "paging": ["h2-concurrency-testing"],
        "mapping": ["easyquery-transaction"],
        "entity": ["easyquery-transaction"],
    },
    "feature": {
        "default": ["easyquery-transaction"],
        "repository": ["easyquery-transaction"],
        "transaction": ["easyquery-transaction"],
        "page": ["h2-concurrency-testing"],
        "paging": ["h2-concurrency-testing"],
        "test": ["h2-concurrency-testing"],
    },
    "review": {
        "default": ["easyquery-transaction", "h2-concurrency-testing"],
        "transaction": ["easyquery-transaction"],
        "repository": ["easyquery-transaction"],
        "query": ["easyquery-transaction"],
        "test": ["h2-concurrency-testing"],
    },
    "test": {
        "default": ["h2-concurrency-testing", "easyquery-transaction"],
        "database": ["h2-concurrency-testing"],
        "transaction": ["h2-concurrency-testing", "easyquery-transaction"],
        "concurrent": ["h2-concurrency-testing"],
        "integration": ["h2-concurrency-testing"],
    },
}


def _recommend_cards(task_type: str, text: str) -> list[str]:
    mapping = _CARD_RECOMMENDATIONS.get(task_type, {})
    text_lower = text.lower()
    expanded_text = " ".join(expand_query_terms(text)) + " " + text_lower

    recommended = set()
    for keyword, slugs in mapping.items():
        if keyword == "default" or re.search(keyword, expanded_text, re.IGNORECASE):
            recommended.update(slugs)
    return sorted(recommended)


def _recommend_cards_from_db(db, task_type: str, text: str) -> list[str]:
    issue_oriented = task_type == "bugfix" and is_issue_oriented_query(text)
    if issue_oriented:
        payload = collect_issue_cards(db, text, limit=8)
        issue_slugs = [card.get("slug", "") for card in payload["results"] if card.get("slug")]
        if issue_slugs:
            return issue_slugs

    terms = list(dict.fromkeys(expand_query_terms(text) + re.findall(r"\w+", text.lower())))
    recommended = []
    seen = set()
    for term in terms[:6]:
        if issue_oriented:
            for card in db.search_knowledge_cards(term, category="fix-pattern", limit=3):
                slug = card.get("slug", "")
                if slug and slug not in seen:
                    seen.add(slug)
                    recommended.append(slug)
        for card in db.search_knowledge_cards(term, limit=3):
            slug = card.get("slug", "")
            if slug and slug not in seen:
                seen.add(slug)
                recommended.append(slug)
    return recommended or _recommend_cards(task_type, text)


def _format_card_section(slugs: list[str]) -> str:
    if not slugs:
        return ""
    lines = ["\n## Recommended Knowledge Cards (read before starting)"]
    for slug in slugs:
        lines.append(f"- `read_knowledge_card(slug='{slug}')`")
    lines.append("\nThese cards contain practical context about EasyQuery transactions, testing, and persistence behavior.")
    return "\n".join(lines)


def _bugfix(problem: str, db=None) -> str:
    cards = _recommend_cards_from_db(db, "bugfix", problem) if db is not None else _recommend_cards("bugfix", problem)
    card_section = _format_card_section(cards)
    return f"""You are debugging an EasyQuery-related issue.

## Problem
{problem}
{card_section}

## Debugging Steps
1. Identify whether the failure comes from mapping, generated SQL, transaction handling, pagination, or connection setup
2. Trace the failing call from service/repository entry to the actual EasyQuery operation
3. Inspect entity metadata, converter usage, and SQL/dialect assumptions
4. Compare test-only behavior with the real target database if the issue looks environment-specific
5. Prefer minimal fixes that preserve query readability and transaction semantics

## Output
- Root cause analysis
- Minimal code fix
- Verification steps
- Any schema or configuration impact
"""


def _feature_impl(requirement: str, db=None) -> str:
    cards = _recommend_cards_from_db(db, "feature", requirement) if db is not None else _recommend_cards("feature", requirement)
    card_section = _format_card_section(cards)
    return f"""You are implementing a new EasyQuery-related feature.

## Requirement
{requirement}
{card_section}

## Implementation Guidelines
- Keep entity mapping, repository logic, and service orchestration responsibilities separated
- Prefer typed EasyQuery APIs over handwritten SQL unless there is a clear need
- Design pagination, sorting, and filtering semantics explicitly
- Consider transaction boundaries, concurrency, and migration impact
- Add or update docs/examples for non-obvious usage

## Output
- Implementation plan
- Code changes
- Test plan
- Any required schema/config additions
"""


def _arch_review(context: str, db=None) -> str:
    cards = _recommend_cards_from_db(db, "review", context) if db is not None else _recommend_cards("review", context)
    card_section = _format_card_section(cards)
    return f"""You are reviewing the architecture of an EasyQuery-oriented project.

## Review Focus
{context}
{card_section}

## Architecture Checklist
1. Are entities, repositories, and services cleanly separated?
2. Are transactions scoped tightly and intentionally?
3. Are generated SQL, pagination, and join composition understandable?
4. Is raw SQL used sparingly and justified?
5. Are tests representative of the target database and concurrency profile?

## Output
- Findings summary
- Critical issues
- Improvement recommendations
- Suggested refactors with priority
"""


def _test_gen(target: str, db=None) -> str:
    cards = _recommend_cards_from_db(db, "test", target) if db is not None else _recommend_cards("test", target)
    card_section = _format_card_section(cards)
    return f"""You are generating tests for an EasyQuery-based component.

## Target
{target}
{card_section}

## Test Guidelines
- Use unit tests for orchestration and branch logic
- Use H2 or an appropriate target-like database for repository behavior
- Verify CRUD, pagination, joins, transactions, and rollback paths where relevant
- Add concurrency tests when the code touches retries, locks, or shared rows

## Output
- Complete test class
- Setup and teardown strategy
- Clear individual test methods
- Assertions that verify both behavior and data shape
"""


def list_prompts() -> list[dict]:
    return list(PROMPT_REGISTRY.values())


def get_prompt(name: str, arguments: dict, db=None) -> Optional[str]:
    if name == "bugfix_prompt":
        return _bugfix(arguments["problem"], db=db)
    if name == "feature_impl_prompt":
        return _feature_impl(arguments["requirement"], db=db)
    if name == "architecture_review_prompt":
        return _arch_review(arguments["context"], db=db)
    if name == "test_generation_prompt":
        return _test_gen(arguments["target"], db=db)
    return None
