"""
Ingest Architecture Decision Records (ADRs) from a repository's clone.

    uv run python manage.py ingest_adrs --repo owner/repo

Reads numbered markdown ADRs (docs/adr/NNNN-*.md and similar) straight from the
clone — no token — and creates Decision rows keyed "ADR-N", which is what lets
"implements ADR-N" references in commits/PRs link up. The repo must already be
ingested (run ingest_repo first).
"""

from django.core.management.base import BaseCommand, CommandError

from hictx.graph.models import Repository
from hictx.graph.services.adr_ingest import ingest_adrs


class Command(BaseCommand):
    help = "Read ADR markdown files from the clone and store them as Decisions."

    def add_arguments(self, parser):
        parser.add_argument("--repo", required=True, help="Repository full_name.")

    def handle(self, *args, **options):
        repo = Repository.objects.filter(full_name=options["repo"]).first()
        if repo is None:
            raise CommandError(f"No repository {options['repo']!r}; run ingest_repo first.")
        if not repo.clone_path:
            raise CommandError(f"{repo.full_name} has no local clone.")

        result = ingest_adrs(repo)
        w = self.stdout.write
        w(self.style.MIGRATE_HEADING(f"\n{repo.full_name}"))
        w(f"  ADR files scanned : {result.files_scanned}")
        w(f"  decisions created : {result.decisions_created}")
        w(f"  decisions updated : {result.decisions_updated}")
        w(f"  supersedes linked : {result.supersedes_linked}")
        for note in result.notes:
            w(self.style.WARNING(f"  note: {note}"))
        w(self.style.SUCCESS("  done — run link_decisions " + repo.full_name
                             + " (or reconnect) to form implements edges"))
