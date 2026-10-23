"""
Phase 5: evaluate whether captured context improves an AI task, per repository.

    uv run python manage.py evaluate --repo acme/payments --stub
    uv run python manage.py evaluate --repo acme/payments --task migrate --target Go

--stub uses an offline echo-runner + keyword judge to demonstrate the metric with
no API cost (the numbers are a wiring proof, not a real model result). Drop --stub
(set ANTHROPIC_API_KEY) for a real evaluation with an LLM judge.

Headline: DELTA = must-preserve items the with-context run kept that the
context-free run dropped (the Phase 0 metric).
"""

from django.core.management.base import BaseCommand, CommandError

from hictx.graph.models import Repository
from hictx.graph.services.evaluation import (
    KeywordJudge,
    LLMJudge,
    evaluate_repository,
)
from hictx.graph.services.app_settings import active_task_runner
from hictx.graph.services.llm import EchoTaskRunner


class Command(BaseCommand):
    help = "Evaluate the value of context: with vs without, across a repo's entities."

    def add_arguments(self, parser):
        parser.add_argument("--repo", required=True, help="Repository full_name.")
        parser.add_argument("--task", default="explain", choices=["explain", "migrate"])
        parser.add_argument("--target", default="", help="Target language for migrate.")
        parser.add_argument("--stub", action="store_true", help="Offline echo runner + keyword judge.")

    def handle(self, *args, **options):
        repo = Repository.objects.filter(full_name=options["repo"]).first()
        if repo is None:
            raise CommandError(f"No repository named {options['repo']!r}.")

        if options.get("stub"):
            runner, judge = EchoTaskRunner(), KeywordJudge()
        else:
            runner = active_task_runner()
            judge = LLMJudge(runner)

        report = evaluate_repository(
            repo, runner=runner, judge=judge,
            task=options["task"], min_confidence=0.0,
        )
        self._report(repo, report, stub=options.get("stub", False))

    def _report(self, repo, report, stub):
        w = self.stdout.write
        w(self.style.MIGRATE_HEADING(f"\nEvaluation: {repo.full_name}"
                                     + ("  (stub — wiring proof, not a model result)" if stub else "")))
        if not report.entities:
            w(self.style.WARNING("  no entities with trusted context to evaluate"))
            return
        for e in report.entities:
            w(f"  {e.entity_path}: preserved with={e.preserved_with}/"
              f"{e.items_total}  without={e.preserved_without}/{e.items_total}  "
              f"delta=+{e.delta}")
        w(self.style.MIGRATE_HEADING("\n  TOTALS"))
        w(f"  must-preserve items : {report.items_total}")
        w(f"  preserved WITH ctx  : {report.preserved_with}")
        w(f"  preserved WITHOUT   : {report.preserved_without}")
        w(self.style.SUCCESS(
            f"  DELTA (context win) : +{report.delta}  "
            "(items kept thanks to context that a context-free run dropped)"
        ))
