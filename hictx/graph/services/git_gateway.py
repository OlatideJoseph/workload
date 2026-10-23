"""
Thin gateway over the `git` CLI, scoped to one local clone.

All git I/O in Phase 2 goes through here so the linker can be unit-tested
against a throwaway real repo (and, later, mocked). We keep a persistent clone
per repository precisely so `git blame` is cheap and offline (see CLAUDE.md).
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass

_SHA_LINE_RE = re.compile(r"^([0-9a-f]{40}) \d+ (\d+)")


class GitError(RuntimeError):
    """A git command failed or the requested revision does not exist."""


@dataclass(frozen=True)
class CommitMeta:
    sha: str
    author_name: str
    author_email: str
    authored_at: str  # ISO 8601
    subject: str
    body: str = ""


def _force_rmtree(path: str) -> None:
    """Remove a directory tree, clearing read-only bits (Windows .git files)."""
    def on_error(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    if os.path.exists(path):
        shutil.rmtree(path, onerror=on_error)


def clone_or_fetch(url: str, dest: str) -> None:
    """
    Ensure a persistent local clone of `url` exists at `dest`.

    First run clones (anonymous HTTPS works for public repos — no token needed);
    later runs `git fetch` to pick up new history. This is what makes offline
    blame/log possible without fighting API rate limits (CLAUDE.md).
    """
    # Never block on an interactive credential prompt (e.g. a private repo with
    # no token) — fail fast so the error surfaces to the session instead of the
    # background thread hanging forever.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}

    def _clone_fresh():
        # git clone refuses a non-empty destination, so the stale clone must be
        # gone first. On Windows shutil.rmtree trips over read-only .git files;
        # clear the read-only bit and retry, then verify the path is actually gone.
        _force_rmtree(dest)
        if os.path.exists(dest) and os.listdir(dest):
            raise GitError(
                f"could not clear stale clone at {dest!r} (files may be locked by "
                "another process); remove it and retry"
            )
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        subprocess.run(
            ["git", "clone", url, dest],
            capture_output=True, text=True, check=True, env=env,
        )

    try:
        if os.path.isdir(os.path.join(dest, ".git")):
            # A previous clone that failed partway leaves a stub with no
            # resolvable HEAD; fetching into it later dies with
            # "ambiguous argument 'HEAD'". Only fetch a healthy clone —
            # otherwise start over.
            healthy = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
                cwd=dest, capture_output=True, text=True, env=env,
            ).returncode == 0
            if healthy:
                subprocess.run(
                    ["git", "fetch", "--all", "--prune"],
                    cwd=dest, capture_output=True, text=True, check=True, env=env,
                )
            else:
                _clone_fresh()
        else:
            _clone_fresh()
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise GitError(f"clone/fetch of {url!r} failed: {detail}") from exc


class GitGateway:
    def __init__(self, repo_path: str):
        self.repo_path = repo_path

    # -- low-level ---------------------------------------------------------

    def _run(self, *args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:  # git missing, or clone_path stale/not a directory
            raise GitError(f"cannot run git in {self.repo_path!r}: {exc}") from exc
        if result.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} failed: {result.stderr.strip()}"
            )
        return result.stdout

    # -- queries -----------------------------------------------------------

    def resolve(self, rev: str) -> str:
        """Resolve a revision (sha, branch, tag) to a full 40-char sha."""
        return self._run("rev-parse", "--verify", f"{rev}^{{commit}}").strip()

    def parent(self, rev: str) -> str | None:
        """First parent of a commit, or None for a root commit."""
        out = self._run("rev-list", "--parents", "-n", "1", rev).split()
        # Output: "<sha> [<parent1> <parent2> ...]"
        return out[1] if len(out) > 1 else None

    def commit_diff(self, rev: str) -> str:
        """Unified diff introduced by a commit (message header stripped)."""
        return self._run("show", "--no-color", "--format=", "-M", rev)

    def commit_meta(self, rev: str) -> CommitMeta:
        # Newline-delimited: the leading fields are single-line, and the body
        # (%b, possibly multi-line) comes last so a bounded split keeps it whole.
        # Avoids a NUL byte in argv (rejected by Windows CreateProcess).
        fmt = "%H%n%an%n%ae%n%aI%n%s%n%b"
        out = self._run("show", "-s", f"--format={fmt}", rev)
        parts = out.split("\n", 5)
        while len(parts) < 6:
            parts.append("")
        sha, an, ae, ai, subject, body = parts[:6]
        return CommitMeta(sha, an, ae, ai, subject, body.rstrip("\n"))

    def file_at(self, rev: str, path: str) -> str:
        """Contents of a file at a revision (empty string if it doesn't exist)."""
        try:
            return self._run("show", f"{rev}:{path}")
        except GitError:
            return ""

    def list_files(self, rev: str = "HEAD") -> list[str]:
        """Repo-relative paths tracked at `rev` (empty on an unborn HEAD)."""
        try:
            out = self._run("ls-tree", "-r", "--name-only", rev)
        except GitError:
            return []
        return [line for line in out.splitlines() if line]

    def iter_commit_shas(self, rev: str = "HEAD", limit: int | None = None) -> list[str]:
        """
        Commit shas reachable from `rev`, newest first (for backfill).

        An empty repository has an unborn HEAD (`git log HEAD` errors); treat
        that as simply zero commits rather than failing the whole ingest.
        """
        args = ["log", "--format=%H"]
        if limit:
            args += ["-n", str(limit)]
        args.append(rev)
        try:
            out = self._run(*args)
        except GitError:
            return []
        return [line for line in out.splitlines() if line]

    def name_status(self, rev: str) -> list[tuple[str, str]]:
        """
        Files a commit touched, as (change_type, path) using AffectsEdge's
        vocabulary. Renames report the new path.
        """
        out = self._run("show", "--name-status", "--format=", "-M", rev)
        mapping = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed"}
        changes: list[tuple[str, str]] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            fields = line.split("\t")
            code = fields[0][:1]
            path = fields[-1]  # for renames (R\told\tnew) this is the new path
            changes.append((mapping.get(code, "modified"), path))
        return changes

    def blame_file(self, rev: str, path: str) -> dict[int, str]:
        """
        Map every line number in `path` at revision `rev` to the sha that last
        touched it. One blame call per file; callers index the lines they need.
        """
        try:
            out = self._run("blame", "--porcelain", rev, "--", path)
        except GitError:
            return {}
        line_to_sha: dict[int, str] = {}
        current_sha: str | None = None
        final_line: int | None = None
        for line in out.splitlines():
            m = _SHA_LINE_RE.match(line)
            if m:
                current_sha = m.group(1)
                final_line = int(m.group(2))
            elif line.startswith("\t") and current_sha and final_line is not None:
                line_to_sha[final_line] = current_sha
        return line_to_sha
