"""
Ingest a repository's pull requests as CodeChanges (with body + merge sha).

    uv run python manage.py ingest_prs --repo owner/repo --token $GITHUB_TOKEN

For squash-merge repos this is what makes linking possible: the PR body carries
the "fixes #N" closing refs that the commits lack. Needs a token for real volume.
The repo must already exist (run ingest_repo first).
"""

import os

from django.core.management.base import BaseCommand, CommandError

from hictx.graph.models import Repository
from hictx.graph.services.github_issues import GitHubIssuesClient
from hictx.graph.services.github_prs import ingest_pull_requests


class Command(BaseCommand):
    help = "Fetch GitHub pull requests and store them as CodeChanges."

    def add_arguments(self, parser):
        parser.add_argument("--repo", required=True, help="Repository full_name.")
        parser.add_argument("--token", default="", help="GitHub token (or $GITHUB_TOKEN).")
        parser.add_argument("--state", default="all", choices=["all", "open", "closed"])
        parser.add_argument("--max-pages", type=int, default=20)

    def handle(self, *args, **options):
        repo = Repository.objects.filter(full_name=options["repo"]).first()
        if repo is None:
            raise CommandError(f"No repository {options['repo']!r}; run ingest_repo first.")
        token = options.get("token") or os.environ.get("GITHUB_TOKEN", "")
        client = GitHubIssuesClient(token=token, max_pages=options["max_pages"])
        try:
            result = ingest_pull_requests(repo, token=token, state=options["state"],
                                          client=client)
        except (RuntimeError, ValueError) as exc:
            raise CommandError(str(exc))

        w = self.stdout.write
        w(self.style.MIGRATE_HEADING(f"\n{repo.full_name}"))
        w(f"  PRs created      : {result.prs_created}")
        w(f"  PRs updated      : {result.prs_updated}")
        w(f"  merged (with sha): {result.merged_with_sha}")
        w(self.style.SUCCESS(
            "  done — now run: ingest_issues (or seed) + link_incidents " + repo.full_name
        ))
