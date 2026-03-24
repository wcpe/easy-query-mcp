"""Knowledge publishing collaboration policy and candidate review checks."""

from __future__ import annotations

from typing import Any


POLICY_MARKDOWN = """# Knowledge Publishing Collaboration Policy

## Roles

- Owner: 对候选知识负责的人，负责补充上下文、证据和后续维护
- Reviewer: 审核人，负责确认结论、证据质量和可发布性
- Publisher: 触发发布的人，必须经过 release gate

## Candidate -> Reviewed -> Published

1. Candidate 必须有 owner
2. Review 阶段必须记录 reviewed_by 和 review_notes
3. Publish 前必须补充 change_summary
4. fix-pattern / anti-pattern / architecture 类卡片需要更高质量 evidence
5. Gate 不通过时不得发布，除非明确补齐证据并重新审核

## Evidence Expectations

- `fix-pattern`: 需要修复热点、验证步骤，最好带 related tests 或 related commits
- `architecture`: 需要结构证据、关键文件或关键符号
- `anti-pattern`: 需要至少两个命中点，并明确 recommended cards 或 avoid 内容
- `troubleshooting`: 需要明确 probable causes / suggested checks

## Baseline Rules

- 修改 pattern / miner / ranking / query expansion 后，先跑 evaluation
- 只有在 diff 符合预期时才更新 baseline / history
- baseline 更新和知识发布应分开审阅
"""


def policy_requirements(category: str) -> dict[str, Any]:
    category = (category or "").lower()
    base = {
        "require_owner": True,
        "require_reviewer": True,
        "require_review_notes": True,
        "require_change_summary": True,
        "recommended_evidence_quality": {"medium", "high"},
        "min_evidence_count": 1,
    }
    if category == "fix-pattern":
        base.update({
            "min_evidence_count": 2,
            "recommended_evidence_quality": {"high"},
            "requires_sections": ["Patch Hotspots", "Validation Steps"],
        })
    elif category == "anti-pattern":
        base.update({
            "min_evidence_count": 2,
            "recommended_evidence_quality": {"high"},
            "requires_sections": ["Recommended Cards", "Avoid"],
        })
    elif category == "architecture":
        base.update({
            "min_evidence_count": 2,
            "recommended_evidence_quality": {"medium", "high"},
            "requires_any": ["related_symbols", "content"],
        })
    elif category == "troubleshooting":
        base.update({
            "recommended_evidence_quality": {"medium", "high"},
            "requires_any": ["summary", "content"],
        })
    return base


def evaluate_candidate_collaboration(candidate: dict) -> dict[str, Any]:
    """Validate collaboration metadata and evidence expectations."""
    reqs = policy_requirements(candidate.get("category", ""))
    failures: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    owner = str(candidate.get("owner", "") or "").strip()
    reviewer = str(candidate.get("reviewed_by", "") or "").strip()
    review_notes = str(candidate.get("review_notes", "") or "").strip()
    change_summary = str(candidate.get("change_summary", "") or "").strip()
    evidence_quality = str(candidate.get("evidence_quality", "") or "").strip().lower()
    content = str(candidate.get("content", "") or "")
    evidence_count = int(candidate.get("evidence_count", 0) or 0)

    if reqs["require_owner"] and not owner:
        failures.append({"reason": "owner is missing"})
    if reqs["require_reviewer"] and not reviewer:
        failures.append({"reason": "reviewed_by is missing"})
    if reqs["require_review_notes"] and not review_notes:
        failures.append({"reason": "review_notes is missing"})
    if reqs["require_change_summary"] and not change_summary:
        failures.append({"reason": "change_summary is missing"})
    if evidence_count < int(reqs["min_evidence_count"]):
        failures.append({"reason": f"evidence_count < {reqs['min_evidence_count']}"})

    if evidence_quality:
        if evidence_quality not in reqs["recommended_evidence_quality"]:
            warnings.append({"reason": f"evidence_quality '{evidence_quality}' is below recommended level"})
    else:
        warnings.append({"reason": "evidence_quality is missing"})

    for heading in reqs.get("requires_sections", []):
        if f"## {heading}" not in content:
            failures.append({"reason": f"missing content section '{heading}'"})

    requires_any = reqs.get("requires_any", [])
    if requires_any and not any(str(candidate.get(field, "") or "").strip() for field in requires_any):
        failures.append({"reason": f"one of {requires_any} must be populated"})

    return {
        "passed": not failures,
        "requirements": reqs,
        "failures": failures,
        "warnings": warnings,
    }
