"""Regression evaluation, release gating, and report generation."""

from __future__ import annotations

import argparse
import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import load_config
from .database import Database
from .error_miner import diagnose_from_log
from .knowledge_health import get_knowledge_health_summary, refresh_knowledge_health
from .publishing_policy import evaluate_candidate_collaboration, policy_requirements
from .tools_advanced import search_by_concept


SUITE_ORDER = [
    "diagnostics",
    "graph_paths",
    "retrieval",
    "bilingual_queries",
    "architecture",
]


def _default_fixture_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "eval"


def _default_baseline_path(fixture_root: Path) -> Path:
    return fixture_root / "baseline.json"


def _default_history_path(fixture_root: Path) -> Path:
    return fixture_root / "history.json"


def _load_fixture(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_json(path: Optional[Path]) -> dict | list | None:
    if path is None or not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _suite_summary(cases: list[dict]) -> dict:
    total = len(cases)
    passed = sum(1 for item in cases if item.get("passed"))
    failed = total - passed
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": round((passed / total) * 100.0, 2) if total else 100.0,
        "cases": cases,
    }


def _result_snapshot(summary: dict) -> dict:
    suites = {
        suite: summary[suite]
        for suite in SUITE_ORDER
        if suite in summary
    }
    return {
        "generated_at": summary["generated_at"],
        "overall": summary["overall"],
        "suites": suites,
    }


def _compare_cases(current_cases: list[dict], baseline_cases: list[dict]) -> list[dict]:
    current_by_name = {case["name"]: case for case in current_cases}
    baseline_by_name = {case["name"]: case for case in baseline_cases}
    diffs = []
    for name in sorted(set(current_by_name) | set(baseline_by_name)):
        before = baseline_by_name.get(name)
        after = current_by_name.get(name)
        if before != after:
            diffs.append({
                "name": name,
                "baseline_passed": before.get("passed") if before else None,
                "current_passed": after.get("passed") if after else None,
                "baseline": before,
                "current": after,
            })
    return diffs


def _is_failure_delta(item: dict) -> bool:
    baseline_passed = item.get("baseline_passed")
    current_passed = item.get("current_passed")
    return baseline_passed != current_passed or current_passed is False or baseline_passed is False


def _compare_with_baseline(summary: dict, baseline: dict | None) -> dict:
    if not baseline:
        return {"available": False, "suite_deltas": {}, "failure_diff": [], "result_diff": []}

    suite_deltas = {}
    failure_diff = []
    result_diff = []
    baseline_suites = baseline.get("suites", baseline)
    for suite in SUITE_ORDER:
        current_suite = summary.get(suite, {})
        baseline_suite = baseline_suites.get(suite, {})
        suite_deltas[suite] = {
            "baseline_passed": baseline_suite.get("passed", 0),
            "baseline_total": baseline_suite.get("total", 0),
            "current_passed": current_suite.get("passed", 0),
            "current_total": current_suite.get("total", 0),
            "pass_delta": current_suite.get("passed", 0) - baseline_suite.get("passed", 0),
            "pass_rate_delta": round(
                current_suite.get("pass_rate", 0.0) - baseline_suite.get("pass_rate", 0.0),
                2,
            ),
        }
        for item in _compare_cases(
            current_suite.get("cases", []),
            baseline_suite.get("cases", []),
        ):
            enriched = {
                "suite": suite,
                **item,
            }
            result_diff.append(enriched)
            if _is_failure_delta(enriched):
                failure_diff.append(enriched)

    return {
        "available": True,
        "suite_deltas": suite_deltas,
        "failure_diff": failure_diff,
        "result_diff": result_diff,
    }


def _append_history(path: Path, summary: dict) -> list[dict]:
    existing = _load_json(path)
    history = existing if isinstance(existing, list) else []
    history.append({
        "generated_at": summary["generated_at"],
        "overall": summary["overall"],
        "suites": {
            suite: {
                "passed": summary[suite]["passed"],
                "total": summary[suite]["total"],
                "pass_rate": summary[suite]["pass_rate"],
            }
            for suite in SUITE_ORDER
            if suite in summary
        },
    })
    history = history[-30:]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    return history


def _trend_snapshot(history: list[dict]) -> dict:
    if not history:
        return {"entries": 0, "latest": None, "previous": None}
    latest = history[-1]
    previous = history[-2] if len(history) > 1 else None
    deltas = {}
    if previous:
        for suite in SUITE_ORDER:
            latest_suite = latest.get("suites", {}).get(suite, {})
            previous_suite = previous.get("suites", {}).get(suite, {})
            deltas[suite] = round(
                latest_suite.get("pass_rate", 0.0) - previous_suite.get("pass_rate", 0.0),
                2,
            )
    return {
        "entries": len(history),
        "latest": latest,
        "previous": previous,
        "pass_rate_delta_from_previous": deltas,
    }


def _render_report(result: dict) -> str:
    lines = [
        "# Knowledge Evaluation Report",
        "",
        f"Generated at: {result['generated_at']}",
        f"Overall: {result['overall']['passed']}/{result['overall']['total']} passed",
        "",
        "| Suite | Passed | Total | Pass Rate | Delta vs Baseline |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    suite_deltas = result.get("comparison", {}).get("suite_deltas", {})
    for suite in SUITE_ORDER:
        suite_result = result.get(suite)
        if not suite_result:
            continue
        delta = suite_deltas.get(suite, {}).get("pass_rate_delta", 0.0)
        lines.append(
            f"| {suite} | {suite_result['passed']} | {suite_result['total']} | "
            f"{suite_result['pass_rate']}% | {delta:+.2f}% |"
        )

    failure_diff = result.get("comparison", {}).get("failure_diff", [])
    if failure_diff:
        lines.extend(["", "## Failure Diff", ""])
        for item in failure_diff[:20]:
            lines.append(
                f"- `{item['suite']}/{item['name']}`: "
                f"{item['baseline_passed']} -> {item['current_passed']}"
            )
    else:
        result_diff = result.get("comparison", {}).get("result_diff", [])
        if result_diff:
            lines.extend(["", "## Result Diff", ""])
            for item in result_diff[:20]:
                lines.append(
                    f"- `{item['suite']}/{item['name']}` changed while remaining "
                    f"{'passing' if item['current_passed'] else 'failing'}"
                )

    gate = result.get("gate")
    if gate:
        lines.extend([
            "",
            "## Release Gate",
            "",
            f"Gate status: {'PASS' if gate['passed'] else 'FAIL'}",
        ])
        for check in gate.get("checks", []):
            lines.append(f"- {check['name']}: {'PASS' if check['passed'] else 'FAIL'}")

    return "\n".join(lines) + "\n"


def _write_report(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".json":
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        return
    path.write_text(_render_report(result), encoding="utf-8")


def _evaluate_diagnostics(db: Database, cases: list[dict]) -> dict:
    results = []
    for case in cases:
        diagnosis = diagnose_from_log(db, case["log_text"], repo_hint=case.get("repo_hint"))
        matched = {item["pattern"] for item in diagnosis.get("matched_patterns", [])}
        required = set(case.get("expected_patterns", []))
        expected_symbol = case.get("expected_root_symbol")
        root = diagnosis.get("root_cause_location") or {}
        root_ok = not expected_symbol or root.get("symbol", "") == expected_symbol
        passed = required.issubset(matched) and root_ok
        results.append({
            "name": case["name"],
            "passed": passed,
            "expected_patterns": sorted(required),
            "matched_patterns": sorted(matched),
            "expected_root_symbol": expected_symbol,
            "root_cause_location": root,
            "proven_fix_patterns": [
                item.get("slug", "")
                for item in diagnosis.get("proven_fix_patterns", [])
            ],
        })
    return _suite_summary(results)


def _evaluate_graph_paths(db: Database, cases: list[dict]) -> dict:
    results = []
    for case in cases:
        path = db.trace_knowledge_path(case["source_slug"], case["target_slug"], case.get("max_depth", 3))
        found = bool(path)
        results.append({
            "name": case["name"],
            "passed": found,
            "path_length": len(path),
            "path": path,
        })
    return _suite_summary(results)


def _evaluate_query_suite(db: Database, cases: list[dict]) -> dict:
    results = []
    for case in cases:
        mode = case.get("mode", "knowledge")
        query = case["query"]
        if mode == "docs":
            search_results = db.search_docs(query, repo_id=case.get("repo_id"), limit=case.get("limit", 5))
            hits = [item.get("path", "") for item in search_results]
            expected_any = case.get("expected_any_paths", [])
            expected_top = case.get("expected_top_path")
        elif mode == "concept":
            concept = json.loads(search_by_concept(db, query, repo_id=case.get("repo_id"), limit=case.get("limit", 5)))
            search_results = concept.get("knowledge_cards", [])
            hits = [item.get("slug", "") for item in search_results]
            expected_any = case.get("expected_any_slugs", [])
            expected_top = case.get("expected_top_slug")
        else:
            search_results = db.search_knowledge_cards(query, category=case.get("category"), limit=case.get("limit", 5))
            hits = [item.get("slug", "") for item in search_results]
            expected_any = case.get("expected_any_slugs", [])
            expected_top = case.get("expected_top_slug")

        any_ok = all(target in hits for target in expected_any)
        top_ok = not expected_top or (hits and hits[0] == expected_top)
        passed = any_ok and top_ok
        results.append({
            "name": case["name"],
            "passed": passed,
            "mode": mode,
            "query": query,
            "top_hit": hits[0] if hits else "",
            "hits": hits[:5],
            "expected_top": expected_top,
            "expected_any": expected_any,
        })
    return _suite_summary(results)


def _evaluate_architecture(db: Database, cases: list[dict]) -> dict:
    results = []
    for case in cases:
        card = db.get_knowledge_card(case["slug"])
        relations = db.get_related_cards(case["slug"], limit=20) if card else []
        related_targets = {
            relation["target_slug"] if relation["source_slug"] == case["slug"] else relation["source_slug"]
            for relation in relations
        }
        expected_relations = set(case.get("expected_relations", []))
        title_contains = case.get("expected_title_contains", "")
        title_ok = not title_contains or (card and title_contains.lower() in card.get("title", "").lower())
        passed = bool(card) and expected_relations.issubset(related_targets) and title_ok
        results.append({
            "name": case["name"],
            "passed": passed,
            "slug": case["slug"],
            "card_found": bool(card),
            "related_targets": sorted(related_targets),
            "expected_relations": sorted(expected_relations),
        })
    return _suite_summary(results)


def run_evaluation(db: Database,
                   fixture_dir: Path | None = None,
                   baseline_path: Path | None = None,
                   history_path: Path | None = None,
                   write_baseline: bool = False,
                   write_history: bool = False,
                   report_path: Path | None = None) -> dict:
    fixture_root = fixture_dir or _default_fixture_dir()
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    diagnostics = _load_fixture(fixture_root / "diagnostics.json")
    graph_cases = _load_fixture(fixture_root / "graph_paths.json")
    retrieval_cases = _load_fixture(fixture_root / "retrieval.json")
    bilingual_cases = _load_fixture(fixture_root / "bilingual_queries.json")
    architecture_cases = _load_fixture(fixture_root / "architecture.json")

    summary = {
        "generated_at": generated_at,
        "fixture_dir": str(fixture_root),
        "diagnostics": _evaluate_diagnostics(db, diagnostics),
        "graph_paths": _evaluate_graph_paths(db, graph_cases),
        "retrieval": _evaluate_query_suite(db, retrieval_cases),
        "bilingual_queries": _evaluate_query_suite(db, bilingual_cases),
        "architecture": _evaluate_architecture(db, architecture_cases),
    }
    summary["overall"] = {
        "passed": sum(summary[suite]["passed"] for suite in SUITE_ORDER),
        "total": sum(summary[suite]["total"] for suite in SUITE_ORDER),
    }

    baseline_file = baseline_path or _default_baseline_path(fixture_root)
    baseline = _load_json(baseline_file)
    summary["comparison"] = _compare_with_baseline(summary, baseline if isinstance(baseline, dict) else None)

    if write_baseline:
        baseline_file.parent.mkdir(parents=True, exist_ok=True)
        with open(baseline_file, "w", encoding="utf-8") as f:
            json.dump(_result_snapshot(summary), f, ensure_ascii=False, indent=2)
        summary["baseline_written_to"] = str(baseline_file)
        baseline = _result_snapshot(summary)

    history = []
    history_file = history_path or _default_history_path(fixture_root)
    if write_history:
        history = _append_history(history_file, summary)
        summary["history_written_to"] = str(history_file)
    else:
        existing_history = _load_json(history_file)
        history = existing_history if isinstance(existing_history, list) else []
    summary["trend"] = _trend_snapshot(history)

    if report_path:
        _write_report(report_path, summary)
        summary["report_written_to"] = str(report_path)

    return summary


def _candidate_gate_checks(db: Database,
                           candidate_id: Optional[str],
                           require_reviewed: bool,
                           min_evidence_count: int) -> dict:
    candidates = []
    if candidate_id:
        candidate = db.get_candidate(candidate_id)
        if candidate:
            candidates.append(candidate)
    else:
        candidates = db.get_candidates(limit=500)

    failing = []
    warning = []
    for candidate in candidates:
        category_requirements = policy_requirements(candidate.get("category", ""))
        category_min_evidence = max(min_evidence_count, int(category_requirements.get("min_evidence_count", 1)))
        if require_reviewed and candidate.get("status") != "reviewed":
            failing.append({
                "card_id": candidate["card_id"],
                "reason": "candidate is not reviewed",
            })
        elif candidate.get("status") == "candidate":
            warning.append({
                "card_id": candidate["card_id"],
                "reason": "candidate still pending review",
            })

        if int(candidate.get("evidence_count", 0) or 0) < category_min_evidence:
            failing.append({
                "card_id": candidate["card_id"],
                "reason": f"evidence_count < {category_min_evidence}",
            })

        if float(candidate.get("stale_score", 0.0) or 0.0) >= 0.6 or candidate.get("lifecycle_status") == "stale":
            failing.append({
                "card_id": candidate["card_id"],
                "reason": "candidate is stale and needs refresh",
            })

        if not (candidate.get("summary", "") or "").strip():
            failing.append({
                "card_id": candidate["card_id"],
                "reason": "summary is empty",
            })

        collaboration = evaluate_candidate_collaboration(candidate)
        for item in collaboration.get("failures", []):
            failing.append({
                "card_id": candidate["card_id"],
                "reason": item["reason"],
            })
        for item in collaboration.get("warnings", []):
            warning.append({
                "card_id": candidate["card_id"],
                "reason": item["reason"],
            })

    return {
        "checked": len(candidates),
        "failed": failing,
        "warnings": warning,
    }


def run_release_gate(db: Database,
                     fixture_dir: Path | None = None,
                     baseline_path: Path | None = None,
                     history_path: Path | None = None,
                     report_path: Path | None = None,
                     candidate_id: Optional[str] = None,
                     require_reviewed: bool = False,
                     min_evidence_count: int = 2,
                     max_stale_cards: int = 25,
                     write_history: bool = False) -> dict:
    evaluation = run_evaluation(
        db,
        fixture_dir=fixture_dir,
        baseline_path=baseline_path,
        history_path=history_path,
        write_history=write_history,
    )
    audit = refresh_knowledge_health(db)
    health = get_knowledge_health_summary(db, limit=20)
    candidate_checks = _candidate_gate_checks(
        db,
        candidate_id=candidate_id,
        require_reviewed=require_reviewed,
        min_evidence_count=min_evidence_count,
    )

    checks = [
        {
            "name": "evaluation_regression",
            "passed": evaluation["overall"]["passed"] == evaluation["overall"]["total"],
            "details": evaluation["comparison"].get("failure_diff", [])[:20],
        },
        {
            "name": "freshness_audit",
            "passed": health["counts"].get("stale", 0) <= max_stale_cards,
            "details": {
                "stale_cards": health["counts"].get("stale", 0),
                "threshold": max_stale_cards,
            },
        },
        {
            "name": "candidate_review_checks",
            "passed": not candidate_checks["failed"],
            "details": candidate_checks,
        },
    ]

    gate = {
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
        "evaluation": evaluation,
        "freshness_audit": audit,
        "health_summary": health,
        "candidate_checks": candidate_checks,
    }
    if report_path:
        result = {**evaluation, "gate": gate}
        _write_report(report_path, result)
        gate["report_written_to"] = str(report_path)
    return gate


def _bootstrap_sample_db() -> tuple[Database, Path]:
    from tests.test_support import seed_sample_db

    workspace_tmp = Path(__file__).resolve().parent.parent.parent / ".tmp_tests"
    workspace_tmp.mkdir(parents=True, exist_ok=True)
    root = workspace_tmp / f"eval-bootstrap-{uuid.uuid4().hex}"
    root.mkdir(parents=True, exist_ok=True)
    db = Database(str(root / "evaluation.db"))
    seed_sample_db(db, root)
    return db, root


def main() -> None:
    parser = argparse.ArgumentParser(description="Run regression fixtures and release gates for easy-query-mcp knowledge")
    parser.add_argument("--config", "-c", default=None, help="Path to repos.yaml")
    parser.add_argument("--db", "-d", default=None, help="Database path override")
    parser.add_argument("--fixtures", default=None, help="Fixture directory override")
    parser.add_argument("--baseline", default=None, help="Baseline snapshot path")
    parser.add_argument("--history", default=None, help="Trend history path")
    parser.add_argument("--report", default=None, help="Write JSON or Markdown report to this path")
    parser.add_argument("--write-baseline", action="store_true", help="Overwrite the baseline snapshot with current results")
    parser.add_argument("--write-history", action="store_true", help="Append current scores to history")
    parser.add_argument("--gate", action="store_true", help="Run release gate checks and exit non-zero on failure")
    parser.add_argument("--candidate-id", default=None, help="Gate a specific candidate card")
    parser.add_argument("--require-reviewed", action="store_true", help="Require candidate status=reviewed in gate mode")
    parser.add_argument("--min-evidence-count", type=int, default=2, help="Minimum evidence count for candidate gating")
    parser.add_argument("--max-stale-cards", type=int, default=25, help="Fail gate if stale published cards exceed this count")
    parser.add_argument("--bootstrap-sample-db", action="store_true", help="Seed a temporary sample DB from tests fixtures for CI smoke checks")
    args = parser.parse_args()

    fixture_root = Path(args.fixtures) if args.fixtures else None
    baseline_path = Path(args.baseline) if args.baseline else None
    history_path = Path(args.history) if args.history else None
    report_path = Path(args.report) if args.report else None

    temp_dir: Optional[Path] = None
    if args.bootstrap_sample_db:
        db, temp_dir = _bootstrap_sample_db()
    else:
        config = load_config(args.config)
        db = Database(args.db or config.db_path)
        db.initialize()

    try:
        if args.gate:
            gate = run_release_gate(
                db,
                fixture_dir=fixture_root,
                baseline_path=baseline_path,
                history_path=history_path,
                report_path=report_path,
                candidate_id=args.candidate_id,
                require_reviewed=args.require_reviewed,
                min_evidence_count=args.min_evidence_count,
                max_stale_cards=args.max_stale_cards,
                write_history=args.write_history,
            )
            print(json.dumps({"status": "ok", "gate": gate}, ensure_ascii=False, indent=2))
            raise SystemExit(0 if gate["passed"] else 1)

        summary = run_evaluation(
            db,
            fixture_dir=fixture_root,
            baseline_path=baseline_path,
            history_path=history_path,
            write_baseline=args.write_baseline,
            write_history=args.write_history,
            report_path=report_path,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        db.close()
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
