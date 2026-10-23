"""
Run the Phase 2 incident-to-commit linker over one or all repositories.

    uv run python manage.py link_incidents --repo acme/payments

Thin command: all logic lives in graph.services.incident_linker (Two Scoops:
keep management commands as a shell around a callable service).
"""

from django.core.management.base import BaseCommand, CommandError

from hictx.graph.models import Repository
from hictx.graph.services.incident_linker import IncidentCommitLinker


class Command(BaseCommand):
    help = "Infer Incident --caused_by--> CodeChange edges via SZZ-style blame."

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
            if not repo.clone_path:
                self.stderr.write(
                    self.style.WARNING(f"{repo.full_name}: no clone_path set; skipping")
                )
                continue
            result = IncidentCommitLinker(repo).run()
            self._report(repo, result)

    def _report(self, repo, result):
        w = self.stdout.write
        w(self.style.MIGRATE_HEADING(f"\n{repo.full_name}"))
        w(f"  incidents processed : {result.incidents_processed}")
        w(f"  with a fix found    : {result.incidents_with_fix}")
        w(f"  caused_by created   : {result.edges_created}")
        w(f"  caused_by refreshed : {result.edges_refreshed}")
        for note in result.skipped:
            w(self.style.WARNING(f"  skipped: {note}"))
        w(self.style.SUCCESS("  done"))
