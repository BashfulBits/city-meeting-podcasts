"""Execute a remedy publication plan: one isolated branch per PR (review/51 P4, live stage).

``citypods.remedy_queue.plan_publications`` decides *what*; this module does it without letting one
unit of work touch another:

* every PR is built in **its own git worktree**, created from ``origin/main`` *before* its change is
  applied, never as an aggregate edit of the working tree followed by a branch switch;
* only the files the mutation reports it changed (the **manifest**) are staged, and they must all
  be feed configuration; there is no broad ``git clean`` and no rollback beyond the unit's own
  worktree, so a failure in one unit cannot delete another unit's files;
* the repository's own gate (``verify_remedy_mutations``) runs on that exact tree and the manifest
  hash is recomputed after it, immediately before the commit that is pushed;
* the body carries stable decision markers and per-decision bindings, so the PR is recognized when
  it is merged, closed or reopened, and a repeat dispatch updates it instead of opening another.

Git and GitHub sit behind small interfaces so the whole path is tested against a real temporary
repository with a local bare origin and a fake GitHub.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from citypods.remedy_queue import PlannedPR, render_binding, render_markers

FEED_DIR = "config/feeds"
GENERATED_START = "<!-- citypods:generated:start -->"
GENERATED_END = "<!-- citypods:generated:end -->"
BOT_NAME = "citypods-remedy[bot]"
BOT_EMAIL = "citypods-remedy@users.noreply.github.com"
TOKEN_NOTE = (
    "GitHub Actions does not automatically run checks for PRs created with `GITHUB_TOKEN`; a "
    "maintainer must dispatch the required checks against this branch before merge. Human "
    "review and merge are required."
)


class PublicationError(RuntimeError):
    """A unit could not be published; its worktree and branch were removed."""


class GitHub(Protocol):
    def find_pr(self, branch: str) -> list[dict[str, Any]]: ...

    def create_pr(self, branch: str, title: str, body: str) -> str: ...

    def edit_body(self, kind: str, number: int, body: str) -> None: ...

    def reopen(self, kind: str, number: int) -> None: ...

    def create_issue(self, title: str, body: str) -> str: ...


class Git:
    """The few git operations a unit needs, each run in an explicit directory."""

    def __init__(self, root: Path, *, runner: Callable[..., Any] = subprocess.run):
        self.root = Path(root)
        self._run = runner

    def run(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        done = self._run(
            ["git", *args], cwd=cwd or self.root, capture_output=True, text=True, check=False
        )
        if check and done.returncode != 0:
            raise PublicationError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
        return done.stdout

    def head_of(self, ref: str) -> str:
        return self.run("rev-parse", "--verify", ref).strip()

    def worktree_add(self, path: Path, branch: str, base: str) -> None:
        self.run("worktree", "add", "-b", branch, str(path), base)

    def worktree_remove(self, path: Path, branch: str) -> None:
        self.run("worktree", "remove", "--force", str(path), check=False)
        self.run("branch", "-D", branch, check=False)


@dataclass(frozen=True)
class UnitDetail:
    """What a PR body says about one decision, from the P3 report and its evidence."""

    decision_id: str
    binding: Mapping[str, Any]
    finding_id: str
    evidence_hash: str
    action: str
    target_feeds: Sequence[str]
    new_feed_slug: str = ""
    routes: Sequence[Mapping[str, Any]] = ()
    adjudication: str = ""
    recording_count: int = 0


@dataclass(frozen=True)
class Published:
    branch: str
    url: str
    decision_ids: tuple[str, ...]
    manifest: tuple[str, ...]
    manifest_hash: str
    created: bool


def manifest_hash(root: Path, manifest: Sequence[str]) -> str:
    """A digest of the changed files' contents, so the body names exactly what was pushed."""
    digest = hashlib.sha256()
    for rel in sorted(manifest):
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update((root / rel).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def check_manifest(root: Path, changed: Sequence[Path]) -> list[str]:
    """The relative, feed-only paths a unit may stage; anything else refuses the unit."""
    rels: list[str] = []
    base = root.resolve()
    for path in changed:
        resolved = Path(path).resolve()
        try:
            rel = resolved.relative_to(base).as_posix()
        except ValueError as exc:
            raise PublicationError(f"{path} is outside the unit's worktree") from exc
        if not rel.startswith(f"{FEED_DIR}/") or not rel.endswith(".yml"):
            raise PublicationError(f"{rel} is not feed configuration")
        rels.append(rel)
    return sorted(set(rels))


def render_pr_body(
    unit: PlannedPR,
    details: Sequence[UnitDetail],
    *,
    manifest: Sequence[str],
    files_hash: str,
    verification: str,
) -> str:
    """The generated body: evidence, identity, models, scope and the limits of the check."""
    lines = [
        GENERATED_START,
        f"Validated proposal for **{unit.city}** ({len(details)} decision(s), feed family "
        f"`{unit.family}`, outcome `{unit.outcome}`). Feed changes were generated through the "
        "existing remedy mutation API, revalidated against current evidence, and passed the "
        f"repository gate on this exact tree ({verification}).",
        "",
        "## Decisions",
    ]
    for item in sorted(details, key=lambda d: d.decision_id):
        binding = item.binding
        target = (
            f"new feed `{item.new_feed_slug}`"
            if item.action == "new_feed"
            else ", ".join(f"`{slug}`" for slug in sorted(item.target_feeds)) or "none"
        )
        lines += [
            f"### `{item.decision_id[:12]}` · {binding['normalized_label']}",
            f"- Action: `{item.action}` → {target}",
            f"- Source: `{binding['source_key']}` · policy `{binding['policy_id']}`",
            f"- Evidence SHA-256 `{item.evidence_hash}` · policy `{binding['policy_hash']}` · "
            f"config `{binding['config_hash']}`",
            f"- Recordings affected: {item.recording_count}",
        ]
        for route in item.routes:
            lines.append(
                f"- Model: `{route.get('route_id')}` ({route.get('upstream_model')}), "
                f"reasoning `{route.get('reasoning_level') or 'provider default'}`"
            )
        if item.adjudication:
            lines.append(f"- Adjudication: {item.adjudication}")
        lines.append(f"- Finding: `{item.finding_id}`")
    lines += [
        "",
        "## Scope",
        f"Files changed ({len(manifest)}), SHA-256 of their contents `{files_hash}`:",
        *(f"- `{rel}`" for rel in manifest),
        "",
        "## Limits",
        "- No negative replay was run for this proposal; the deterministic validator and the "
        "repository gate were.",
        "- Unresolved findings are filed as separate issues, not hidden here.",
        f"- {TOKEN_NOTE}",
        GENERATED_END,
        "",
        render_markers(item.decision_id for item in details),
        *(render_binding(item.decision_id, item.binding) for item in details),
    ]
    return "\n".join(lines) + "\n"


def replace_generated(prior: str, generated: str) -> str:
    """Swap the generated section of an existing body, keeping a person's notes around it."""
    if GENERATED_START in prior and GENERATED_END in prior:
        head, rest = prior.split(GENERATED_START, 1)
        _old, tail = rest.split(GENERATED_END, 1)
        return head + generated + tail
    return generated + "\n" + prior


def publish_pr(
    unit: PlannedPR,
    details: Sequence[UnitDetail],
    *,
    git: Git,
    github: GitHub,
    work_root: Path,
    base: str,
    apply: Callable[[Path], Sequence[Path]],
    verify: Callable[[Path], tuple[bool, str]],
) -> Published:
    """Open one PR in an isolated worktree, or return the existing one for the same branch.

    ``apply(worktree)`` writes the unit's change inside the worktree and returns the paths it
    changed; ``verify(worktree)`` runs the repository gate there. Any failure removes the worktree
    and branch and leaves everything else untouched."""
    existing = github.find_pr(unit.branch)
    if len(existing) > 1:
        raise PublicationError(f"duplicate PRs for {unit.branch}")
    if existing:
        return Published(unit.branch, existing[0]["url"], unit.decision_ids, (), "", False)
    worktree = Path(work_root) / unit.branch.replace("/", "__")
    if worktree.exists():
        shutil.rmtree(worktree)  # a leftover of a crashed run, outside the main checkout
        git.run("worktree", "prune", check=False)
    try:
        git.worktree_add(worktree, unit.branch, base)
        manifest = check_manifest(worktree, apply(worktree))
        if not manifest:
            raise PublicationError("the change produced no feed changes")
        ok, message = verify(worktree)
        if not ok:
            raise PublicationError(f"verification failed: {message[:400]}")
        # Recomputed on the exact tree that is committed and pushed, after verification.
        files_hash = manifest_hash(worktree, manifest)
        git.run("add", "--", *manifest, cwd=worktree)
        if not git.run("diff", "--cached", "--name-only", cwd=worktree).strip():
            raise PublicationError("nothing was staged")
        staged = sorted(git.run("diff", "--cached", "--name-only", cwd=worktree).split())
        if staged != manifest:
            raise PublicationError("the staged files differ from the manifest")
        git.run(
            "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}",
            "commit", "-m", f"fix(config): propose validated remedy ({len(details)} decision(s))",
            cwd=worktree,
        )  # fmt: skip
        # The branch name is derived from this unit's decision ids and owned by this publisher. A
        # remote copy with no PR is the orphan of a run that crashed between its push and creating
        # the PR (no PR exists for the branch in any state, checked above); it is replaced, but
        # only if it is still the commit observed now, so nothing else can be overwritten.
        remote = git.run(
            "ls-remote", "--heads", "origin", f"refs/heads/{unit.branch}", cwd=worktree
        ).split()
        observed = remote[0] if remote else ""
        git.run(
            "push",
            "-u",
            f"--force-with-lease=refs/heads/{unit.branch}:{observed}",
            "origin",
            unit.branch,
            cwd=worktree,
        )
        body = render_pr_body(
            unit, details, manifest=manifest, files_hash=files_hash, verification="ruff, pytest"
        )
        title = f"fix(config): remedy {unit.city} {unit.family} ({len(details)})"
        url = github.create_pr(unit.branch, title, body)
        return Published(unit.branch, url, unit.decision_ids, tuple(manifest), files_hash, True)
    finally:
        git.worktree_remove(worktree, unit.branch)


def refinement_issue_body(
    decision_id: str, binding: Mapping[str, Any], *, rejected_pr: str, evidence_hash: str
) -> str:
    """The issue that answers a rejected PR: it preserves the evidence and the decision asked."""
    return (
        "\n".join(
            [
                GENERATED_START,
                f"## Refinement needed: {binding['normalized_label']} ({binding['city']})",
                "",
                f"The pull request {rejected_pr} for this decision was closed without merging, "
                "so it will not be proposed again on the same evidence.",
                "",
                "**Decision requested.** The assignment the closed pull request proposed; its "
                "description holds the evidence links, models and changed files.",
                f"- Decision `{decision_id}`",
                f"- Source `{binding['source_key']}` · evidence SHA-256 `{evidence_hash}`",
                f"- Recordings: {len(binding.get('recording_refs') or [])}",
                "",
                "**What to do.** Say here what was wrong or what evidence is missing. When there "
                f"is something new to judge, comment `/remedy reopen {decision_id}`; the next "
                "publication pass will propose it again within the weekly budget.",
                GENERATED_END,
                "",
                render_markers([decision_id]),
                render_binding(decision_id, binding),
            ]
        )
        + "\n"
    )


__all__ = [
    "Git",
    "GitHub",
    "PublicationError",
    "Published",
    "UnitDetail",
    "check_manifest",
    "manifest_hash",
    "publish_pr",
    "refinement_issue_body",
    "render_pr_body",
    "replace_generated",
]
