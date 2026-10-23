from django.contrib import admin

from .models import (
    AffectsEdge,
    CausedByEdge,
    CodeChange,
    CodeEntity,
    Decision,
    ImplementsEdge,
    Incident,
    Repository,
    RootCause,
)


@admin.register(Repository)
class RepositoryAdmin(admin.ModelAdmin):
    list_display = ("full_name", "provider", "default_branch", "url")
    search_fields = ("full_name", "url")
    list_filter = ("provider",)


@admin.register(Decision)
class DecisionAdmin(admin.ModelAdmin):
    list_display = ("key", "title", "status", "repository", "supersedes")
    search_fields = ("key", "title")
    list_filter = ("status", "repository")


@admin.register(CodeChange)
class CodeChangeAdmin(admin.ModelAdmin):
    list_display = ("kind", "identifier", "title", "repository", "authored_at")
    search_fields = ("identifier", "title", "author_email")
    list_filter = ("kind", "provider", "repository")


@admin.register(CodeEntity)
class CodeEntityAdmin(admin.ModelAdmin):
    list_display = ("kind", "path", "name", "repository")
    search_fields = ("path", "name")
    list_filter = ("kind", "repository")


class RootCauseInline(admin.TabularInline):
    model = RootCause
    extra = 0


@admin.register(Incident)
class IncidentAdmin(admin.ModelAdmin):
    list_display = ("external_id", "title", "provider", "status", "repository")
    search_fields = ("external_id", "title")
    list_filter = ("status", "provider", "repository")
    inlines = [RootCauseInline]


@admin.register(RootCause)
class RootCauseAdmin(admin.ModelAdmin):
    list_display = ("summary", "incident", "source", "confidence", "status")
    list_filter = ("source", "status", "category")


@admin.register(ImplementsEdge)
class ImplementsEdgeAdmin(admin.ModelAdmin):
    list_display = ("code_change", "decision", "source", "confidence", "status")
    list_filter = ("source", "status")


@admin.register(CausedByEdge)
class CausedByEdgeAdmin(admin.ModelAdmin):
    list_display = ("incident", "code_change", "source", "confidence", "status")
    list_filter = ("source", "status")


@admin.register(AffectsEdge)
class AffectsEdgeAdmin(admin.ModelAdmin):
    list_display = ("code_change", "code_entity", "change_type")
    list_filter = ("change_type",)
