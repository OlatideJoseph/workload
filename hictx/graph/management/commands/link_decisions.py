"""
Run the Phase 2 change-to-decision linker over one or all repositories.

    uv run python manage.py link_decisions --repo acme/payments

Thin command around graph.services.decision_linker.
"""

from django.core.management.base import BaseCommand, CommandError

from hictx.graph.models import Repository
from hictx.graph.services.decision_linker import DecisionLinker


class Command(BaseCommand):
    help = "Infer CodeChange --implements--> Decision edges from ADR references."

    def add_arguments(self, parser):
        parser.add_argument(
            "--repo",
            dest="repo",
            help="Repository full_name (owner/name). Omit to run over all repos.",
        )

    def handle(self, *args, **options):
        repos = Repository.objects.all()
        if options.get("repo"):
            repos = repos.filter(full_name=options["repo"])
            if not repos.exists():
                raise CommandError(f"No repository named {options['repo']!r}.")

        for repo in repos:
            result = DecisionLinker(repo).run()
            self._report(repo, result)

    def _report(self, repo, result):
        w = self.stdout.write
        w(self.style.MIGRATE_HEADING(f"\n{repo.full_name}"))
        w(f"  changes processed  : {result.changes_processed}")
        w(f"  implements created : {result.edges_created}")
        w(f"  implements refreshed: {result.edges_refreshed}")
        for note in result.skipped:
            w(self.style.WARNING(f"  skipped: {note}"))
        w(self.style.SUCCESS("  done"))
