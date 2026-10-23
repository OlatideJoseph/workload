"""
hictx MCP server — exposes the captured history context as tools any AI coding
agent (Claude Code, Cursor, …) can call while it works.

This is the relevance layer: instead of being a dashboard someone has to open,
hictx becomes a tool the agent queries on demand — "before you touch this file,
here's the decision behind it and the incident a change to it once caused." It
reuses the Phase-4 retrieval and the timeline; the tools just render them as text
an agent can drop straight into its context.

Run it with `python manage.py mcp_serve` (stdio transport).

The `*_text` functions are plain and unit-tested; the `@mcp.tool()` wrappers are
the thin MCP surface.
"""

from __future__ import annotations

from asgiref.sync import sync_to_async
from mcp.server.fastmcp import FastMCP

from ..models import CodeEntity, Repository
from .retrieval import render_context, retrieve_entity_subgraph
from .timeline import file_timeline, repository_timeline

mcp = FastMCP("hictx")


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------

def _resolve_repo(name: str) -> Repository | None:
    name = (name or "").strip()
    if not name:
        return None
    return (
        Repository.objects.filter(full_name=name).first()
        or Repository.objects.filter(full_name__icontains=name).first()
    )


def _resolve_entity(repo: Repository, path: str) -> CodeEntity | None:
    path = (path or "").strip().replace("\\", "/")
    if not path:
        return None
    return (
        CodeEntity.objects.filter(repository=repo, path=path).first()
        or CodeEntity.objects.filter(repository=repo, path__iendswith=path).first()
        or CodeEntity.objects.filter(repository=repo, path__icontains=path).first()
    )


# ---------------------------------------------------------------------------
# Tool bodies (plain, testable)
# ---------------------------------------------------------------------------

def list_repositories_text() -> str:
    repos = list(Repository.objects.order_by("full_name"))
    if not repos:
        return "No repositories are connected to hictx yet."
    return "Repositories hictx has captured:\n" + "\n".join(
        f"- {r.full_name}" for r in repos
    )


def file_context_text(repository: str, path: str) -> str:
    repo = _resolve_repo(repository)
    if not repo:
        return (f"No repository matching {repository!r}. "
                "Call list_repositories to see what's connected.")
    entity = _resolve_entity(repo, path)
    if not entity:
        return (f"No file matching {path!r} in {repo.full_name}. "
                "Call find_files to locate it.")
    sub = retrieve_entity_subgraph(entity, include_inferred=True, min_confidence=0.0)
    if sub.is_empty():
        return (f"No captured context for {entity.path} in {repo.full_name} yet — "
                "no decisions or incidents are linked to the changes that touched it.")
    return render_context(sub)


def file_story_text(repository: str, path: str, limit: int = 40) -> str:
    repo = _resolve_repo(repository)
    if not repo:
        return f"No repository matching {repository!r}. Call list_repositories."
    entity = _resolve_entity(repo, path)
    if not entity:
        return f"No file matching {path!r} in {repo.full_name}. Call find_files."
    return _render_timeline(f"{repo.full_name} · {entity.path}",
                            file_timeline(entity, limit=limit))


def repo_story_text(repository: str, limit: int = 40) -> str:
    repo = _resolve_repo(repository)
    if not repo:
        return f"No repository matching {repository!r}. Call list_repositories."
    return _render_timeline(repo.full_name, repository_timeline(repo, limit=limit))


def _render_timeline(subject: str, t: dict) -> str:
    if not t["events"]:
        return f"No notable history captured for {subject} yet."
    header = (f"Story of {subject} "
              f"({(t['span']['from'] or '')[:10]} → {(t['span']['to'] or '')[:10]}, "
              f"{t['count']} notable events):")
    lines = [header, ""]
    for e in t["events"]:
        date = (e["date"] or "undated")[:10]
        links = f"  [{', '.join(e['links'])}]" if e["links"] else ""
        detail = f" — {e['detail'][:120]}" if e["detail"] else ""
        lines.append(f"{date} · {e['kind']}: {e['title']}{links}{detail}")
    return "\n".join(lines)


def find_files_text(repository: str, query: str) -> str:
    repo = _resolve_repo(repository)
    if not repo:
        return f"No repository matching {repository!r}. Call list_repositories."
    q = (query or "").strip()
    qs = CodeEntity.objects.filter(repository=repo)
    if q:
        qs = qs.filter(path__icontains=q)
    paths = list(qs.order_by("path").values_list("path", flat=True)[:40])
    if not paths:
        return f"No files matching {query!r} in {repo.full_name}."
    return (f"Files in {repo.full_name} matching {query!r}:\n"
            + "\n".join(f"- {p}" for p in paths))


# ---------------------------------------------------------------------------
# MCP tool surface
# ---------------------------------------------------------------------------

# Tools are async and run the (sync) ORM bodies via sync_to_async, because
# FastMCP executes tools inside an event loop and Django forbids sync ORM there.

@mcp.tool()
async def list_repositories() -> str:
    """List the repositories whose history hictx has captured."""
    return await sync_to_async(list_repositories_text)()


@mcp.tool()
async def get_file_context(repository: str, path: str) -> str:
    """The captured "why" behind a file: the architecture decisions it implements,
    the past incidents a change to it caused, and their root causes.

    Call this BEFORE modifying, reviewing, or porting a file, so you preserve its
    load-bearing, non-obvious behavior instead of silently dropping it. `repository`
    is "owner/name"; `path` is the repo-relative file path.
    """
    return await sync_to_async(file_context_text)(repository, path)


@mcp.tool()
async def get_file_story(repository: str, path: str) -> str:
    """A chronological history of one file: the changes that shaped it and the
    decisions/incidents behind them. Explains why the file looks the way it does."""
    return await sync_to_async(file_story_text)(repository, path)


@mcp.tool()
async def get_repo_story(repository: str) -> str:
    """A chronological summary of a repository's notable decisions and incidents —
    how and why the codebase evolved. Good for onboarding or high-level context."""
    return await sync_to_async(repo_story_text)(repository)


@mcp.tool()
async def find_files(repository: str, query: str) -> str:
    """Find files in a repository whose path contains a query — use it to resolve a
    path before calling get_file_context or get_file_story."""
    return await sync_to_async(find_files_text)(repository, query)
