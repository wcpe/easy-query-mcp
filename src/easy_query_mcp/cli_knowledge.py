"""CLI for knowledge mining and publishing."""

import argparse
import sys
import time

from .config import load_config
from .database import Database
from .knowledge_miner import KnowledgeMiner
from .knowledge_pipeline import KnowledgePipeline


def main_mine():
    """Entry point: easy-query-mcp-knowledge-mine"""
    parser = argparse.ArgumentParser(description="Mine knowledge from indexed source code")
    parser.add_argument("--config", "-c", default=None, help="Config file path")
    parser.add_argument("--db", "-d", default=None, help="Database path override")
    parser.add_argument("--repo", "-r", default=None, help="Only mine from this repo")
    parser.add_argument("--changed-only", action="store_true",
                        help="Only process files changed since last commit (git diff HEAD~1)")
    parser.add_argument("--since-commit", default=None,
                        help="Only process files changed since this commit (e.g. abc1234 or HEAD~5)")
    parser.add_argument("--auto-publish", type=float, default=None,
                        help="Auto-publish candidates above this confidence threshold after mining")
    args = parser.parse_args()

    config = load_config(args.config)
    db_path = args.db or config.db_path
    db = Database(db_path)
    db.initialize()

    print(f"Database: {db_path}")

    miner = KnowledgeMiner(db)
    start = time.time()
    stats = miner.mine_all(
        repo_id=args.repo,
        changed_only=args.changed_only,
        since_commit=args.since_commit,
    )
    elapsed = time.time() - start

    print(f"\nMining complete in {elapsed:.1f}s")
    print(f"  Chunks:     {stats['chunks']}")
    print(f"  Topics:     {stats['topics']}")
    print(f"  Candidates: {stats['candidates']}")
    print(f"  Relations:  {stats['relations']}")

    # Run error mining
    from .error_miner import ErrorMiner
    err_miner = ErrorMiner(db)
    err_stats = err_miner.mine_error_knowledge()
    print(f"  Error cards: {err_stats['cards_generated']}")
    print(f"  Error rels:  {err_stats['relations_added']}")

    # Run architecture mining
    from .architecture_miner import ArchitectureMiner
    arch_miner = ArchitectureMiner(db)
    arch_stats = arch_miner.mine_architecture(repo_id=args.repo)
    print(f"  Arch cards:  {arch_stats['cards_generated']}")
    print(f"  Arch rels:   {arch_stats['relations_added']}")

    from .anti_pattern_miner import AntiPatternMiner
    anti_miner = AntiPatternMiner(db)
    anti_stats = anti_miner.scan(repo_id=args.repo, persist=True)
    print(f"  Anti-pattern findings: {anti_stats['findings']}")
    print(f"  Anti-pattern cards:    {anti_stats['cards_generated']}")

    from .fix_miner import FixPatternMiner
    fix_miner = FixPatternMiner(db)
    fix_stats = fix_miner.mine(repo_id=args.repo, persist=True)
    print(f"  Fix-pattern commits: {fix_stats['matched_commits']}")
    print(f"  Fix-pattern cards:   {fix_stats['cards_generated']}")

    from .knowledge_health import refresh_knowledge_health
    health_stats = refresh_knowledge_health(db)
    print(f"  Health active/review/stale: "
          f"{health_stats['knowledge_cards']['active']}/"
          f"{health_stats['knowledge_cards']['review']}/"
          f"{health_stats['knowledge_cards']['stale']}")

    if args.auto_publish is not None:
        pipeline = KnowledgePipeline(db)
        count = pipeline.publish_above_confidence(args.auto_publish)
        print(f"  Auto-published: {count} cards above {args.auto_publish}")

    # Show top candidates
    candidates = db.get_candidates(status="candidate", limit=20)
    if candidates:
        print(f"\nTop candidates (by confidence):")
        for c in candidates[:10]:
            print(f"  [{c['confidence']:.2f}] {c['title']} ({c['category']}) - {c['card_id']}")

    db.close()


def main_publish():
    """Entry point: easy-query-mcp-knowledge-publish"""
    parser = argparse.ArgumentParser(description="Review and publish candidate knowledge cards")
    parser.add_argument("--config", "-c", default=None, help="Config file path")
    parser.add_argument("--db", "-d", default=None, help="Database path override")
    parser.add_argument("--fixtures", default=None, help="Evaluation fixture directory override for publish gating")
    sub = parser.add_subparsers(dest="action", help="Action to perform")

    # List candidates
    list_p = sub.add_parser("list", help="List candidate cards")
    list_p.add_argument("--status", "-s", default=None, help="Filter by status")
    list_p.add_argument("--category", default=None, help="Filter by category")

    # Show a candidate
    show_p = sub.add_parser("show", help="Show a candidate card")
    show_p.add_argument("card_id", help="Card ID or slug")

    # Review
    review_p = sub.add_parser("review", help="Approve a candidate")
    review_p.add_argument("card_id", help="Card ID or slug")
    review_p.add_argument("--reject", action="store_true", help="Reject instead of approve")
    review_p.add_argument("--reviewer", default="", help="Reviewer name or handle")
    review_p.add_argument("--owner", default="", help="Owner or maintainer for this card")
    review_p.add_argument("--notes", default="", help="Review notes / rationale")
    review_p.add_argument("--change-summary", default="", help="Summary of what changed and why this card matters")
    review_p.add_argument("--evidence-quality", default="", help="Evidence quality label: low, medium, high")

    # Publish
    pub_p = sub.add_parser("publish", help="Publish a candidate to knowledge_cards")
    pub_p.add_argument("card_id", nargs="?", default=None, help="Card ID (or --all / --auto)")
    pub_p.add_argument("--all", action="store_true", help="Publish all reviewed")
    pub_p.add_argument("--auto", type=float, default=None,
                       help="Auto-publish above confidence threshold (e.g. 0.7)")
    pub_p.add_argument("--min-evidence-count", type=int, default=2,
                       help="Minimum evidence count required for auto-publish gate")

    # Export
    export_p = sub.add_parser("export", help="Export candidate as markdown file")
    export_p.add_argument("card_id", help="Card ID or slug")
    export_p.add_argument("--output", "-o", default="knowledge_cards", help="Output directory")

    args = parser.parse_args()

    config = load_config(args.config)
    db_path = args.db or config.db_path
    db = Database(db_path)
    db.initialize()

    pipeline = KnowledgePipeline(db)

    if args.action == "list":
        cards = pipeline.list_candidates(status=args.status, category=args.category)
        if not cards:
            print("No candidates found.")
        else:
            print(f"{'ID':<30} {'Status':<12} {'Conf':>5} {'Category':<15} Title")
            print("-" * 100)
            for c in cards:
                print(f"{c['card_id']:<30} {c['status']:<12} {c['confidence']:>5.2f} "
                      f"{c['category']:<15} {c['title']}")

    elif args.action == "show":
        card = db.get_candidate(args.card_id)
        if card is None:
            print(f"Not found: {args.card_id}")
        else:
            print(f"Title:      {card['title']}")
            print(f"Slug:       {card['slug']}")
            print(f"Category:   {card['category']}")
            print(f"Status:     {card['status']}")
            print(f"Confidence: {card['confidence']}")
            print(f"Tags:       {card['tags']}")
            print(f"Keywords:   {card['keywords']}")
            print(f"Symbols:    {card['related_symbols'][:100]}")
            print(f"Owner:      {card.get('owner', '')}")
            print(f"Reviewer:   {card.get('reviewed_by', '')}")
            print(f"EvidenceQ:  {card.get('evidence_quality', '')}")
            print(f"Change:     {card.get('change_summary', '')[:120]}")
            print(f"\n--- Summary ---\n{card['summary']}")
            print(f"\n--- Content (first 500 chars) ---\n{card['content'][:500]}")
            print(f"\n--- Evidence ---\n{card['evidence'][:500]}")
            print(f"\n--- Review Notes ---\n{card.get('review_notes', '')[:500]}")

    elif args.action == "review":
        ok = pipeline.review_card(
            args.card_id,
            approve=not args.reject,
            reviewer=args.reviewer,
            review_notes=args.notes,
            owner=args.owner,
            change_summary=args.change_summary,
            evidence_quality=args.evidence_quality,
        )
        action = "rejected" if args.reject else "approved"
        print(f"Card {args.card_id}: {action}" if ok else f"Failed to review {args.card_id}")

    elif args.action == "publish":
        if args.all:
            count = pipeline.publish_all_reviewed(fixture_dir=args.fixtures)
            print(f"Published {count} reviewed cards")
        elif args.auto is not None:
            count = pipeline.publish_above_confidence(
                args.auto,
                fixture_dir=args.fixtures,
                min_evidence_count=args.min_evidence_count,
            )
            print(f"Auto-published {count} cards above confidence {args.auto}")
        elif args.card_id:
            slug = pipeline.publish_card(args.card_id, fixture_dir=args.fixtures)
            if slug:
                print(f"Published: {slug}")
            else:
                print(f"Failed to publish: {args.card_id}")
        else:
            print("Specify a card_id, --all, or --auto <threshold>")

    elif args.action == "export":
        path = pipeline.export_candidate_md(args.card_id, args.output)
        if path:
            print(f"Exported to: {path}")
        else:
            print(f"Not found: {args.card_id}")

    else:
        parser.print_help()

    db.close()
