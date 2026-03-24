"""MCP Tools - Phase 2: advanced search, troubleshooting, and prompt generation."""

import json
import re
from typing import Optional

from .database import Database
from .indexer import extract_canonical_topics
from .query_expansion import expand_query_terms
from .repair_retrieval import collect_issue_cards, dedup_cards
from .retrieval_intent import analyze_issue_query, is_issue_oriented_query, issue_sort_key
from .source_scope import merge_source_results, resolve_source_scope, row_in_scope, source_ids_for
from .semantic_ranking import rerank_semantic_candidates
from .pagination import clamp_limit, clamp_offset
from .query_guard import trim_list, truncation_meta, MAX_MATCH_LINES


QUERY_INTENT_TERMS = {
    "query", "查询", "where", "select", "join", "group by", "order by",
    "having", "projection", "predicate", "filter",
}

QUERY_PRIORITY_TERMS = [
    "queryable", "where", "select", "join", "groupby", "orderby", "predicate",
]

USAGE_INTENT_TERMS = {
    "how to", "usage", "example", "examples", "sample", "samples", "best practice",
    "strategy", "guide", "guideline", "should", "use", "using", "写法", "示例", "如何", "怎么", "最佳实践",
}

GUIDANCE_INTENT_TERMS = {
    "best practice", "strategy", "guide", "guideline", "pattern", "recommend",
    "recommended", "advice", "最佳实践", "建议", "推荐", "策略", "指南",
}

BEHAVIOR_INTENT_TERMS = {
    "rollback", "roll back", "commit", "savepoint", "save point", "nested",
    "retry", "deadlock", "isolation", "propagation", "exception",
    "回滚", "提交", "保存点", "嵌套", "重试", "死锁", "隔离级别", "传播", "异常",
}

MAPPING_INTENT_TERMS = {
    "entity", "mapping", "column", "table", "code first", "migration",
    "logic delete", "converter", "schema", "ddl", "sharding",
}

MAPPING_PRIORITY_TERMS = [
    "entity", "mapping", "table", "column", "codefirst", "migration", "logicdelete",
]

CANONICAL_TOPIC_LABELS = {
    "pagination": "分页",
    "transaction": "事务",
    "dynamic-condition": "动态条件",
    "logic-delete": "逻辑删除",
    "sharding": "分片",
    "code-first": "CodeFirst",
}

GUIDANCE_GENERIC_TITLE_TERMS = {
    "introduction",
    "intro",
    "overview",
    "reference",
    "readme",
    "documentation",
    "guide",
    "getting started",
    "getting-started",
    "getting_started",
    "简介",
    "介绍",
    "概述",
    "入门",
    "说明",
    "总览",
}

GUIDANCE_QUERY_STOPWORDS = {
    "how", "to", "use", "using", "usage", "guide", "guideline", "example", "examples",
    "sample", "samples", "strategy", "best", "practice", "practices", "recommended",
    "recommend", "advice", "should", "what", "when", "why", "which", "with", "from",
    "into", "about", "that", "this", "have", "has", "had", "will", "would", "could",
    "should", "can", "query", "queries", "easy", "querying",
}

GUIDANCE_TERM_ALIASES = {
    "rollback": ("rollback", "roll back", "rolled back", "回滚"),
    "commit": ("commit", "committed", "提交"),
    "exception": ("exception", "error", "throw", "throws", "thrown", "异常", "错误"),
    "nested": ("nested", "nest", "嵌套"),
    "savepoint": ("savepoint", "save point", "保存点"),
    "retry": ("retry", "重试"),
    "atomic": ("atomic", "原子"),
}


def _is_query_intent(text: str) -> bool:
    normalized = (text or "").lower()
    return any(term in normalized for term in QUERY_INTENT_TERMS)


def _is_mapping_intent(text: str) -> bool:
    normalized = (text or "").lower()
    return any(term in normalized for term in MAPPING_INTENT_TERMS)


def _is_usage_intent(text: str) -> bool:
    normalized = (text or "").lower()
    return any(term in normalized for term in USAGE_INTENT_TERMS)


def _is_guidance_intent(text: str) -> bool:
    normalized = (text or "").lower()
    return any(term in normalized for term in GUIDANCE_INTENT_TERMS)


def _is_behavior_intent(text: str) -> bool:
    normalized = (text or "").lower()
    return any(term in normalized for term in BEHAVIOR_INTENT_TERMS)


def _prepend_priority_keywords(keywords: list[str], priority_terms: list[str], limit: int = 12) -> list[str]:
    merged: list[str] = []
    seen = set()
    for term in priority_terms + keywords:
        normalized = (term or "").strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            merged.append(normalized)
        if len(merged) >= limit:
            break
    return merged


def _split_refs(raw: object) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        items = raw
    else:
        items = str(raw).split(",")
    refs: list[str] = []
    seen = set()
    for item in items:
        ref = str(item or "").strip().replace("\\", "/")
        if not ref:
            continue
        variants = [ref]
        if "/" in ref:
            variants.append(ref.split("/", 1)[1])
        for variant in variants:
            ref_lower = variant.lower()
            if ref_lower not in seen:
                seen.add(ref_lower)
                refs.append(variant)
    return refs


def _split_tags(raw: object) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        items = raw
    else:
        items = re.split(r"[,\|;/]+", str(raw))
    tags: list[str] = []
    seen = set()
    for item in items:
        tag = str(item or "").strip().lower()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
    return tags


def _collect_related_paths(cards: list[dict], limit: int = 3) -> dict[str, list[str]]:
    docs_refs: list[str] = []
    example_refs: list[str] = []
    source_cards: list[str] = []
    seen_docs = set()
    seen_examples = set()
    for card in cards[:limit]:
        slug = str(card.get("slug", "") or "")
        if slug:
            source_cards.append(slug)
        for ref in _split_refs(card.get("related_docs")):
            lowered = ref.lower()
            if lowered not in seen_docs:
                seen_docs.add(lowered)
                docs_refs.append(ref)
        for ref in _split_refs(card.get("related_examples")):
            lowered = ref.lower()
            if lowered not in seen_examples:
                seen_examples.add(lowered)
                example_refs.append(ref)
    return {
        "source_cards": source_cards,
        "docs_refs": docs_refs,
        "example_refs": example_refs,
    }


def _apply_related_boost(results: list[dict],
                         path_key: str,
                         references: list[str],
                         source_cards: list[str],
                         boost: float) -> list[dict]:
    if not results or not references:
        return results
    normalized_refs = [ref.replace("\\", "/").lower() for ref in references]
    for item in results:
        path_value = str(item.get(path_key, "") or "").replace("\\", "/").lower()
        matches = [ref for ref in normalized_refs if ref in path_value or path_value.endswith(ref)]
        if not matches:
            continue
        current_score = float(item.get("semantic_score", 0.0) or 0.0)
        item["related_boost"] = round(boost, 4)
        item["semantic_score"] = round(current_score + boost, 4)
        item["related_evidence"] = {
            "source_cards": source_cards,
            "matched_refs": matches,
        }
    return results


def _item_source_id(item: dict) -> str:
    return str(item.get("source_id") or item.get("repo_id") or "")


def _item_score(item: dict) -> float:
    breakdown = item.get("score_breakdown") or {}
    if isinstance(breakdown, dict) and breakdown.get("final_score") is not None:
        try:
            return float(breakdown.get("final_score", 0.0) or 0.0)
        except (TypeError, ValueError):
            pass
    for key in ("semantic_score", "confidence", "priority"):
        value = item.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _split_metadata_terms(raw: object) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        values = raw
    else:
        values = re.split(r"[,|;/]+", str(raw))
    terms: list[str] = []
    seen = set()
    for value in values:
        term = str(value or "").strip().lower()
        if not term or term in seen:
            continue
        seen.add(term)
        terms.append(term)
    return terms


def _canonical_topics_for_item(item: dict) -> list[str]:
    terms = _split_metadata_terms(item.get("feature_tags")) + _split_metadata_terms(item.get("tags"))
    matched: list[str] = []
    seen = set()
    for term in terms:
        normalized = term.removeprefix("topic:").strip()
        if normalized in CANONICAL_TOPIC_LABELS and normalized not in seen:
            seen.add(normalized)
            matched.append(normalized)
    return matched


def _domain_signal(item: dict) -> float:
    domain = str(item.get("content_domain", "") or "").lower()
    return {
        "api": 0.9,
        "test": 0.85,
        "example": 0.7,
        "knowledge": 0.45,
        "migration": 0.35,
        "faq": 0.3,
        "doc": 0.2,
        "source": 0.25,
    }.get(domain, 0.0)


def _stability_signal(item: dict) -> float:
    stability = str(item.get("stability", "") or "").lower()
    return {
        "stable": 0.35,
        "draft": -0.2,
        "experimental": -0.35,
        "internal": -0.4,
        "deprecated": -0.75,
        "legacy": -0.45,
    }.get(stability, 0.0)


def _visibility_signal(item: dict) -> float:
    visibility = str(item.get("visibility", "") or "").lower()
    return {
        "public": 0.2,
        "protected": -0.05,
        "internal": -0.35,
        "private": -0.6,
        "experimental": -0.2,
    }.get(visibility, 0.0)


def _bucket_rank(
    bucket: str,
    issue_oriented: bool,
    usage_intent: bool = False,
    guidance_intent: bool = False,
) -> int:
    if issue_oriented:
        ranks = {
            "fix_patterns": 5,
            "symbols": 4,
            "files": 4,
            "examples": 3,
            "knowledge_cards": 2,
            "docs": 1,
            "wiki": 0,
        }
    elif guidance_intent:
        ranks = {
            "docs": 5,
            "knowledge_cards": 4,
            "examples": 4,
            "files": 2,
            "fix_patterns": 2,
            "wiki": 2,
            "symbols": 1,
        }
    elif usage_intent:
        ranks = {
            "examples": 5,
            "docs": 4,
            "knowledge_cards": 3,
            "files": 3,
            "symbols": 2,
            "fix_patterns": 2,
            "wiki": 1,
        }
    else:
        ranks = {
            "symbols": 5,
            "files": 4,
            "examples": 4,
            "knowledge_cards": 3,
            "docs": 1,
            "wiki": 0,
            "fix_patterns": 2,
        }
    return ranks.get(bucket, 0)


def _source_affinity(item: dict, scope: dict) -> float:
    if not scope or scope.get("all_ids") is None:
        return 0.0
    source_id = _item_source_id(item)
    if not source_id:
        return 0.0
    requested_id = str(scope.get("requested_id") or "")
    anchor_id = str(scope.get("anchor_id") or "")
    if requested_id and source_id == requested_id:
        return 2.0
    if anchor_id and source_id == anchor_id:
        return 1.5
    if source_id in set(scope.get("all_ids") or []):
        return 1.0
    return 0.0


def _canonical_candidate_label(item: dict) -> str:
    return str(
        item.get("title")
        or item.get("name")
        or item.get("slug")
        or item.get("path")
        or item.get("rel_path")
        or item.get("qualified_name")
        or ""
    )


def _title_text(item: dict) -> str:
    return " ".join(
        part for part in [
            str(item.get("title", "") or ""),
            str(item.get("name", "") or ""),
            str(item.get("slug", "") or ""),
            str(item.get("path", "") or ""),
            str(item.get("rel_path", "") or ""),
        ]
        if part
    ).replace("\\", "/").lower()


def _title_only_text(item: dict) -> str:
    return " ".join(
        part for part in [
            str(item.get("title", "") or ""),
            str(item.get("name", "") or ""),
            str(item.get("slug", "") or ""),
        ]
        if part
    ).replace("\\", "/").lower()


def _path_only_text(item: dict) -> str:
    return " ".join(
        part for part in [
            str(item.get("path", "") or ""),
            str(item.get("rel_path", "") or ""),
        ]
        if part
    ).replace("\\", "/").lower()


def _guidance_title_signal(item: dict, query_topics: list[str]) -> float:
    title_text = _title_only_text(item)
    path_text = _path_only_text(item)
    combined_text = " ".join(part for part in [title_text, path_text] if part).strip()
    if not combined_text:
        return 0.0
    signal = 0.0
    topic_hits = 0
    for topic in query_topics:
        label = CANONICAL_TOPIC_LABELS.get(topic, topic)
        compact = topic.replace("-", "")
        if topic and topic in title_text:
            topic_hits += 2
        elif label and label.lower() in title_text:
            topic_hits += 2
        elif topic and (topic in path_text or compact in path_text):
            topic_hits += 1
        elif label and label.lower() in path_text:
            topic_hits += 1
    if topic_hits:
        signal += min(1.1, 0.28 * topic_hits)
    if any(term in title_text for term in GUIDANCE_GENERIC_TITLE_TERMS):
        signal -= 1.25
    if title_text in GUIDANCE_GENERIC_TITLE_TERMS:
        signal -= 0.45
    if topic_hits and any(term in title_text for term in ("introduction", "intro", "overview", "reference", "readme", "documentation", "guide", "简介", "介绍", "概述", "入门", "说明", "总览")):
        signal -= 0.3
    if topic_hits and any(term in path_text for term in ("logic-delete", "transaction", "dynamic", "pagination", "sharding", "code-first")):
        signal += 0.25
    return signal


def _guidance_query_terms(concept: str, query_topics: list[str]) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z\\-]{2,}", (concept or "").lower())
    topic_parts = {
        part
        for topic in query_topics
        for part in topic.replace("-", " ").split()
        if part
    }
    terms: list[str] = []
    seen = set()
    for word in words:
        normalized = word.strip("-")
        if (
            not normalized
            or normalized in GUIDANCE_QUERY_STOPWORDS
            or normalized in topic_parts
            or normalized in seen
        ):
            continue
        seen.add(normalized)
        terms.append(normalized)
    return terms


def _matches_guidance_alias(text: str, alias: str) -> bool:
    if not text or not alias:
        return False
    normalized_text = text.lower()
    normalized_alias = alias.lower()
    if re.fullmatch(r"[a-z0-9 -]+", normalized_alias):
        pattern = r"(?<![a-z0-9])" + re.escape(normalized_alias).replace(r"\ ", r"\s+") + r"(?![a-z0-9])"
        return re.search(pattern, normalized_text) is not None
    return normalized_alias in normalized_text


def _guidance_query_signal(item: dict, guidance_terms: list[str]) -> float:
    if not guidance_terms:
        return 0.0
    domain = str(item.get("content_domain", "") or "").lower()
    title_text = _title_only_text(item)
    headings_text = str(item.get("headings", "") or "").lower()
    summary_text = str(item.get("summary", "") or "").lower()
    snippet_text = str(item.get("snippet", "") or "").lower()
    tags_text = str(item.get("feature_tags", "") or "").lower()
    path_text = _path_only_text(item)
    if not any((title_text, headings_text, summary_text, snippet_text, tags_text, path_text)):
        return 0.0
    matched = 0
    exact_matches = 0
    for term in guidance_terms:
        aliases = GUIDANCE_TERM_ALIASES.get(term, (term,))
        title_hit = any(
            _matches_guidance_alias(title_text, alias) or _matches_guidance_alias(headings_text, alias)
            for alias in aliases
        )
        body_hit = any(
            _matches_guidance_alias(summary_text, alias)
            or _matches_guidance_alias(snippet_text, alias)
            or _matches_guidance_alias(tags_text, alias)
            or _matches_guidance_alias(path_text, alias)
            for alias in aliases
        )
        if title_hit or body_hit:
            matched += 1
            if title_hit:
                exact_matches += 1
    if matched == 0:
        penalty = -0.7 if len(guidance_terms) <= 2 else -0.5
        if domain == "api":
            penalty -= 0.25
        elif domain in {"doc", "knowledge", "faq"}:
            penalty -= 0.15
        return round(penalty, 4)
    signal = min(1.9, 0.42 * matched + 0.55 * exact_matches)
    if domain == "api" and exact_matches:
        signal += 0.15
    if matched < len(guidance_terms):
        signal -= min(0.65, 0.25 * (len(guidance_terms) - matched))
    return round(signal, 4)


def _example_behavior_signal(item: dict, guidance_terms: list[str]) -> float:
    if not guidance_terms:
        return 0.0
    title_text = _title_only_text(item)
    snippet_text = str(item.get("snippet", "") or "").lower()
    rel_path_text = str(item.get("rel_path", "") or "").replace("\\", "/").lower()
    matched = 0
    exact_matches = 0
    for term in guidance_terms:
        aliases = GUIDANCE_TERM_ALIASES.get(term, (term,))
        title_hit = any(_matches_guidance_alias(title_text, alias) for alias in aliases)
        path_hit = any(_matches_guidance_alias(rel_path_text, alias) for alias in aliases)
        snippet_hit = any(_matches_guidance_alias(snippet_text, alias) for alias in aliases)
        if title_hit or path_hit or snippet_hit:
            matched += 1
            if title_hit or path_hit:
                exact_matches += 1
    if matched == 0:
        return 0.0
    return round(min(1.25, 0.45 * matched + 0.35 * exact_matches), 4)


def _promote_behavior_file_examples(
    examples: list[dict],
    files: list[dict],
    guidance_terms: list[str],
    limit: int = 6,
) -> list[dict]:
    if not guidance_terms or not files:
        return list(examples)
    promoted: list[dict] = list(examples)
    seen = {
        (str(item.get("repo_id", "") or ""), str(item.get("rel_path", "") or ""))
        for item in promoted
    }
    promoted_hits = 0
    for item in files:
        domain = str(item.get("content_domain", "") or "").lower()
        if domain not in {"test", "example"}:
            continue
        signal = _example_behavior_signal(item, guidance_terms)
        if signal <= 0:
            continue
        key = (str(item.get("repo_id", "") or ""), str(item.get("rel_path", "") or ""))
        if key in seen:
            continue
        candidate = dict(item)
        candidate["promoted_from"] = "files"
        candidate["semantic_score"] = round(float(candidate.get("semantic_score", 0.0) or 0.0) + signal + 0.25, 4)
        promoted.append(candidate)
        seen.add(key)
        promoted_hits += 1
        if promoted_hits >= limit:
            break
    return promoted


def _merge_example_evidence(items: list[dict]) -> list[dict]:
    merged: dict[tuple[str, str], dict] = {}
    ordered_keys: list[tuple[str, str]] = []
    for item in items:
        key = (str(item.get("repo_id", "") or ""), str(item.get("rel_path", "") or ""))
        existing = merged.get(key)
        if existing is None:
            merged[key] = item
            ordered_keys.append(key)
            continue
        current_score = float(item.get("semantic_score", item.get("_score", 0.0)) or 0.0)
        existing_score = float(existing.get("semantic_score", existing.get("_score", 0.0)) or 0.0)
        if current_score > existing_score:
            merged[key] = item
    return [merged[key] for key in ordered_keys]


def _normalize_file_titles(items: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for item in items:
        enriched = dict(item)
        title = str(enriched.get("title", "") or "").strip()
        if not title:
            rel_path = str(enriched.get("rel_path", "") or "").replace("\\", "/").strip()
            path = str(enriched.get("path", "") or "").replace("\\", "/").strip()
            source_path = rel_path or path
            title = source_path.rsplit("/", 1)[-1] if source_path else ""
        rel_path = str(enriched.get("rel_path", "") or "").replace("\\", "/").strip()
        path = str(enriched.get("path", "") or "").replace("\\", "/").strip()
        if title and title in {rel_path, path}:
            title = title.rsplit("/", 1)[-1]
        enriched["title"] = title
        normalized.append(enriched)
    return normalized


def _item_topics(item: dict) -> list[str]:
    topics = extract_canonical_topics(
        str(item.get("title", "") or ""),
        str(item.get("summary", "") or ""),
        str(item.get("keywords", "") or ""),
        str(item.get("tags", "") or ""),
        str(item.get("feature_tags", "") or ""),
        str(item.get("path", "") or ""),
        str(item.get("rel_path", "") or ""),
        str(item.get("content_snippet", "") or ""),
        str(item.get("snippet", "") or ""),
    )
    known = set(CANONICAL_TOPIC_LABELS.keys())
    for tag in _split_tags(item.get("feature_tags")) + _split_tags(item.get("tags")):
        normalized = tag.removeprefix("topic:")
        if normalized in known and normalized not in topics:
            topics.append(normalized)
    return topics


def _topic_bucket_weight(bucket: str) -> float:
    return {
        "fix_patterns": 3.2,
        "knowledge_cards": 3.0,
        "examples": 2.4,
        "symbols": 2.2,
        "files": 2.0,
        "docs": 1.5,
        "wiki": 1.2,
    }.get(bucket, 1.0)


def _build_canonical_topics(concept: str, buckets: dict[str, list[dict]]) -> dict:
    query_topics = extract_canonical_topics(concept)
    scores: dict[str, float] = {topic: 3.5 for topic in query_topics}
    evidence: dict[str, list[dict]] = {topic: [] for topic in query_topics}

    for bucket, items in buckets.items():
        weight = _topic_bucket_weight(bucket)
        for rank, item in enumerate(items[:5], start=1):
            item_topics = _item_topics(item)
            if not item_topics:
                continue
            rank_bonus = max(0.6, 1.4 - (rank - 1) * 0.2)
            for topic in item_topics:
                topic_weight = weight * rank_bonus
                if query_topics:
                    if topic in query_topics:
                        topic_weight *= 1.8
                    else:
                        topic_weight *= 0.35
                scores[topic] = scores.get(topic, 0.0) + topic_weight
                evidence.setdefault(topic, []).append({
                    "bucket": bucket,
                    "title": _canonical_candidate_label(item),
                    "source_id": _item_source_id(item),
                })

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    if query_topics:
        threshold = max(1.6, max(scores.get(topic, 0.0) for topic in query_topics) * 0.3)
        ranked = [
            item for item in ranked
            if item[0] in query_topics or item[1] >= threshold
        ]
    result_topics = [
        {
            "slug": slug,
            "label": CANONICAL_TOPIC_LABELS.get(slug, slug),
            "score": round(score, 3),
            "evidence": evidence.get(slug, [])[:3],
        }
        for slug, score in ranked[:6]
    ]
    primary = ""
    if query_topics:
        for topic in query_topics:
            if any(item["slug"] == topic for item in result_topics):
                primary = topic
                break
        if not primary:
            primary = query_topics[0]
    elif result_topics:
        primary = result_topics[0]["slug"]
    return {
        "primary_topic": primary,
        "query_topics": query_topics,
        "result_topics": result_topics,
        "topic_status": "resolved" if primary else "none",
    }


def _domain_priority(item: dict, *, bucket: str, issue_oriented: bool, query_intent: bool, mapping_intent: bool) -> int:
    domain = str(item.get("content_domain", "") or "").lower()
    if issue_oriented:
        order = {
            "test": 7,
            "api": 6,
            "example": 5,
            "knowledge": 4,
            "migration": 3,
            "doc": 2,
            "faq": 1,
            "source": 0,
        }
    elif query_intent:
        order = {
            "api": 8,
            "example": 7,
            "test": 6,
            "knowledge": 5,
            "doc": 3,
            "faq": 2,
            "source": 1,
            "migration": 0,
        }
    elif mapping_intent:
        order = {
            "api": 8,
            "migration": 7,
            "doc": 6,
            "knowledge": 5,
            "example": 4,
            "test": 3,
            "faq": 2,
            "source": 1,
        }
    else:
        order = {
            "api": 6,
            "knowledge": 5,
            "example": 4,
            "test": 4,
            "doc": 3,
            "migration": 2,
            "faq": 1,
            "source": 0,
        }
    return order.get(domain, 0) + _bucket_rank(bucket, issue_oriented)


def _apply_domain_preferences(items: list[dict], *, bucket: str, issue_oriented: bool, query_intent: bool, mapping_intent: bool) -> list[dict]:
    ranked = list(items)
    ranked.sort(
        key=lambda item: (
            _domain_priority(item, bucket=bucket, issue_oriented=issue_oriented, query_intent=query_intent, mapping_intent=mapping_intent),
            float(item.get("semantic_score", item.get("_score", 0.0)) or 0.0),
            float((item.get("score_breakdown") or {}).get("final_score", 0.0) or 0.0),
            str(item.get("title") or item.get("name") or item.get("slug") or item.get("path") or item.get("rel_path") or ""),
        ),
        reverse=True,
    )
    return ranked


def _apply_usage_preferences(
    items: list[dict],
    *,
    bucket: str,
    usage_intent: bool,
    guidance_intent: bool,
    behavior_intent: bool,
    query_topics: list[str],
    guidance_terms: list[str],
) -> list[dict]:
    if not usage_intent or not items:
        return list(items)
    ranked: list[dict] = []
    for item in items:
        enriched = dict(item)
        score = float(enriched.get("semantic_score", enriched.get("_score", 0.0)) or 0.0)
        item_topics = _item_topics(enriched)
        topic_overlap = len([topic for topic in item_topics if topic in query_topics]) if query_topics else 0
        usage_bonus = 0.0
        if bucket == "examples":
            usage_bonus += 2.4
            if guidance_intent:
                usage_bonus -= 0.6
            if behavior_intent and guidance_terms:
                behavior_signal = _example_behavior_signal(enriched, guidance_terms)
                usage_bonus += behavior_signal
                if behavior_signal > 0:
                    domain = str(enriched.get("content_domain", "") or "").lower()
                    if domain == "test":
                        usage_bonus += 0.25
                    elif domain == "example":
                        usage_bonus += 0.15
        elif bucket == "docs":
            usage_bonus += 1.8
            if guidance_intent:
                usage_bonus += 0.8
                usage_bonus += _guidance_title_signal(enriched, query_topics)
                usage_bonus += _guidance_query_signal(enriched, guidance_terms)
        elif bucket == "knowledge_cards":
            usage_bonus += 1.4
            if guidance_intent:
                usage_bonus += 0.8
                usage_bonus += 0.55 * _guidance_title_signal(enriched, query_topics)
                usage_bonus += 0.75 * _guidance_query_signal(enriched, guidance_terms)
        elif bucket == "files":
            usage_bonus -= 1.8
            domain = str(enriched.get("content_domain", "") or "").lower()
            rel_path = str(enriched.get("rel_path", "") or "").replace("\\", "/").lower()
            title = str(enriched.get("title", "") or "").lower()
            path_signal = " ".join(part for part in [rel_path, title] if part)
            if domain == "test":
                usage_bonus += 0.6
            elif domain == "example":
                usage_bonus += 1.3
            elif domain == "api":
                usage_bonus -= 0.2
            if any(token in path_signal for token in ("example", "sample", "demo", "quickstart", "getting-started")):
                usage_bonus += 1.1
            if any(token in path_signal for token in ("test", "tests", "spec", "assert")):
                usage_bonus += 0.5
            if domain == "source":
                usage_bonus -= 0.4
            if not topic_overlap:
                usage_bonus -= 0.8
            if guidance_intent:
                usage_bonus -= 0.5
            if behavior_intent and guidance_terms:
                behavior_signal = _example_behavior_signal(enriched, guidance_terms)
                if domain in {"test", "example"}:
                    usage_bonus += 0.7 * behavior_signal
                    if behavior_signal > 0:
                        usage_bonus += 0.15
        elif bucket == "symbols":
            usage_bonus -= 2.1
            if str(enriched.get("kind", "") or "").lower() in {"class", "interface", "annotation", "enum", "object"}:
                usage_bonus += 0.4
            if guidance_intent:
                usage_bonus -= 0.4
        if topic_overlap:
            usage_bonus += 0.9 * topic_overlap
        enriched["semantic_score"] = round(score + usage_bonus, 4)
        enriched["usage_bonus"] = round(usage_bonus, 4)
        ranked.append(enriched)
    ranked.sort(
        key=lambda item: (
            float(item.get("semantic_score", item.get("_score", 0.0)) or 0.0),
            float((item.get("score_breakdown") or {}).get("final_score", 0.0) or 0.0),
            str(item.get("title") or item.get("name") or item.get("slug") or item.get("path") or item.get("rel_path") or ""),
        ),
        reverse=True,
    )
    return ranked


def _build_canonical_arbitration(
    buckets: dict[str, list[dict]],
    scope: dict,
    issue_oriented: bool,
    usage_intent: bool = False,
    guidance_intent: bool = False,
    behavior_intent: bool = False,
    query_topics: Optional[list[str]] = None,
    guidance_terms: Optional[list[str]] = None,
) -> tuple[list[dict], list[dict], str, float]:
    ranked: list[dict] = []
    guidance_topics = query_topics or []
    guidance_tokens = guidance_terms or []
    for bucket, items in buckets.items():
        trust = _bucket_rank(
            bucket,
            issue_oriented,
            usage_intent=usage_intent,
            guidance_intent=guidance_intent,
        )
        if trust <= 0:
            continue
        for item in items:
            score = _item_score(item)
            affinity = _source_affinity(item, scope)
            domain_signal = _domain_signal(item)
            stability_signal = _stability_signal(item)
            visibility_signal = _visibility_signal(item)
            guidance_signal = 0.0
            if guidance_intent and bucket in {"docs", "wiki", "knowledge_cards"}:
                guidance_signal = _guidance_title_signal(item, guidance_topics)
                guidance_signal += _guidance_query_signal(item, guidance_tokens)
            elif behavior_intent and bucket == "examples":
                guidance_signal = _example_behavior_signal(item, guidance_tokens)
            elif behavior_intent and bucket == "files":
                domain = str(item.get("content_domain", "") or "").lower()
                if domain in {"test", "example"}:
                    guidance_signal = _example_behavior_signal(item, guidance_tokens) * 0.65
            candidate = dict(item)
            candidate["bucket"] = bucket
            candidate["trust_rank"] = trust
            candidate["source_affinity"] = round(affinity, 3)
            candidate["arbitration_score"] = round(score + affinity + domain_signal + stability_signal + visibility_signal + guidance_signal, 4)
            if guidance_signal:
                candidate["guidance_signal"] = round(guidance_signal, 4)
            candidate["why"] = _arbitration_reason(
                bucket, trust, affinity, score, scope, candidate
            )
            ranked.append(candidate)

    ranked.sort(
        key=lambda item: (
            int(item.get("trust_rank", 0) or 0),
            float(item.get("source_affinity", 0.0) or 0.0),
            float(item.get("arbitration_score", 0.0) or 0.0),
            str(item.get("title") or item.get("name") or item.get("slug") or ""),
        ),
        reverse=True,
    )

    canonical = ranked[: min(5, len(ranked))]
    evidence_chain = []
    for item in canonical[:3]:
        evidence_chain.append({
            "bucket": item.get("bucket", ""),
            "title": _canonical_candidate_label(item),
            "source_id": _item_source_id(item),
            "score": round(float(item.get("arbitration_score", 0.0) or 0.0), 4),
            "trust_rank": int(item.get("trust_rank", 0) or 0),
            "why": item.get("why", ""),
            "path": item.get("path") or item.get("rel_path") or "",
            "slug": item.get("slug", ""),
            "content_domain": item.get("content_domain", ""),
            "visibility": item.get("visibility", ""),
            "stability": item.get("stability", ""),
            "canonical_topics": _canonical_topics_for_item(item),
        })

    if not canonical:
        return [], [], "No scoped evidence found after arbitration.", 0.0

    top = canonical[0]
    runner_up = canonical[1] if len(canonical) > 1 else None
    confidence = _arbitration_confidence(top, runner_up)
    conflict_reason = _arbitration_conflict_reason(top, runner_up)
    return canonical, evidence_chain, conflict_reason, confidence


def _arbitration_reason(bucket: str, trust: int, affinity: float, score: float, scope: dict, item: dict) -> str:
    scoped = bool(scope and scope.get("all_ids") is not None)
    scope_hint = "scoped" if scoped else "global"
    content_domain = str(item.get("content_domain", "") or "")
    stability = str(item.get("stability", "") or "")
    visibility = str(item.get("visibility", "") or "")
    metadata_hint = ", ".join(part for part in [content_domain, visibility, stability] if part)
    if trust >= 5:
        return f"High-trust {scope_hint} evidence outranks docs ({metadata_hint}).".strip()
    if trust >= 4:
        return f"Source-level evidence outranks docs in the {scope_hint} arbitration order ({metadata_hint}).".strip()
    if trust >= 3:
        return f"Knowledge-card evidence provides a {scope_hint} summary layer ({metadata_hint}).".strip()
    if affinity > 0:
        return f"{bucket} evidence ranked by combined score, source affinity, and {scope_hint} scope ({metadata_hint}).".strip()
    return f"{bucket} evidence ranked by combined score in {scope_hint} scope ({metadata_hint}).".strip()


def _arbitration_conflict_reason(top: dict, runner_up: Optional[dict]) -> str:
    if runner_up is None:
        return "Single dominant candidate after scope filtering."
    top_bucket = str(top.get("bucket", ""))
    runner_bucket = str(runner_up.get("bucket", ""))
    if top_bucket != runner_bucket:
        return f"{top_bucket} outranks {runner_bucket} under source-of-truth precedence."
    top_score = float(top.get("arbitration_score", 0.0) or 0.0)
    runner_score = float(runner_up.get("arbitration_score", 0.0) or 0.0)
    if abs(top_score - runner_score) <= max(0.25, top_score * 0.1):
        return f"Multiple {top_bucket} candidates are close; selected the strongest scoped match."
    return f"{top_bucket} candidate is clearly ahead after scope and score arbitration."


def _arbitration_confidence(top: dict, runner_up: Optional[dict]) -> float:
    score = float(top.get("arbitration_score", 0.0) or 0.0)
    trust = float(top.get("trust_rank", 0) or 0)
    confidence = 0.42 + min(0.22, score / 30.0) + min(0.25, trust * 0.05)
    if runner_up is not None:
        runner_score = float(runner_up.get("arbitration_score", 0.0) or 0.0)
        runner_trust = float(runner_up.get("trust_rank", 0) or 0)
        if trust == runner_trust:
            gap = max(0.0, score - runner_score)
            confidence += min(0.08, gap / max(abs(score), 1.0) * 0.12)
        elif trust > runner_trust:
            confidence += 0.04
    return round(max(0.2, min(0.98, confidence)), 3)

# ── Fallback error patterns for EasyQuery troubleshooting ──────────

ERROR_PATTERNS = [
    {
        "pattern": r"NoClassDefFoundError.*easyquery|ClassNotFoundException.*easyquery",
        "module": "EasyQuery Runtime",
        "cause": "EasyQuery runtime or extension classes are missing from the runtime classpath.",
        "fix": [
            "Check dependency packaging and shading rules",
            "Align easy-query module versions",
            "Inspect the built artifact to confirm the missing class exists",
        ],
    },
    {
        "pattern": r"SQLSyntaxErrorException|JdbcSQLSyntaxErrorException|SQL.*syntax.*error",
        "module": "SQL / Dialect",
        "cause": "Generated or handwritten SQL does not match the target database dialect.",
        "fix": [
            "Inspect generated SQL and mapping metadata",
            "Check reserved keywords and quoting strategy",
            "Verify the query against the real target database engine",
        ],
    },
    {
        "pattern": r"Cannot invoke.*because.*is null",
        "module": "General",
        "cause": "NullPointerException - a reference is null when accessed.",
        "fix": [
            "Check whether the value is optional and validate it earlier",
            "Verify entity fields and query parameters are populated before use",
            "Replace unsafe dereference with explicit validation",
        ],
    },
    {
        "pattern": r"java\.sql\.SQLException|DataAccessException",
        "module": "Database / SQL",
        "cause": "Database error during persistence or query execution.",
        "fix": [
            "Check SQL syntax and table schema",
            "Verify entity mapping annotations and generated SQL",
            "Check database connection configuration",
        ],
    },
    {
        "pattern": r"ClassCastException",
        "module": "General",
        "cause": "Invalid type cast at runtime.",
        "fix": [
            "Check configuration value types",
            "Verify converter and projection result types",
            "Use instanceof checks before casting",
        ],
    },
    {
        "pattern": r"StaleObjectStateException|optimistic.*lock|deadlock|Lock wait timeout",
        "module": "Transaction / Concurrency",
        "cause": "Concurrent writes conflict or transaction retries are insufficient.",
        "fix": [
            "Add bounded retry and backoff",
            "Reduce transaction scope",
            "Confirm optimistic locking fields are configured correctly",
        ],
    },
    {
        "pattern": r"StackOverflowError",
        "module": "General",
        "cause": "Infinite recursion detected.",
        "fix": [
            "Check for recursive converter, mapper, or graph traversal calls",
            "Review self-referential entity serialization",
        ],
    },
    {
        "pattern": r"H2.*(?:connection|closed|timeout)|Database may be already in use",
        "module": "H2 / Test Database",
        "cause": "Test database lifecycle or locking is misconfigured.",
        "fix": [
            "Keep DB_CLOSE_DELAY=-1 where needed",
            "Isolate per-test database names",
            "Review pool and teardown lifecycle",
        ],
    },
]


def find_usages(db: Database, symbol: str, repo_id: Optional[str] = None,
                source_id: Optional[str] = None,
                limit: int = 30) -> str:
    """Find usage locations of a symbol across indexed repositories.

    Args:
        symbol: Symbol name to search for
        repo_id: Optional repo filter
        limit: Max results

    Returns:
        JSON array of files and matching lines where the symbol appears.
    """
    resolved_source = source_id or repo_id
    scope = resolve_source_scope(db, resolved_source)
    raw_results = merge_source_results(
        scope["code_ids"],
        lambda sid, row_limit: db.find_usages(symbol, repo_id=sid, limit=row_limit),
        lambda row: (
            row.get("repo_id", ""),
            row.get("rel_path", ""),
            tuple((match.get("line", 0), match.get("text", "")) for match in row.get("matches", [])[:MAX_MATCH_LINES]),
        ),
        limit,
    )
    results = []
    truncated = False
    for row in raw_results:
        trimmed_matches, row_truncated = trim_list(list(row.get("matches", [])), MAX_MATCH_LINES)
        enriched = dict(row)
        enriched["matches"] = trimmed_matches
        if row_truncated:
            truncated = True
        results.append(enriched)
    if not results:
        return json.dumps({
            "status": "no_results",
            "message": f"No usages found for '{symbol}'.",
            "suggestions": [
                "The symbol might be used via import alias",
                "Try searching with the simple class name",
                "Check if all relevant repos are indexed",
            ],
        }, ensure_ascii=False, indent=2)

    return json.dumps({
        "status": "ok",
        "count": len(results),
        "truncation": truncation_meta(
            truncated=truncated,
            reason="usage match lines trimmed" if truncated else "",
            limits={"max_match_lines": MAX_MATCH_LINES},
        ),
        "results": results,
    }, ensure_ascii=False, indent=2)


def search_by_concept(db: Database, concept: str, repo_id: Optional[str] = None,
                      source_id: Optional[str] = None,
                      limit: int = 20,
                      offset: int = 0) -> str:
    """Search by concept or natural language description rather than exact API name.

    Searches across knowledge cards, docs, examples, symbols, and source files.
    Priority: knowledge_cards > docs > examples > symbols > source.

    Args:
        concept: Natural language description (e.g. "how to paginate a joined query")
        repo_id: Optional repo filter
        limit: Max results per category

    Returns:
        Combined results from all sources matching the concept.
    """
    safe_limit = clamp_limit(limit, default=20)
    safe_offset = clamp_offset(offset)
    stop_words = {"the", "a", "an", "is", "are", "was", "were", "be", "been",
                  "being", "have", "has", "had", "do", "does", "did", "will",
                  "would", "could", "should", "may", "might", "can", "shall",
                  "to", "of", "in", "for", "on", "with", "at", "by", "from",
                  "as", "into", "through", "during", "before", "after", "and",
                  "but", "or", "not", "no", "if", "then", "else", "when",
                  "how", "what", "which", "who", "where", "why", "all", "each",
                  "every", "both", "few", "more", "most", "other", "some", "such",
                  "than", "too", "very", "just", "about", "until"}

    words = re.findall(r'\w+', concept.lower())
    keywords = [w for w in words if w not in stop_words and len(w) > 2]
    keywords = list(dict.fromkeys(expand_query_terms(concept) + keywords))

    if not keywords:
        keywords = words[:5]

    mapping_intent = _is_mapping_intent(concept)
    query_intent = _is_query_intent(concept) and not mapping_intent
    usage_intent = _is_usage_intent(concept) or (query_intent and any(token in concept.lower() for token in ("how", "usage", "strategy", "guide", "example", "best practice", "如何", "怎么", "示例")))
    guidance_intent = _is_guidance_intent(concept)
    behavior_intent = _is_behavior_intent(concept)
    retrieval_usage_intent = usage_intent or behavior_intent
    if mapping_intent:
        keywords = _prepend_priority_keywords(keywords, MAPPING_PRIORITY_TERMS)
    elif query_intent:
        keywords = _prepend_priority_keywords(keywords, QUERY_PRIORITY_TERMS)
    query_topics = extract_canonical_topics(concept)
    guidance_terms = _guidance_query_terms(concept, query_topics) if (guidance_intent or behavior_intent) else []

    intent = analyze_issue_query(concept)
    issue_oriented = bool(intent["issue_oriented"])

    resolved_source = source_id or repo_id
    scope = resolve_source_scope(db, resolved_source)
    knowledge_source_ids = source_ids_for(scope, "knowledge")
    docs_source_ids = source_ids_for(scope, "docs")
    wiki_source_ids = source_ids_for(scope, "wiki")
    code_source_ids = source_ids_for(scope, "code")

    all_results: dict[str, list] = {
        "knowledge_cards": [],
        "fix_patterns": [],
        "docs": [],
        "wiki": [],
        "examples": [],
        "symbols": [],
        "files": [],
    }

    if issue_oriented:
        issue_payload = collect_issue_cards(db, concept, limit=max(safe_limit + safe_offset, 8), source_id=resolved_source)
        all_results["fix_patterns"].extend(issue_payload["fix_patterns"])
        all_results["knowledge_cards"].extend(issue_payload["results"])

    candidate_limit = max(safe_limit * 3, 20)
    for kw in keywords[:8]:
        all_results["knowledge_cards"].extend(
            merge_source_results(
                knowledge_source_ids,
                lambda sid, row_limit: db.search_knowledge_cards(kw, limit=row_limit, source_id=sid),
                lambda row: (row.get("source_id", ""), row.get("slug", "")),
                5,
            )
        )

        try:
            for candidate in db.search_candidates(kw, limit=candidate_limit):
                if candidate.get("status") not in ("reviewed", "published"):
                    continue
                if not row_in_scope(candidate, scope):
                    continue
                all_results["knowledge_cards"].append({
                    "card_id": candidate["card_id"],
                    "title": candidate["title"],
                    "slug": candidate["slug"],
                    "category": candidate.get("category", ""),
                    "summary": candidate.get("summary", ""),
                    "tags": candidate.get("tags", ""),
                    "keywords": candidate.get("keywords", ""),
                    "feature_tags": candidate.get("feature_tags", ""),
                    "content_domain": candidate.get("content_domain", ""),
                    "visibility": candidate.get("visibility", ""),
                    "stability": candidate.get("stability", ""),
                    "build_commit": candidate.get("build_commit", ""),
                    "build_time": candidate.get("build_time", ""),
                    "priority": int(candidate.get("confidence", 0) * 100),
                    "source_id": candidate.get("source_id", ""),
                    "source_type": candidate.get("source_type", ""),
                })
        except Exception:
            pass

        all_results["docs"].extend(
            merge_source_results(
                docs_source_ids,
                lambda sid, row_limit: db.search_docs(kw, repo_id=sid, limit=row_limit, source_type="docs"),
                lambda row: (row.get("repo_id", ""), row.get("path", "")),
                5,
            )
        )
        all_results["wiki"].extend(
            merge_source_results(
                wiki_source_ids,
                lambda sid, row_limit: db.search_docs(kw, repo_id=sid, limit=row_limit, source_type="wiki"),
                lambda row: (row.get("repo_id", ""), row.get("path", "")),
                5,
            )
        )
        all_results["examples"].extend(
            merge_source_results(
                code_source_ids,
                lambda sid, row_limit: db.get_examples(kw, repo_id=sid, limit=row_limit),
                lambda row: (row.get("repo_id", ""), row.get("rel_path", ""), row.get("title", "")),
                5,
            )
        )
        all_results["symbols"].extend(
            merge_source_results(
                code_source_ids,
                lambda sid, row_limit: db.search_symbols(kw, repo_id=sid, limit=row_limit),
                lambda row: (row.get("repo_id", ""), row.get("qualified_name", ""), row.get("line_number", 0)),
                5,
            )
        )
        all_results["files"].extend(
            merge_source_results(
                code_source_ids,
                lambda sid, row_limit: db.search_files_fts(kw, repo_id=sid, limit=row_limit),
                lambda row: (row.get("repo_id", ""), row.get("rel_path", "")),
                5,
            )
        )

    if scope.get("all_ids") is not None:
        for bucket, rows in list(all_results.items()):
            all_results[bucket] = [row for row in rows if row_in_scope(row, scope)]

    unique_cards = dedup_cards(all_results["knowledge_cards"])
    unique_fix_patterns = dedup_cards(all_results["fix_patterns"])
    unique_docs = _dedup(all_results["docs"], lambda d: (d.get("repo_id", ""), d.get("path", "")))
    unique_wiki = _dedup(all_results["wiki"], lambda d: (d.get("repo_id", ""), d.get("path", "")))
    unique_exs = _dedup(all_results["examples"], lambda e: (e.get("repo_id", ""), e.get("rel_path", "")))
    unique_syms = _dedup(all_results["symbols"], lambda s: (s.get("repo_id", ""), s.get("qualified_name", ""), s.get("line_number", 0)))
    unique_files = _dedup(all_results["files"], lambda f: (f.get("repo_id", ""), f.get("rel_path", "")))
    unique_files = _normalize_file_titles(unique_files)

    card_weights = {"title": 3.0, "summary": 2.4, "tags": 2.0, "keywords": 2.0, "category": 1.2}
    doc_weights = {"title": 2.6, "summary": 2.4, "headings": 2.0, "tags": 1.5, "snippet": 1.8, "path": 1.0}
    example_weights = {"title": 2.2, "snippet": 2.6, "path": 1.2}
    if query_intent:
        card_weights = {"title": 3.4, "summary": 2.4, "tags": 2.2, "keywords": 2.6, "category": 1.4}
        doc_weights = {"title": 3.2, "summary": 2.3, "headings": 2.6, "tags": 1.8, "snippet": 1.8, "path": 2.8}
        example_weights = {"title": 2.4, "snippet": 2.8, "path": 2.6}

    unique_cards = rerank_semantic_candidates(
        unique_cards,
        concept,
        {"title": "title", "summary": "summary", "tags": "tags", "keywords": "keywords", "category": "category"},
        card_weights,
        max((safe_limit + safe_offset) * 2, 10),
    )
    unique_docs = rerank_semantic_candidates(
        unique_docs,
        concept,
        {"title": "title", "summary": "summary", "headings": "headings", "tags": "tags", "snippet": "snippet", "path": "path"},
        doc_weights,
        max((safe_limit + safe_offset) * 2, 10),
    )
    unique_wiki = rerank_semantic_candidates(
        unique_wiki,
        concept,
        {"title": "title", "summary": "summary", "headings": "headings", "tags": "tags", "snippet": "snippet", "path": "path"},
        doc_weights,
        max((safe_limit + safe_offset) * 2, 10),
    )
    unique_exs = rerank_semantic_candidates(
        unique_exs,
        concept,
        {"title": "title", "snippet": "snippet", "rel_path": "path"},
        example_weights,
        max((safe_limit + safe_offset) * 2, 10),
    )

    unique_cards = _apply_domain_preferences(
        unique_cards,
        bucket="knowledge_cards",
        issue_oriented=issue_oriented,
        query_intent=query_intent,
        mapping_intent=mapping_intent,
    )
    unique_docs = _apply_domain_preferences(
        unique_docs,
        bucket="docs",
        issue_oriented=issue_oriented,
        query_intent=query_intent,
        mapping_intent=mapping_intent,
    )
    unique_wiki = _apply_domain_preferences(
        unique_wiki,
        bucket="wiki",
        issue_oriented=issue_oriented,
        query_intent=query_intent,
        mapping_intent=mapping_intent,
    )
    unique_exs = _apply_domain_preferences(
        unique_exs,
        bucket="examples",
        issue_oriented=issue_oriented,
        query_intent=query_intent,
        mapping_intent=mapping_intent,
    )
    unique_syms = _apply_domain_preferences(
        unique_syms,
        bucket="symbols",
        issue_oriented=issue_oriented,
        query_intent=query_intent,
        mapping_intent=mapping_intent,
    )
    unique_files = _apply_domain_preferences(
        unique_files,
        bucket="files",
        issue_oriented=issue_oriented,
        query_intent=query_intent,
        mapping_intent=mapping_intent,
    )

    unique_cards = _apply_usage_preferences(unique_cards, bucket="knowledge_cards", usage_intent=retrieval_usage_intent, guidance_intent=(guidance_intent or behavior_intent), behavior_intent=behavior_intent, query_topics=query_topics, guidance_terms=guidance_terms)
    unique_docs = _apply_usage_preferences(unique_docs, bucket="docs", usage_intent=retrieval_usage_intent, guidance_intent=(guidance_intent or behavior_intent), behavior_intent=behavior_intent, query_topics=query_topics, guidance_terms=guidance_terms)
    unique_wiki = _apply_usage_preferences(unique_wiki, bucket="docs", usage_intent=retrieval_usage_intent, guidance_intent=(guidance_intent or behavior_intent), behavior_intent=behavior_intent, query_topics=query_topics, guidance_terms=guidance_terms)
    unique_exs = _apply_usage_preferences(unique_exs, bucket="examples", usage_intent=retrieval_usage_intent, guidance_intent=(guidance_intent or behavior_intent), behavior_intent=behavior_intent, query_topics=query_topics, guidance_terms=guidance_terms)
    unique_syms = _apply_usage_preferences(unique_syms, bucket="symbols", usage_intent=retrieval_usage_intent, guidance_intent=(guidance_intent or behavior_intent), behavior_intent=behavior_intent, query_topics=query_topics, guidance_terms=guidance_terms)
    unique_files = _apply_usage_preferences(unique_files, bucket="files", usage_intent=retrieval_usage_intent, guidance_intent=(guidance_intent or behavior_intent), behavior_intent=behavior_intent, query_topics=query_topics, guidance_terms=guidance_terms)
    unique_files = _normalize_file_titles(unique_files)
    if behavior_intent and guidance_terms:
        unique_exs = _merge_example_evidence(
            _promote_behavior_file_examples(unique_exs, unique_files, guidance_terms)
        )
        unique_exs.sort(
            key=lambda item: (
                float(item.get("semantic_score", item.get("_score", 0.0)) or 0.0),
                float((item.get("score_breakdown") or {}).get("final_score", 0.0) or 0.0),
                str(item.get("title") or item.get("rel_path") or ""),
            ),
            reverse=True,
        )

    related_routing = _collect_related_paths(unique_cards, limit=3)
    doc_boost = 0.45 if query_intent else 0.35
    example_boost = 0.55 if query_intent else 0.45
    unique_docs = _apply_related_boost(unique_docs, "path", related_routing["docs_refs"], related_routing["source_cards"], doc_boost)
    unique_wiki = _apply_related_boost(unique_wiki, "path", related_routing["docs_refs"], related_routing["source_cards"], doc_boost)
    unique_exs = _apply_related_boost(unique_exs, "rel_path", related_routing["example_refs"], related_routing["source_cards"], example_boost)

    if issue_oriented:
        unique_cards.sort(key=lambda item: issue_sort_key(item, intent))
        unique_fix_patterns.sort(key=lambda item: issue_sort_key(item, intent))

    total = len(unique_cards) + len(unique_docs) + len(unique_wiki) + len(unique_exs) + len(unique_syms) + len(unique_files)
    if total == 0:
        return json.dumps({
            "status": "no_results",
            "message": f"No results found for concept: '{concept}'",
            "keywords_used": keywords,
            "source_scope": {
                "requested_id": scope.get("requested_id"),
                "anchor_id": scope.get("anchor_id"),
                "resolved": scope.get("resolved", True),
                "scope_status": scope.get("scope_status", "resolved"),
                "fallback_reason": scope.get("fallback_reason", ""),
                "all_ids": scope.get("all_ids"),
                "code_ids": scope.get("code_ids"),
                "docs_ids": scope.get("docs_ids"),
                "wiki_ids": scope.get("wiki_ids"),
                "knowledge_ids": scope.get("knowledge_ids"),
            },
            "canonical_answer_candidates": [],
            "canonical_topics": _build_canonical_topics(concept, {}),
            "evidence_chain": [],
            "conflict_reason": "No results after scope filtering.",
            "confidence": 0.0,
            "suggestions": [
                "Try rephrasing with more specific technical terms",
                "Use search_knowledge_cards for structured knowledge",
                "Use search_symbol for exact API names",
                "Check if relevant repos are indexed",
            ],
        }, ensure_ascii=False, indent=2)

    canonical_answer_candidates, evidence_chain, conflict_reason, arbitration_confidence = _build_canonical_arbitration(
        {
            "fix_patterns": unique_fix_patterns,
            "knowledge_cards": unique_cards,
            "examples": unique_exs,
            "symbols": unique_syms,
            "files": unique_files,
            "docs": unique_docs,
            "wiki": unique_wiki,
        },
        scope,
        issue_oriented,
        usage_intent=retrieval_usage_intent,
        guidance_intent=(guidance_intent or behavior_intent),
        behavior_intent=behavior_intent,
        query_topics=query_topics,
        guidance_terms=guidance_terms,
    )
    canonical_topics = _build_canonical_topics(
        concept,
        {
            "fix_patterns": unique_fix_patterns,
            "knowledge_cards": unique_cards,
            "examples": unique_exs,
            "symbols": unique_syms,
            "files": unique_files,
            "docs": unique_docs,
            "wiki": unique_wiki,
        }
    )

    return json.dumps({
        "status": "ok",
        "concept": concept,
        "retrieval_strategy": intent["query_mode"] if issue_oriented else "hybrid_semantic",
        "intent": intent,
        "domain_hint": "entity_mapping" if mapping_intent else ("query_builder" if query_intent else "generic"),
        "usage_intent": usage_intent,
        "guidance_intent": guidance_intent,
        "behavior_intent": behavior_intent,
        "keywords_used": keywords,
        "source_scope": {
            "requested_id": scope.get("requested_id"),
            "anchor_id": scope.get("anchor_id"),
            "resolved": scope.get("resolved", True),
            "scope_status": scope.get("scope_status", "resolved"),
            "fallback_reason": scope.get("fallback_reason", ""),
            "all_ids": scope.get("all_ids"),
            "code_ids": scope.get("code_ids"),
            "docs_ids": scope.get("docs_ids"),
            "wiki_ids": scope.get("wiki_ids"),
            "knowledge_ids": scope.get("knowledge_ids"),
        },
        "semantic_rerank": {
            "enabled": True,
            "strategy": "token_overlap_weighted_fields",
            "categories": ["knowledge_cards", "docs", "wiki", "examples"],
        },
        "related_routing": {
            "enabled": bool(related_routing["docs_refs"] or related_routing["example_refs"]),
            "source_cards": related_routing["source_cards"],
            "docs_refs": related_routing["docs_refs"],
            "example_refs": related_routing["example_refs"],
        },
        "canonical_answer_candidates": canonical_answer_candidates,
        "canonical_topics": canonical_topics,
        "evidence_chain": evidence_chain,
        "conflict_reason": conflict_reason,
        "confidence": arbitration_confidence,
        "pagination": {
            "limit": safe_limit,
            "offset": safe_offset,
            "has_more": any(len(category) > safe_offset + safe_limit for category in [unique_fix_patterns, unique_cards, unique_docs, unique_wiki, unique_exs, unique_syms, unique_files]),
        },
        "fix_patterns": unique_fix_patterns[safe_offset:safe_offset + safe_limit],
        "knowledge_cards": unique_cards[safe_offset:safe_offset + safe_limit],
        "docs": unique_docs[safe_offset:safe_offset + safe_limit],
        "wiki": unique_wiki[safe_offset:safe_offset + safe_limit],
        "examples": unique_exs[safe_offset:safe_offset + safe_limit],
        "symbols": unique_syms[safe_offset:safe_offset + safe_limit],
        "files": unique_files[safe_offset:safe_offset + safe_limit],
    }, ensure_ascii=False, indent=2)


def _dedup(items: list[dict], key_fn) -> list[dict]:
    """Deduplicate a list of dicts by a key function."""
    seen = set()
    result = []
    for item in items:
        k = key_fn(item)
        if k not in seen:
            seen.add(k)
            result.append(item)
    return result


def explain_error(db: Database, error_text: str) -> str:
    """Analyze an error log or exception and provide diagnosis with fix suggestions.

    Uses error pattern matching + knowledge card lookup + source search.

    Args:
        error_text: Error message, stack trace, or exception text

    Returns:
        JSON with matched patterns, causes, related cards, and investigation steps.
    """
    from .error_miner import diagnose_from_log

    diagnosis = diagnose_from_log(db, error_text)

    if diagnosis.get("status") == "no_match":
        # Fallback: try old-style pattern matching + symbol search
        class_refs = re.findall(r'([A-Z]\w+(?:\.[A-Z]\w+)*)', error_text)
        unique_refs = list(dict.fromkeys(class_refs))[:10]

        related_symbols = []
        for ref in unique_refs[:5]:
            simple_name = ref.split(".")[-1]
            syms = db.search_symbols(simple_name, limit=3)
            related_symbols.extend(syms)

        # Also try knowledge card search
        issue_payload = collect_issue_cards(db, error_text, limit=6)
        words = list(dict.fromkeys(expand_query_terms(error_text) + re.findall(r'\w{4,}', error_text.lower())))
        related_cards = dedup_cards(issue_payload["results"])
        related_fix_patterns = dedup_cards(issue_payload["fix_patterns"])
        for w in words[:5]:
            cards = db.search_knowledge_cards(w, limit=2)
            related_cards.extend(cards)
        related_cards = dedup_cards(related_cards)

        return json.dumps({
            "status": "partial_match",
            "message": "No exact error pattern matched, but found related context.",
            "intent": issue_payload["intent"],
            "extracted_references": unique_refs,
            "related_symbols": related_symbols[:10],
            "related_knowledge_cards": [
                {"slug": c.get("slug", ""), "title": c.get("title", "")}
                for c in related_cards[:5]
            ],
            "proven_fix_patterns": [
                {"slug": c.get("slug", ""), "title": c.get("title", "")}
                for c in related_fix_patterns[:5]
            ],
            "suggestions": [
                "Search for specific class names using search_symbol",
                "Use search_by_concept with keywords from the error",
                "Check EasyQuery docs for the relevant API or mapping behavior",
            ],
        }, ensure_ascii=False, indent=2)

    return json.dumps(diagnosis, ensure_ascii=False, indent=2)


def generate_agent_prompt(db: Database, task_type: str,
                          context: str = "",
                          target: str = "") -> str:
    """Generate a structured prompt for a code agent based on task type.

    Args:
        task_type: One of 'bugfix', 'feature', 'architecture_review', 'test_generation'
        context: Additional context (error message, requirement description, etc.)
        target: Target class/module/file for the task

    Returns:
        A detailed prompt string suitable for a code agent.
    """
    # Gather relevant symbols if target is specified
    related_info = ""
    if target:
        syms = db.search_symbols(target, limit=10)
        if syms:
            sym_lines = []
            for s in syms:
                sym_lines.append(f"  - {s['kind']} {s['signature']} "
                                 f"({s['repo_id']}:{s['rel_path']}:{s['line_number']})")
            related_info = "Related symbols found:\n" + "\n".join(sym_lines)

    fix_info = ""
    if task_type == "bugfix" and is_issue_oriented_query(context or target):
        issue_payload = collect_issue_cards(db, context or target, limit=6)
        fix_lines = []
        for card in issue_payload["fix_patterns"][:5]:
            slug = card.get("slug", "")
            if slug:
                reason = card.get("retrieval_reason", "")
                fix_lines.append(f"  - {card['title']} ({slug}) {reason}".rstrip())
        if fix_lines:
            fix_info = "Proven fix patterns to review first:\n" + "\n".join(fix_lines)

    templates = {
        "bugfix": _bugfix_prompt(context, target, related_info, fix_info),
        "feature": _feature_prompt(context, target, related_info),
        "architecture_review": _architecture_review_prompt(context, target, related_info),
        "test_generation": _test_generation_prompt(context, target, related_info),
    }

    prompt = templates.get(task_type)
    if prompt is None:
        return json.dumps({
            "status": "error",
            "message": f"Unknown task_type: '{task_type}'",
            "valid_types": list(templates.keys()),
        }, ensure_ascii=False, indent=2)

    return json.dumps({
        "status": "ok",
        "task_type": task_type,
        "prompt": prompt,
    }, ensure_ascii=False, indent=2)


def _bugfix_prompt(context: str, target: str, related: str, fix_info: str = "") -> str:
    return f"""You are debugging an EasyQuery-based persistence or query issue.

## Environment
- ORM: EasyQuery
- Language: Java and/or Kotlin
- Common layers: entity, repository, service, docs, tests

## Bug Description
{context or "[Describe the bug here]"}

## Target
{target or "[Specify the class/module/file]"}

{related}
{fix_info}

## Instructions
1. First, read the source code of the target file to understand current behavior
2. Identify the root cause by tracing the execution flow
3. Check if the issue is related to:
   - entity mapping and generated SQL
   - transaction boundaries and retry logic
   - paging, sorting, or query-builder composition
   - schema bootstrap or CodeFirst drift
   - database connection and dialect configuration
4. Propose a minimal fix that doesn't break existing functionality
5. Verify the fix against both code intent and likely SQL/runtime output
6. Consider edge cases: null inputs, empty result sets, concurrent writes, dialect differences

## Output Format
- Root cause analysis
- Proposed fix with code
- Test verification steps
"""


def _feature_prompt(context: str, target: str, related: str) -> str:
    return f"""You are implementing a new EasyQuery-related feature.

## Environment
- ORM: EasyQuery
- Language: Java and/or Kotlin
- Typical layers: entity, repository, service

## Feature Requirement
{context or "[Describe the feature here]"}

## Target Module
{target or "[Specify where this feature should be added]"}

{related}

## Instructions
1. Analyze the existing code structure to find the best integration point
2. Follow the project's existing patterns and conventions
3. For database operations: use the project's repository / EasyQuery patterns
4. Keep entity mapping, query composition, and service orchestration responsibilities separated
5. Prefer typed query builders and consistent transaction boundaries
6. Ensure documentation or examples reflect the new API usage
7. Consider:
   - pagination and sorting behavior
   - transaction semantics and concurrent access
   - proper error handling and diagnostics
   - migration or schema impact

## Output Format
- Architecture decision
- Implementation code
- Configuration additions (if any)
- Test plan
"""


def _architecture_review_prompt(context: str, target: str, related: str) -> str:
    return f"""You are reviewing the architecture of an EasyQuery-oriented project.

## Environment
- ORM: EasyQuery
- Language: Java and/or Kotlin
- Focus areas: entity mapping, repositories, services, transactions

## Review Context
{context or "[Describe what aspect to review]"}

## Target
{target or "[Specify the module/package to review]"}

{related}

## Review Checklist
1. **Data Layer**
   - Is the ORM / repository layer used correctly?
   - Are entity mappings and naming strategies explicit enough?
   - Is connection pooling or datasource usage configured sanely?

2. **Transaction Design**
   - Are transaction scopes tight and understandable?
   - Is concurrent write behavior handled intentionally?
   - Are retries or optimistic locks used where appropriate?

3. **Query Composition**
   - Are joins, projections, and pagination built in a readable way?
   - Are count queries and page queries consistent?
   - Is raw SQL used only where justified?

4. **Error Handling**
   - Are exceptions caught and logged appropriately?
   - Are generated SQL or mapping failures diagnosable?
   - Is there graceful degradation?

## Output Format
- Summary of findings
- Critical issues (must fix)
- Recommendations (should fix)
- Nice-to-haves (could improve)
"""


def _test_generation_prompt(context: str, target: str, related: str) -> str:
    return f"""You are generating tests for an EasyQuery-based component.

## Environment
- ORM: EasyQuery
- Testing: JUnit 5, H2 (or target DB), mocking framework as needed
- Language: Java and/or Kotlin

## Test Target
{target or "[Specify the class/function to test]"}

## Context
{context or "[Additional context about what to test]"}

{related}

## Testing Guidelines
1. **Unit Tests**
   - Test business logic in isolation
   - Mock external dependencies when behavior is outside query generation
   - Use H2 in-memory database for repository/DAO tests
   - Test edge cases: null inputs, empty collections, boundary values

2. **Integration Tests**
   - Exercise repository and query-builder behavior end-to-end
   - Verify generated SQL or resulting rows match expectations
   - Cover transaction rollback and commit paths

3. **Database Tests**
   - Use H2 for ORM / repository tests
   - Test CRUD operations
   - Test query builders
   - Test transaction behavior
   - Test optimistic locking if applicable

4. **Concurrency Tests**
   - Test concurrent database access
   - Test retry/backoff or optimistic locking behavior

## Output Format
- Test class with proper annotations
- Setup/teardown methods
- Individual test methods with clear names
- Assertions with meaningful messages
"""
