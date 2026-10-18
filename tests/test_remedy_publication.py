"""review/51 P4 live stage: isolated per-PR worktrees, manifest-limited staging, rollback."""

from __future__ import annotations

import subprocess

import pytest

from citypods import remedy_publication as pub
from citypods.remedy_queue import PlannedPR, branch_name, parse_bindings, parse_markers

DECISION_A = "a" * 64
DECISION_B = "b" * 64


def sh(*args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    """A repository with feed files and a local bare origin."""
    origin = tmp_path / "origin.git"
    sh("git", "init", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    root = tmp_path / "checkout"
    root.mkdir()
    sh("git", "init", "-b", "main", cwd=root)
    sh("git", "config", "user.name", "t", cwd=root)
    sh("git", "config", "user.email", "t@example.test", cwd=root)
    (root / "config" / "feeds").mkdir(parents=True)
    (root / "config" / "feeds" / "council.yml").write_text("slug: council\n")
    (root / "config" / "feeds" / "library.yml").write_text("slug: library\n")
    (root / "notes.txt").write_text("not feed config\n")
    sh("git", "add", "-A", cwd=root)
    sh("git", "commit", "-m", "base", cwd=root)
    sh("git", "remote", "add", "origin", str(origin), cwd=root)
    sh("git", "push", "-u", "origin", "main", cwd=root)
    return root


class FakeGitHub:
    def __init__(self, existing=()):
        self.prs = {
            b: [{"number": 1, "url": f"https://github.com/o/r/pull/{i}", "state": "OPEN"}]
            for i, b in enumerate(existing, 1)
        }
        self.created: list[tuple[str, str, str]] = []

    def find_pr(self, branch):
        return self.prs.get(branch, [])

    def create_pr(self, branch, title, body):
        self.created.append((branch, title, body))
        return f"https://github.com/o/r/pull/{100 + len(self.created)}"

    def edit_body(self, kind, number, body): ...

    def reopen(self, kind, number): ...

    def create_issue(self, title, body):
        return "https://github.com/o/r/issues/1"


def binding(decision):
    return {
        "city": "example-tx",
        "source_key": "source-a",
        "policy_id": "committee",
        "normalized_label": "committee",
        "evidence_hash": "e" * 64,
        "config_hash": "c" * 64,
        "policy_hash": "d" * 64,
        "recording_refs": [{"source_key": "source-a", "uid": "u1", "provider_guid": None}],
    }


def unit(*ids):
    ids = tuple(sorted(ids))
    return PlannedPR(
        branch_name("example-tx", "council", ids), "example-tx", "council", "union", ids
    )


def details(*ids):
    return [
        pub.UnitDetail(
            i,
            binding(i),
            f"finding-{i[:3]}",
            "e" * 64,
            "union",
            ["council"],
            recording_count=2,
            routes=[{"route_id": "r1", "upstream_model": "m1", "reasoning_level": None}],
        )
        for i in ids
    ]


def apply_to(rel, text):
    def apply(worktree):
        path = worktree / rel
        path.write_text(path.read_text() + text)
        return [path]

    return apply


def publish(repo, tmp_path, planned, github=None, apply=None, verify=None, ids=(DECISION_A,)):
    github = github or FakeGitHub()
    result = pub.publish_pr(
        planned,
        details(*ids),
        git=pub.Git(repo),
        github=github,
        work_root=tmp_path / "work",
        base="origin/main",
        apply=apply or apply_to("config/feeds/council.yml", "body_any: [x]\n"),
        verify=verify or (lambda root: (True, "")),
    )
    return result, github


def status(root):
    return sh("git", "status", "--porcelain", cwd=root).strip()


def test_a_unit_is_built_in_its_own_worktree_and_pushed_with_only_its_manifest(repo, tmp_path):
    planned = unit(DECISION_A, DECISION_B)
    result, github = publish(repo, tmp_path, planned, ids=(DECISION_A, DECISION_B))
    assert result.created and result.url.endswith("/pull/101")
    assert result.manifest == ("config/feeds/council.yml",)
    # The branch is on the origin with exactly one commit touching only the feed file.
    names = sh("git", "diff", "--name-only", "main", planned.branch, cwd=repo.parent / "origin.git")
    assert names.split() == ["config/feeds/council.yml"]
    # The main checkout was never touched: no switch, no edits, no leftover branch or worktree.
    assert status(repo) == "" and sh("git", "branch", "--show-current", cwd=repo).strip() == "main"
    assert planned.branch not in sh("git", "branch", cwd=repo)
    assert "work" not in sh("git", "worktree", "list", cwd=repo)
    [(branch, title, body)] = github.created
    assert branch == planned.branch and "example-tx" in title


def test_the_body_carries_markers_bindings_models_scope_and_limits(repo, tmp_path):
    planned = unit(DECISION_A, DECISION_B)
    _result, github = publish(repo, tmp_path, planned, ids=(DECISION_A, DECISION_B))
    body = github.created[0][2]
    assert parse_markers(body) == (DECISION_A, DECISION_B)
    assert set(parse_bindings(body)) == {DECISION_A, DECISION_B}
    for text in (
        "Evidence SHA-256",
        "`r1` (m1)",
        "Recordings affected: 2",
        "config/feeds/council.yml",
        "No negative replay",
        "GITHUB_TOKEN",
        "Human review and merge are required",
    ):
        assert text in body
    assert body.index(pub.GENERATED_START) < body.index("citypods:remedy:v1:") < len(body)


def test_the_manifest_hash_names_exactly_what_was_committed(repo, tmp_path):
    result, github = publish(repo, tmp_path, unit(DECISION_A))
    committed = sh(
        "git", "show", f"{result.branch}:config/feeds/council.yml", cwd=repo.parent / "origin.git"
    )
    expected = pub.hashlib.sha256()
    expected.update(b"config/feeds/council.yml\0" + committed.encode() + b"\0")
    assert (
        result.manifest_hash == expected.hexdigest()
        and result.manifest_hash in github.created[0][2]
    )


def test_an_existing_pr_for_the_branch_is_returned_not_duplicated(repo, tmp_path):
    planned = unit(DECISION_A)
    github = FakeGitHub(existing=[planned.branch])
    result, _ = publish(repo, tmp_path, planned, github=github)
    assert not result.created and github.created == []
    github.prs[planned.branch].append({"number": 2, "url": "u", "state": "CLOSED"})
    with pytest.raises(pub.PublicationError, match="duplicate PRs"):
        publish(repo, tmp_path, planned, github=github)


def test_a_failed_gate_removes_the_worktree_and_branch_and_touches_nothing_else(repo, tmp_path):
    planned = unit(DECISION_A)
    with pytest.raises(pub.PublicationError, match="verification failed: ruff says no"):
        publish(repo, tmp_path, planned, verify=lambda root: (False, "ruff says no"))
    assert planned.branch not in sh("git", "branch", cwd=repo)
    assert sh("git", "ls-remote", "--heads", "origin", cwd=repo).strip().count(planned.branch) == 0
    assert status(repo) == "" and not list((tmp_path / "work").glob("*"))
    # The next unit still publishes: the failure left no state behind.
    ok, github = publish(repo, tmp_path, unit(DECISION_B), ids=(DECISION_B,))
    assert ok.created and len(github.created) == 1


def test_a_unit_cannot_stage_anything_but_feed_configuration(repo, tmp_path):
    with pytest.raises(pub.PublicationError, match="not feed configuration"):
        publish(repo, tmp_path, unit(DECISION_A), apply=apply_to("notes.txt", "x\n"))
    outside = tmp_path / "elsewhere.yml"
    outside.write_text("x")
    with pytest.raises(pub.PublicationError, match="outside the unit's worktree"):
        publish(repo, tmp_path, unit(DECISION_A), apply=lambda worktree: [outside])
    with pytest.raises(pub.PublicationError, match="no feed changes"):
        publish(repo, tmp_path, unit(DECISION_A), apply=lambda worktree: [])
    assert status(repo) == "" and list((tmp_path / "work").glob("*")) == []


def test_two_units_never_share_state_and_each_stages_only_its_own_files(repo, tmp_path):
    first = unit(DECISION_A)
    second = unit(DECISION_B)
    result_a, _ = publish(
        repo,
        tmp_path,
        first,
        ids=(DECISION_A,),
        apply=apply_to("config/feeds/council.yml", "a: 1\n"),
    )
    result_b, _ = publish(
        repo,
        tmp_path,
        second,
        ids=(DECISION_B,),
        apply=apply_to("config/feeds/library.yml", "b: 1\n"),
    )
    assert result_a.manifest == ("config/feeds/council.yml",)
    assert result_b.manifest == ("config/feeds/library.yml",)
    origin = repo.parent / "origin.git"
    assert sh("git", "diff", "--name-only", "main", first.branch, cwd=origin).split() == [
        "config/feeds/council.yml"
    ]
    assert sh("git", "diff", "--name-only", "main", second.branch, cwd=origin).split() == [
        "config/feeds/library.yml"
    ]


def test_a_leftover_worktree_from_a_crashed_run_is_replaced(repo, tmp_path):
    planned = unit(DECISION_A)
    stale = tmp_path / "work" / planned.branch.replace("/", "__")
    stale.mkdir(parents=True)
    (stale / "junk").write_text("left behind")
    result, _ = publish(repo, tmp_path, planned)
    assert result.created and not stale.exists()


def test_a_generated_section_is_replaced_and_a_persons_notes_are_kept():
    prior = f"intro note\n{pub.GENERATED_START}\nold\n{pub.GENERATED_END}\nmy follow-up\n"
    out = pub.replace_generated(prior, f"{pub.GENERATED_START}\nnew\n{pub.GENERATED_END}")
    assert "new" in out and "old" not in out and "intro note" in out and "my follow-up" in out
    assert pub.replace_generated("only a person's text", "GEN") == "GEN\nonly a person's text"


def test_a_refinement_issue_preserves_the_decision_and_points_at_the_rejected_pr():
    body = pub.refinement_issue_body(
        DECISION_A,
        binding(DECISION_A),
        rejected_pr="https://github.com/o/r/pull/9",
        evidence_hash="e" * 64,
    )
    assert parse_markers(body) == (DECISION_A,) and DECISION_A in parse_bindings(body)
    assert "pull/9" in body and f"/remedy reopen {DECISION_A}" in body and "Recordings: 1" in body


def test_an_orphaned_remote_branch_from_a_crashed_run_is_replaced_not_a_permanent_block(
    repo, tmp_path
):
    """CodeRabbit CLI review: a crash between the push and creating the PR left a remote branch
    whose commit differs from the next run's, so the next push was rejected for ever."""
    planned = unit(DECISION_A)
    origin = repo.parent / "origin.git"
    # The orphan: the same branch name, pushed earlier, holding a different commit.
    sh("git", "switch", "-c", planned.branch, cwd=repo)
    (repo / "config" / "feeds" / "council.yml").write_text("slug: council\n# an earlier attempt\n")
    sh("git", "commit", "-am", "earlier attempt", cwd=repo)
    sh("git", "push", "origin", planned.branch, cwd=repo)
    orphan = sh("git", "rev-parse", planned.branch, cwd=origin).strip()
    sh("git", "switch", "main", cwd=repo)
    sh("git", "branch", "-D", planned.branch, cwd=repo)

    result, github = publish(repo, tmp_path, planned)
    assert result.created and len(github.created) == 1
    replaced = sh("git", "rev-parse", planned.branch, cwd=origin).strip()
    assert replaced != orphan
    assert sh("git", "diff", "--name-only", "main", planned.branch, cwd=origin).split() == [
        "config/feeds/council.yml"
    ]


def test_the_push_never_overwrites_a_remote_branch_that_moved_after_it_was_observed(
    repo, tmp_path, monkeypatch
):
    planned = unit(DECISION_A)
    origin = repo.parent / "origin.git"
    real = pub.Git.run

    def racing(self, *args, **kwargs):
        out = real(self, *args, **kwargs)
        if args[:2] == ("ls-remote", "--heads"):
            # Someone else pushes to the branch after we looked and before we push.
            other = repo.parent / "other"
            sh("git", "clone", str(origin), str(other), cwd=repo.parent)
            sh("git", "config", "user.name", "x", cwd=other)
            sh("git", "config", "user.email", "x@example.test", cwd=other)
            sh("git", "switch", "-c", planned.branch, cwd=other)
            (other / "intruder.txt").write_text("not ours")
            sh("git", "add", "-A", cwd=other)
            sh("git", "commit", "-m", "intruder", cwd=other)
            sh("git", "push", "origin", planned.branch, cwd=other)
        return out

    monkeypatch.setattr(pub.Git, "run", racing)
    with pytest.raises(pub.PublicationError, match="git push"):
        publish(repo, tmp_path, planned)
    # The intruder's commit is still there: a lease, not a blind force.
    assert "intruder" in sh("git", "log", "--oneline", planned.branch, cwd=origin)
