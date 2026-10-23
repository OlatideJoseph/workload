"""DRF serializers exposing the context graph read-only for now."""

from rest_framework import serializers

from .models import (
    CausedByEdge,
    CodeChange,
    CodeEntity,
    Decision,
    ImplementsEdge,
    Incident,
    Repository,
    RootCause,
    Session,
)


class RepositorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Repository
        fields = "__all__"


class DecisionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Decision
        fields = "__all__"


class CodeEntitySerializer(serializers.ModelSerializer):
    class Meta:
        model = CodeEntity
        fields = "__all__"


class CodeChangeSerializer(serializers.ModelSerializer):
    class Meta:
        model = CodeChange
        fields = "__all__"


class RootCauseSerializer(serializers.ModelSerializer):
    class Meta:
        model = RootCause
        fields = "__all__"


class IncidentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Incident
        fields = "__all__"


# --- Edge serializers (expose provenance for the confirm/reject UI) ---------

class ImplementsEdgeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImplementsEdge
        fields = "__all__"


class CausedByEdgeSerializer(serializers.ModelSerializer):
    class Meta:
        model = CausedByEdge
        fields = "__all__"


# --- Subgraph: the migration-facing view for one incident -------------------

class IncidentSubgraphSerializer(serializers.ModelSerializer):
    """
    The connected subgraph for one incident:
        Incident -> RootCauses
        Incident -> caused_by CodeChanges -> implemented Decisions
    This is the shape Phase 4 retrieval feeds a migration alongside source.
    """

    root_causes = RootCauseSerializer(many=True, read_only=True)
    causes = serializers.SerializerMethodField()

    class Meta:
        model = Incident
        fields = (
            "id",
            "provider",
            "external_id",
            "title",
            "description",
            "status",
            "root_causes",
            "causes",
        )

    def get_causes(self, incident):
        out = []
        edges = (
            CausedByEdge.objects.filter(incident=incident)
            .select_related("code_change")
            .prefetch_related("code_change__implements")
        )
        for edge in edges:
            change = edge.code_change
            out.append(
                {
                    "confidence": edge.confidence,
                    "source": edge.source,
                    "status": edge.status,
                    "code_change": CodeChangeSerializer(change).data,
                    "decisions": DecisionSerializer(
                        change.implements.all(), many=True
                    ).data,
                }
            )
        return out


# --- Phase 3: capture ("why?" at PR-open) -----------------------------------

class PRWhyCaptureSerializer(serializers.Serializer):
    """Input for the lightweight PR-open capture prompt."""

    repository = serializers.CharField(help_text="Repository full_name (owner/name).")
    change_identifier = serializers.CharField(help_text="PR number or commit sha.")
    why = serializers.CharField(help_text="One sentence: why does this change exist?")
    change_kind = serializers.ChoiceField(
        choices=CodeChange._meta.get_field("kind").choices,
        default="pull_request",
    )
    decision_key = serializers.CharField(required=False, allow_blank=True, default="")
    decision_title = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_repository(self, value):
        repo = Repository.objects.filter(full_name=value).first()
        if repo is None:
            raise serializers.ValidationError(f"No repository named {value!r}.")
        return repo


class CaptureResultSerializer(serializers.Serializer):
    """What capture produced, for the caller to display/confirm."""

    change = CodeChangeSerializer()
    decision = DecisionSerializer()
    edge = ImplementsEdgeSerializer()
    created_decision = serializers.BooleanField()


# --- Sessions: connect a repo -----------------------------------------------

class SessionCreateSerializer(serializers.Serializer):
    """Input for 'create a session for a repo'."""

    url = serializers.CharField(help_text="Repo URL (or local path) to connect.")
    full_name = serializers.CharField(required=False, allow_blank=True, default="")
    token = serializers.CharField(
        required=False, allow_blank=True, default="",
        style={"input_type": "password"},
        help_text="GitHub token — optional for public repos, needed for issues/private.",
    )
    limit = serializers.IntegerField(required=False, allow_null=True, default=None)
    ingest_issues = serializers.BooleanField(required=False, default=True)


class SessionSerializer(serializers.ModelSerializer):
    """The tracked state of a connect-a-repo run — the shape the UI polls."""

    repository = RepositorySerializer(read_only=True)

    class Meta:
        model = Session
        fields = [
            "id", "url", "full_name", "ingest_issues", "limit",
            "status", "phase", "commits_total", "commits_done",
            "incidents_done", "error", "repository",
            "created_at", "updated_at",
        ]


class SessionRetrySerializer(serializers.Serializer):
    """Optional token when retrying (private repos / issues need one)."""

    token = serializers.CharField(
        required=False, allow_blank=True, default="",
        style={"input_type": "password"},
    )


# --- Review queue: inferred items with human-readable labels ----------------

class CausedByReviewSerializer(serializers.ModelSerializer):
    resource = serializers.CharField(read_only=True, default="caused-by-edges")
    change_id = serializers.IntegerField(source="code_change_id", read_only=True)
    incident_title = serializers.CharField(source="incident.title", read_only=True)
    incident_external_id = serializers.CharField(
        source="incident.external_id", read_only=True
    )
    change_identifier = serializers.CharField(
        source="code_change.identifier", read_only=True
    )
    change_title = serializers.CharField(source="code_change.title", read_only=True)

    class Meta:
        model = CausedByEdge
        fields = ["id", "resource", "source", "confidence", "status", "evidence",
                  "incident_title", "incident_external_id",
                  "change_id", "change_identifier", "change_title"]


class ImplementsReviewSerializer(serializers.ModelSerializer):
    resource = serializers.CharField(read_only=True, default="implements-edges")
    change_id = serializers.IntegerField(source="code_change_id", read_only=True)
    change_identifier = serializers.CharField(
        source="code_change.identifier", read_only=True
    )
    change_title = serializers.CharField(source="code_change.title", read_only=True)
    decision_key = serializers.CharField(source="decision.key", read_only=True)
    decision_title = serializers.CharField(source="decision.title", read_only=True)

    class Meta:
        model = ImplementsEdge
        fields = ["id", "resource", "source", "confidence", "status", "evidence",
                  "change_id", "change_identifier", "change_title",
                  "decision_key", "decision_title"]


class RootCauseReviewSerializer(serializers.ModelSerializer):
    resource = serializers.CharField(read_only=True, default="root-causes")
    incident_title = serializers.CharField(source="incident.title", read_only=True)
    incident_external_id = serializers.CharField(
        source="incident.external_id", read_only=True
    )

    class Meta:
        model = RootCause
        fields = ["id", "resource", "source", "confidence", "status",
                  "summary", "category", "detail",
                  "incident_title", "incident_external_id"]


class ReviewQueueSerializer(serializers.Serializer):
    """The provenance-bearing items for one status (inferred/confirmed/rejected)."""

    status = serializers.CharField()
    caused_by_edges = CausedByReviewSerializer(many=True)
    implements_edges = ImplementsReviewSerializer(many=True)
    root_causes = RootCauseReviewSerializer(many=True)


class ReviewCountsSerializer(serializers.Serializer):
    """Per-status totals across all provenance-bearing types, for the sub-tab badges."""

    inferred = serializers.IntegerField()
    confirmed = serializers.IntegerField()
    rejected = serializers.IntegerField()


class LinkDraftResultSerializer(serializers.Serializer):
    """What a link-drafting run produced, for the UI."""

    client = serializers.CharField(required=False)
    changes_processed = serializers.IntegerField()
    links_drafted = serializers.IntegerField()
    links_refreshed = serializers.IntegerField()
    skipped_human = serializers.IntegerField()
    notes = serializers.ListField(child=serializers.CharField())


class DraftLinksRequestSerializer(serializers.Serializer):
    """Options for triggering a draft-links run from the UI."""

    use_llm = serializers.BooleanField(required=False, allow_null=True, default=None)
    use_diff = serializers.BooleanField(required=False, default=False)
    limit = serializers.IntegerField(required=False, allow_null=True, default=None)


class LLMProviderSerializer(serializers.Serializer):
    """Non-secret status of one provider."""

    id = serializers.CharField()
    label = serializers.CharField()
    model = serializers.CharField(allow_blank=True)
    configured = serializers.BooleanField()
    source = serializers.CharField(help_text="env | stored | none")
    hint = serializers.CharField(allow_blank=True, help_text="last 4 chars, never the key")


class LLMSettingsSerializer(serializers.Serializer):
    """The selected provider and per-provider status, for the Settings UI."""

    provider = serializers.CharField(help_text="selected provider id")
    model = serializers.CharField(help_text="selected provider's model")
    configured = serializers.BooleanField(help_text="is the selected provider ready")
    providers = LLMProviderSerializer(many=True)


class LLMConfigSerializer(serializers.Serializer):
    """Select a provider and (optionally) set its key/model. Key is write-only."""

    provider = serializers.CharField()
    key = serializers.CharField(
        required=False, allow_blank=True, write_only=True,
        style={"input_type": "password"}, trim_whitespace=True,
    )
    model = serializers.CharField(required=False, allow_blank=True)


class ChangeDiffSerializer(serializers.Serializer):
    """A code change's unified diff, read from the local clone."""

    change_id = serializers.IntegerField()
    identifier = serializers.CharField()
    kind = serializers.CharField()
    sha = serializers.CharField(allow_blank=True)
    available = serializers.BooleanField()
    diff = serializers.CharField(allow_blank=True)
    truncated = serializers.BooleanField()
    reason = serializers.CharField(allow_blank=True)


# --- Graph view (frontend preview) ------------------------------------------

class GraphNodeSerializer(serializers.Serializer):
    id = serializers.CharField()
    type = serializers.CharField(help_text="incident | change | decision | root_cause | entity")
    label = serializers.CharField()


class GraphEdgeSerializer(serializers.Serializer):
    source = serializers.CharField()
    target = serializers.CharField()
    type = serializers.CharField(help_text="caused_by | implements | explains | affects")
    status = serializers.CharField(help_text="confirmed | inferred | rejected | factual")
    confidence = serializers.FloatField()


class RepositoryGraphSerializer(serializers.Serializer):
    """The connected knowledge core of a repository, for the graph preview."""

    nodes = GraphNodeSerializer(many=True)
    edges = GraphEdgeSerializer(many=True)


class TimelineEventSerializer(serializers.Serializer):
    kind = serializers.CharField(help_text="decision | incident | change")
    id = serializers.CharField()
    date = serializers.CharField(allow_null=True)
    title = serializers.CharField()
    subtitle = serializers.CharField(allow_blank=True)
    detail = serializers.CharField(allow_blank=True)
    links = serializers.ListField(child=serializers.CharField())


class RepositoryTimelineSerializer(serializers.Serializer):
    """The repo's notable events in time order — the story view."""

    repository = serializers.CharField()
    count = serializers.IntegerField()
    span = serializers.DictField()
    events = TimelineEventSerializer(many=True)


class StoryNarrativeSerializer(serializers.Serializer):
    """The LLM-written narrative (cached), plus whether a key is set."""

    narrative = serializers.CharField(allow_null=True)
    generated_at = serializers.CharField(allow_null=True)
    model = serializers.CharField()
    configured = serializers.BooleanField()


class EntityTimelineSerializer(serializers.Serializer):
    """A single file's notable events in time order — the per-file story."""

    entity = serializers.CharField()
    count = serializers.IntegerField()
    span = serializers.DictField()
    events = TimelineEventSerializer(many=True)


class StoryFileSerializer(serializers.Serializer):
    """A file worth telling a story about (has connected context)."""

    id = serializers.IntegerField()
    path = serializers.CharField()
    name = serializers.CharField(allow_blank=True)
    context_count = serializers.IntegerField(
        help_text="distinct decisions/incidents reachable through this file"
    )
