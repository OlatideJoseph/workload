"""
Ingest a repository's GitHub issues as Incidents.

    uv run python manage.py ingest_issues --repo owner/repo --token $GITHUB_TOKEN

Token is optional for public repos (higher rate limit) and required for private
ones. The repo must already exist (run ingest_repo first, or pass --url to create
it on the fly without cloning).
"""

import os

from django.core.management.base import BaseCommand, CommandError

from hictx.graph.models import Provider, Repository
from hictx.graph.services.github_issues import (
    GitHubIssuesClient,
    ingest_repository_issues,
)
from hictx.graph.services.ingest import derive_full_name


class Command(BaseCommand):
    help = "Fetch GitHub issues and upsert them as Incidents."

    def add_arguments(self, parser):
        parser.add_argument("--repo", help="Existing repository full_name (owner/repo).")
        parser.add_argument("--url", help="Create the repo from this URL if missing.")
        parser.add_argument("--token", default="", help="GitHub token (or $GITHUB_TOKEN).")
        parser.add_argument("--state", default="all", choices=["all", "open", "closed"])
        parser.add_argument("--max-pages", type=int, default=20,
                            help="Cap on 100-issue pages fetched (bounds huge repos).")

    def handle(self, *args, **options):
        repo = self._resolve_repo(options)
        token = options.get("token") or os.environ.get("GITHUB_TOKEN", "")
        client = GitHubIssuesClient(token=token, max_pages=options["max_pages"])
        try:
            result = ingest_repository_issues(
                repo, token=token, state=options["state"], client=client
            )
        except (RuntimeError, ValueError) as exc:
            raise CommandError(str(exc))

        w = self.stdout.write
        w(self.style.MIGRATE_HEADING(f"\n{repo.full_name}"))
        w(f"  incidents created : {result.incidents_created}")
        w(f"  incidents updated : {result.incidents_updated}")
        w(f"  PRs skipped       : {result.skipped_pull_requests}")
        w(self.style.SUCCESS("  done — now run: link_incidents --repo " + repo.full_name))

    def _resolve_repo(self, options):
        if options.get("repo"):
            repo = Repository.objects.filter(full_name=options["repo"]).first()
            if repo:
                return repo
            if not options.get("url"):
                raise CommandError(
                    f"No repository {options['repo']!r}; pass --url to create it."
                )
        if options.get("url"):
            full_name = options.get("repo") or derive_full_name(options["url"])
            repo, _ = Repository.objects.get_or_create(
                provider=Provider.GITHUB,
                full_name=full_name,
                defaults={"url": options["url"]},
            )
            return repo
        raise CommandError("Pass --repo (existing) or --url (to create).")
