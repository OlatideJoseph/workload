"""
Backfill a repository from a git clone — no token needed for public repos.

    uv run python manage.py ingest_repo --url https://github.com/owner/repo --limit 500
    uv run python manage.py ingest_repo --url /path/to/local/repo --name owner/repo

Clones (or fetches) into GRAPH_CLONE_ROOT, then walks history to populate commits,
files, and the factual affects edges. Issue/PR metadata (incidents) still needs the
GitHub API and is out of scope here — this is the clone-only path.
"""

from django.core.management.base import BaseCommand, CommandError

from hictx.graph.services.git_gateway import GitError
from hictx.graph.services.ingest import ingest_repository


class Command(BaseCommand):
    help = "Clone a repo and backfill commits/files/affects from its history."

    def add_arguments(self, parser):
        parser.add_argument("--url", required=True, help="Git URL or local path.")
        parser.add_argument("--name", default="", help="Override owner/repo name.")
        parser.add_argument("--branch", default="HEAD", help="Ref to walk.")
        parser.add_argument("--limit", type=int, help="Max commits (newest first).")
        parser.add_argument(
            "--no-clone", action="store_true",
            help="Skip clone/fetch; backfill from the existing clone only.",
        )

    def handle(self, *args, **options):
        try:
            result = ingest_repository(
                url=options["url"],
                full_name=options["name"],
                branch=options["branch"],
                limit=options.get("limit"),
                clone=not options.get("no_clone", False),
            )
        except GitError as exc:
            raise CommandError(str(exc))

        repo = result.repository
        w = self.stdout.write
        w(self.style.MIGRATE_HEADING(f"\n{repo.full_name}"))
        w(f"  clone_path        : {repo.clone_path}")
        w(f"  commits ingested  : {result.commits_ingested}")
        w(f"  affects edges     : {result.affects_edges}")
        w(self.style.SUCCESS("  done — now run: link_decisions / link_incidents / "
                             "run_context_test on this repo"))
