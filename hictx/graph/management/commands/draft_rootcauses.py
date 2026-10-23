"""
Draft LLM root causes for incidents in one or all repositories.

    uv run python manage.py draft_rootcauses --repo acme/payments
    uv run python manage.py draft_rootcauses --repo acme/payments --stub   # no API call
    uv run python manage.py draft_rootcauses --repo acme/payments --force   # re-draft

Thin command around graph.services.rootcause_drafter. Real runs call the Anthropic
API (needs ANTHROPIC_API_KEY or an `ant auth login` profile) and cost tokens;
--stub uses a deterministic offline client for demos and tests.
"""

from django.core.management.base import BaseCommand, CommandError

from hictx.graph.models import Repository
from hictx.graph.services.llm import StubLLMClient
from hictx.graph.services.rootcause_drafter import RootCauseDrafter


class Command(BaseCommand):
    help = "LLM-draft Incident root causes for one-click human confirmation."

    def add_arguments(self, parser):
        parser.add_argument("--repo", dest="repo", help="Repository full_name.")
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-draft incidents that already have LLM drafts (keeps human "
            "verdicts; clears only untouched inferred drafts).",
        )
        parser.add_argument(
            "--stub",
            action="store_true",
            help="Use the offline stub client instead of calling the API.",
        )

    def handle(self, *args, **options):
        repos = Repository.objects.all()
        if options.get("repo"):
            repos = repos.filter(full_name=options["repo"])
            if not repos.exists():
                raise CommandError(f"No repository named {options['repo']!r}.")

        llm_client = StubLLMClient() if options.get("stub") else None
        for repo in repos:
            drafter = RootCauseDrafter(repo, llm_client=llm_client)
            result = drafter.run(force=options.get("force", False))
            self._report(repo, result)

    def _report(self, repo, result):
        w = self.stdout.write
        w(self.style.MIGRATE_HEADING(f"\n{repo.full_name}"))
        w(f"  incidents processed : {result.incidents_processed}")
        w(f"  incidents drafted   : {result.incidents_drafted}")
        w(f"  root causes created : {result.root_causes_created}")
        for note in result.skipped:
            w(self.style.WARNING(f"  skipped: {note}"))
        w(self.style.SUCCESS("  done"))
