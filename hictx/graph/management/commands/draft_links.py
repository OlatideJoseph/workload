"""
Draft change -> decision ("implements") links for human review.

    uv run python manage.py draft_links --repo cloudfoundry/korifi            # keyword (offline)
    uv run python manage.py draft_links --repo cloudfoundry/korifi --llm --use-diff

Proposes candidate links the explicit-reference heuristic misses, as inferred
LLM-sourced edges that show up in the review queue. --llm uses a real model
(needs ANTHROPIC_API_KEY, costs tokens); default is the offline keyword client.
"""

from django.core.management.base import BaseCommand, CommandError

from hictx.graph.models import Repository
from hictx.graph.services.link_drafter import (
    KeywordLinkDraftClient,
    LLMLinkDraftClient,
    draft_implements_links,
)
from hictx.graph.services.app_settings import active_task_runner


class Command(BaseCommand):
    help = "Propose change->decision links (keyword or LLM) for the review queue."

    def add_arguments(self, parser):
        parser.add_argument("--repo", required=True, help="Repository full_name.")
        parser.add_argument("--limit", type=int, help="Max changes to scan.")
        parser.add_argument("--min-confidence", type=float, default=0.5)
        parser.add_argument("--llm", action="store_true", help="Use a real model.")
        parser.add_argument("--use-diff", action="store_true",
                            help="Also read each commit's diff (slower, richer).")

    def handle(self, *args, **options):
        repo = Repository.objects.filter(full_name=options["repo"]).first()
        if repo is None:
            raise CommandError(f"No repository {options['repo']!r}.")

        if options["llm"]:
            client = LLMLinkDraftClient(active_task_runner())
        else:
            client = KeywordLinkDraftClient()

        result = draft_implements_links(
            repo, client=client, limit=options.get("limit"),
            use_diff=options["use_diff"], min_confidence=options["min_confidence"],
        )
        w = self.stdout.write
        w(self.style.MIGRATE_HEADING(f"\n{repo.full_name}"
                                     + ("  (LLM)" if options["llm"] else "  (keyword)")))
        w(f"  changes scanned : {result.changes_processed}")
        w(f"  links drafted   : {result.links_drafted}")
        w(f"  links refreshed : {result.links_refreshed}")
        w(f"  human-kept skips: {result.skipped_human}")
        for note in result.notes:
            w(self.style.WARNING(f"  note: {note}"))
        w(self.style.SUCCESS("  done — confirm/reject them in the Review tab"))
