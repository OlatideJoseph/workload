"""
Phase 4: run an AI task on a code entity WITH and WITHOUT its connected context.

    uv run python manage.py run_context_test --repo acme/payments \
        --entity payments/webhooks.py::handle_charge_succeeded \
        --source path/to/file.py --task explain --stub

--stub uses an offline runner (no API cost) that reports whether each prompt
carried context — enough to prove the wiring. Drop --stub (and set
ANTHROPIC_API_KEY) for a real comparison.
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from hictx.graph.models import CodeEntity, Repository
from hictx.graph.services.context_test import run_context_test
from hictx.graph.services.llm import StubTaskRunner


class Command(BaseCommand):
    help = "Run an AI task with and without the entity's connected context."

    def add_arguments(self, parser):
        parser.add_argument("--repo", required=True, help="Repository full_name.")
        parser.add_argument(
            "--entity", required=True,
            help="Entity as 'path' or 'path::qualified_name'.",
        )
        parser.add_argument("--source", help="File whose contents are the source code.")
        parser.add_argument("--task", default="explain", choices=["explain", "migrate"])
        parser.add_argument("--target", default="", help="Target language for migrate.")
        parser.add_argument("--stub", action="store_true", help="Offline runner.")

    def handle(self, *args, **options):
        repo = Repository.objects.filter(full_name=options["repo"]).first()
        if repo is None:
            raise CommandError(f"No repository named {options['repo']!r}.")

        path, _, name = options["entity"].partition("::")
        entity = CodeEntity.objects.filter(
            repository=repo, path=path, name=name
        ).first()
        if entity is None:
            raise CommandError(
                f"No entity {options['entity']!r} in {repo.full_name}."
            )

        if options.get("source"):
            source_code = Path(options["source"]).read_text(encoding="utf-8")
        else:
            source_code = "# (source not supplied; passing a placeholder)\n"

        runner = StubTaskRunner() if options.get("stub") else None
        result = run_context_test(
            entity, source_code, task=options["task"],
            target_language=options.get("target", ""), runner=runner,
        )
        self._report(result)

    def _report(self, result):
        w = self.stdout.write
        w(self.style.MIGRATE_HEADING(f"\nContext test: {result.task} {result.entity}"))
        w(f"  trusted context found: {result.had_context}")
        w(self.style.MIGRATE_HEADING("\n--- CONNECTED CONTEXT fed to the WITH run ---"))
        w(result.context_text)
        w(self.style.MIGRATE_HEADING("--- WITHOUT context ---"))
        w(result.without_context)
        w(self.style.MIGRATE_HEADING("\n--- WITH context ---"))
        w(result.with_context)
        w(self.style.SUCCESS("\ndone"))
