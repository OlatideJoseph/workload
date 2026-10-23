"""
API over the context graph.

Reads are the ReadOnly viewsets; Phase 3 adds the two write surfaces that keep
the graph trusted and fresh: one-click confirm/reject on any inferred link
(`ReviewActionsMixin`) and the PR-open "why?" capture (`PRWhyCaptureView`).
"""

from django.conf import settings as django_settings
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Session

from .models import (
    CausedByEdge,
    CodeChange,
    CodeEntity,
    Decision,
    ImplementsEdge,
    Incident,
    Repository,
    RootCause,
)
from .serializers import (
    CaptureResultSerializer,
    CausedByEdgeSerializer,
    CodeChangeSerializer,
    CodeEntitySerializer,
    DecisionSerializer,
    ImplementsEdgeSerializer,
    IncidentSerializer,
    IncidentSubgraphSerializer,
    CausedByReviewSerializer,
    ChangeDiffSerializer,
    ImplementsReviewSerializer,
    PRWhyCaptureSerializer,
    RepositorySerializer,
    RootCauseReviewSerializer,
    DraftLinksRequestSerializer,
    EntityTimelineSerializer,
    LinkDraftResultSerializer,
    LLMConfigSerializer,
    LLMSettingsSerializer,
    RepositoryGraphSerializer,
    RepositoryTimelineSerializer,
    ReviewCountsSerializer,
    ReviewQueueSerializer,
    StoryFileSerializer,
    StoryNarrativeSerializer,
    RootCauseSerializer,
    SessionCreateSerializer,
    SessionRetrySerializer,
    SessionSerializer,
)
from .services.capture import capture_pr_rationale
from .services.graph_view import repository_graph
from .services.session import start_session


class ReviewActionsMixin:
    """Adds POST /{id}/confirm/ and /{id}/reject/ to a provenance-bearing viewset."""

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        obj = self.get_object()
        obj.confirm(reviewer=getattr(request, "user", None))
        return Response(self.get_serializer(obj).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        obj = self.get_object()
        obj.reject(reviewer=getattr(request, "user", None))
        return Response(self.get_serializer(obj).data)


class RepositoryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Repository.objects.all()
    serializer_class = RepositorySerializer

    @extend_schema(request=DraftLinksRequestSerializer, responses=LinkDraftResultSerializer)
    @action(detail=True, methods=["post"], url_path="draft-links")
    def draft_links(self, request, pk=None):
        """
        Propose change->decision links the explicit-reference heuristic missed,
        as inferred edges for the review queue. Uses the LLM client when a
        provider key is configured (bounded by `limit`), else the offline
        keyword client. Body: {use_llm?, use_diff?, limit?}.
        """
        from .services.app_settings import active_task_runner, is_configured
        from .services.link_drafter import (
            KeywordLinkDraftClient,
            LLMLinkDraftClient,
            draft_implements_links,
        )

        form = DraftLinksRequestSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        opts = form.validated_data

        has_key = is_configured()
        use_llm = opts["use_llm"]
        if use_llm is None:
            use_llm = has_key
        use_llm = use_llm and has_key

        repo = self.get_object()
        if use_llm:
            client = LLMLinkDraftClient(active_task_runner())
            # Keep a synchronous request bounded; LLM calls are sequential.
            limit = opts["limit"] or 40
            used = "llm"
        else:
            client = KeywordLinkDraftClient()
            limit = opts["limit"]
            used = "keyword"

        result = draft_implements_links(
            repo, client=client, limit=limit, use_diff=opts["use_diff"],
        )
        data = LinkDraftResultSerializer(result).data
        data["client"] = used
        return Response(data)

    @extend_schema(request=None, responses=StoryNarrativeSerializer)
    @action(detail=True, methods=["get", "post"])
    def story(self, request, pk=None):
        """
        GET the cached LLM narrative for this repo; POST to (re)generate it.
        Generation needs a provider key (set once in Settings); the result is
        cached so it isn't regenerated on every visit.
        """
        from .models import Setting
        from .services.app_settings import active_task_runner, get_model, is_configured
        from .services.story import generate_narrative

        repo = self.get_object()
        cache_key = f"story_narrative:{repo.pk}"

        if request.method == "POST":
            if not is_configured():
                return Response(
                    {"detail": "No LLM provider key configured — add one in Settings."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            Setting.put(cache_key, generate_narrative(repo, active_task_runner()))

        row = Setting.objects.filter(name=cache_key).first()
        return Response({
            "narrative": row.value if row else None,
            "generated_at": row.updated_at.isoformat() if row else None,
            "model": get_model(),
            "configured": is_configured(),
        })

    @extend_schema(responses=RepositoryTimelineSerializer)
    @action(detail=True)
    def timeline(self, request, pk=None):
        """The repo's notable events in time order — the story view."""
        from .services.timeline import repository_timeline

        repo = self.get_object()
        try:
            limit = int(request.query_params.get("limit", 200))
        except ValueError:
            limit = 200
        return Response(repository_timeline(repo, limit=limit))

    @extend_schema(responses=StoryFileSerializer(many=True))
    @action(detail=True, url_path="story-files")
    def story_files(self, request, pk=None):
        """
        Files worth a story, ranked by how much DISTINCT decision/incident context
        they carry. A connected PR touches many files, so we rank by the number of
        distinct decisions/incidents reachable through a file's changes and return
        the richest ones, not every file the PR happened to touch.
        """
        from collections import defaultdict

        from .models import AffectsEdge, CausedByEdge, CodeEntity, ImplementsEdge

        repo = self.get_object()
        change_ctx: dict[int, set] = defaultdict(set)
        for cid, did in ImplementsEdge.objects.filter(
            decision__repository=repo
        ).values_list("code_change_id", "decision_id"):
            change_ctx[cid].add(("d", did))
        for cid, iid in CausedByEdge.objects.filter(
            incident__repository=repo
        ).values_list("code_change_id", "incident_id"):
            change_ctx[cid].add(("i", iid))

        entity_ctx: dict[int, set] = defaultdict(set)
        for eid, cid in AffectsEdge.objects.filter(
            code_change_id__in=change_ctx
        ).values_list("code_entity_id", "code_change_id"):
            entity_ctx[eid] |= change_ctx[cid]

        top = sorted(entity_ctx.items(), key=lambda kv: -len(kv[1]))[:50]
        by_id = CodeEntity.objects.in_bulk([eid for eid, _ in top])
        rows = [
            {"id": eid, "path": by_id[eid].path, "name": by_id[eid].name,
             "context_count": len(ctx)}
            for eid, ctx in top if eid in by_id
        ]
        return Response(StoryFileSerializer(rows, many=True).data)

    @extend_schema(responses=RepositoryGraphSerializer)
    @action(detail=True)
    def graph(self, request, pk=None):
        """
        The connected knowledge core (nodes + edges) for the graph preview.
        Query params: include_inferred=true|false, min_confidence=<float>.
        """
        repo = self.get_object()
        include_inferred = request.query_params.get("include_inferred", "true") != "false"
        try:
            min_confidence = float(request.query_params.get("min_confidence", 0.5))
        except ValueError:
            min_confidence = 0.5
        return Response(
            repository_graph(repo, include_inferred=include_inferred,
                             min_confidence=min_confidence)
        )


class DecisionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Decision.objects.all()
    serializer_class = DecisionSerializer


class CodeChangeViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = CodeChange.objects.all()
    serializer_class = CodeChangeSerializer

    @extend_schema(responses=ChangeDiffSerializer)
    @action(detail=True)
    def diff(self, request, pk=None):
        """The unified diff this change introduced, read from the local clone."""
        from dataclasses import asdict

        from .services.diff_view import change_diff

        return Response(asdict(change_diff(self.get_object())))


class CodeEntityViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = CodeEntity.objects.all()
    serializer_class = CodeEntitySerializer

    @action(detail=True)
    def subgraph(self, request, pk=None):
        """
        The connected, trust-filtered subgraph for this entity — the context a
        migration retrieves. Query params: include_inferred=true|false,
        min_confidence=<float>.
        """
        from .services.retrieval import retrieve_entity_subgraph

        entity = self.get_object()
        include_inferred = request.query_params.get("include_inferred", "true") != "false"
        try:
            min_confidence = float(request.query_params.get("min_confidence", 0.5))
        except ValueError:
            min_confidence = 0.5
        sub = retrieve_entity_subgraph(
            entity, include_inferred=include_inferred, min_confidence=min_confidence
        )
        return Response(sub.to_dict())

    @extend_schema(responses=EntityTimelineSerializer)
    @action(detail=True)
    def timeline(self, request, pk=None):
        """This file's notable events in time order — the per-file story."""
        from .services.timeline import file_timeline

        return Response(file_timeline(self.get_object()))

    @extend_schema(request=None, responses=StoryNarrativeSerializer)
    @action(detail=True, methods=["get", "post"])
    def story(self, request, pk=None):
        """GET the cached per-file narrative; POST to (re)generate it (needs a key)."""
        from .models import Setting
        from .services.app_settings import active_task_runner, get_model, is_configured
        from .services.story import generate_file_narrative

        entity = self.get_object()
        cache_key = f"story_narrative:entity:{entity.pk}"
        if request.method == "POST":
            if not is_configured():
                return Response(
                    {"detail": "No LLM provider key configured — add one in Settings."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            Setting.put(cache_key, generate_file_narrative(entity, active_task_runner()))
        row = Setting.objects.filter(name=cache_key).first()
        return Response({
            "narrative": row.value if row else None,
            "generated_at": row.updated_at.isoformat() if row else None,
            "model": get_model(),
            "configured": is_configured(),
        })


class RootCauseViewSet(ReviewActionsMixin, viewsets.ReadOnlyModelViewSet):
    queryset = RootCause.objects.all()
    serializer_class = RootCauseSerializer


class IncidentViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Incident.objects.all()
    serializer_class = IncidentSerializer

    @action(detail=True)
    def subgraph(self, request, pk=None):
        """The connected subgraph a migration would retrieve for this incident."""
        incident = self.get_object()
        return Response(IncidentSubgraphSerializer(incident).data)


class ImplementsEdgeViewSet(ReviewActionsMixin, viewsets.ReadOnlyModelViewSet):
    queryset = ImplementsEdge.objects.all()
    serializer_class = ImplementsEdgeSerializer


class CausedByEdgeViewSet(ReviewActionsMixin, viewsets.ReadOnlyModelViewSet):
    queryset = CausedByEdge.objects.all()
    serializer_class = CausedByEdgeSerializer


class PRWhyCaptureView(APIView):
    """
    POST the answer to "why does this change exist?" at PR-open time.

    Creates a human-authored Decision and a confirmed implements edge — trusted
    context captured in one sentence.
    """

    @extend_schema(request=PRWhyCaptureSerializer, responses=CaptureResultSerializer)
    def post(self, request):
        serializer = PRWhyCaptureSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = capture_pr_rationale(
            reviewer=getattr(request, "user", None),
            **serializer.validated_data,
        )
        return Response(
            CaptureResultSerializer(result).data, status=status.HTTP_201_CREATED
        )


class SessionViewSet(
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    """
    Connect-a-repo sessions. POST to start one (runs in the background); GET a
    session to poll its progress; POST {id}/retry/ to resume a failed one.
    """

    queryset = Session.objects.order_by("-created_at")
    serializer_class = SessionSerializer

    @extend_schema(request=SessionCreateSerializer, responses=SessionSerializer)
    def create(self, request, *args, **kwargs):
        form = SessionCreateSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        data = form.validated_data
        token = data.pop("token", "")
        session = Session.objects.create(
            url=data["url"],
            full_name=data.get("full_name", ""),
            ingest_issues=data.get("ingest_issues", True),
            limit=data.get("limit"),
        )
        # Kick off the work in the background; the client polls this session.
        start_session(session, token=token)
        return Response(
            SessionSerializer(session).data, status=status.HTTP_202_ACCEPTED
        )

    @extend_schema(request=SessionRetrySerializer, responses=SessionSerializer)
    @action(detail=True, methods=["post"])
    def retry(self, request, pk=None):
        """Re-run a session — resumes idempotently, skipping ingested commits."""
        session = self.get_object()
        form = SessionRetrySerializer(data=request.data)
        form.is_valid(raise_exception=True)
        start_session(session, token=form.validated_data.get("token", ""))
        return Response(SessionSerializer(session).data)


class LLMSettingsView(APIView):
    """
    GET the (non-secret) LLM config — the selected provider and per-provider status
    (Anthropic / OpenAI / Gemini). PUT to select a provider and set its key/model.
    Keys are stored once and never returned; an env var overrides a stored key.
    """

    @extend_schema(responses=LLMSettingsSerializer)
    def get(self, request):
        from .services.app_settings import status as llm_status

        return Response(LLMSettingsSerializer(llm_status()).data)

    @extend_schema(request=LLMConfigSerializer, responses=LLMSettingsSerializer)
    def put(self, request):
        from .services.app_settings import (
            PROVIDERS, set_api_key, set_model, set_provider, status as llm_status,
        )

        form = LLMConfigSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        data = form.validated_data
        provider = data["provider"]
        if provider not in PROVIDERS:
            return Response({"detail": f"Unknown provider {provider!r}."},
                            status=status.HTTP_400_BAD_REQUEST)
        # Selecting the provider; set its key/model when supplied (blank clears).
        set_provider(provider)
        if "key" in data:
            set_api_key(provider, data["key"])
        if data.get("model") is not None:
            set_model(provider, data["model"])
        return Response(LLMSettingsSerializer(llm_status()).data)


class ReviewCountsView(APIView):
    """Per-status totals (inferred/confirmed/rejected) for the Review sub-tab badges."""

    @extend_schema(responses=ReviewCountsSerializer)
    def get(self, request):
        counts = {}
        for st in ("inferred", "confirmed", "rejected"):
            counts[st] = (
                CausedByEdge.objects.filter(status=st).count()
                + ImplementsEdge.objects.filter(status=st).count()
                + RootCause.objects.filter(status=st).count()
            )
        return Response(counts)


class ReviewQueueView(APIView):
    """
    GET the provenance-bearing items for a given `status` (default "inferred").
    Pass ?status=inferred|confirmed|rejected to see the pending worklist, or the
    already-reviewed history.
    """

    @extend_schema(responses=ReviewQueueSerializer)
    def get(self, request):
        status_param = request.query_params.get("status", "inferred")
        if status_param not in {"inferred", "confirmed", "rejected"}:
            status_param = "inferred"
        flt = {"status": status_param}
        return Response(
            {
                "status": status_param,
                "caused_by_edges": CausedByReviewSerializer(
                    CausedByEdge.objects.filter(**flt)
                    .select_related("incident", "code_change"),
                    many=True,
                ).data,
                "implements_edges": ImplementsReviewSerializer(
                    ImplementsEdge.objects.filter(**flt)
                    .select_related("code_change", "decision"),
                    many=True,
                ).data,
                "root_causes": RootCauseReviewSerializer(
                    RootCause.objects.filter(**flt).select_related("incident"),
                    many=True,
                ).data,
            }
        )
