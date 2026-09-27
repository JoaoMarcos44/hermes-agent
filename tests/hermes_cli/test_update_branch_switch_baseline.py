"""Regression coverage for updater-driven branch switches (#125685).

The invariants are exercised with disposable real Git repositories.  The first
case pins the reported detached-HEAD miscount.  The second pins a fork round
trip where origin/main moves first and upstream/main then returns HEAD to the
SHA that was running before the update; that is still a successful branch
repair, not a no-op.
"""
from __future__ import annotations

import subprocess

import pytest

from hermes_cli import update_cmd


def git(root, *args):
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.com")
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", root)
    return root


def commit(root, value, message):
    (root / "state.txt").write_text(value + "\n", encoding="utf-8")
    git(root, "add", "state.txt")
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


def prepare(root, *, is_fork=False):
    return update_cmd._prepare_checkout_for_update(
        ["git"],
        "main",
        update_cmd._current_branch_name(["git"], check=True),
        is_fork=is_fork,
        assume_yes=True,
        gateway_mode=False,
        gw_input_fn=None,
        switch_branch=False,
        _windows_gateway_resume=None,
    )


def pull(plan, *, sync_upstream=False):
    return update_cmd._pull_updates(
        ["git"],
        "main",
        plan.auto_stash_ref,
        prompt_for_restore=False,
        gw_input_fn=None,
        discard_local_changes=False,
        keep_stash=False,
        pre_sync_sha=plan.pre_sync_sha,
        rollback_branch=plan.rollback_branch,
        sync_upstream=sync_upstream,
    )


def test_existing_local_main_counts_from_the_code_that_was_running(repo):
    old = commit(repo, "old", "old")
    target = commit(repo, "target", "target")
    git(repo, "update-ref", "refs/remotes/origin/main", target)
    git(repo, "checkout", "-q", "--detach", old)

    plan = prepare(repo)

    assert plan.commit_count == 1
    assert plan.pre_sync_sha == old
    assert plan.rollback_branch == "HEAD"
    assert git(repo, "rev-parse", "HEAD") == target

    pull(plan)

    assert git(repo, "rev-parse", "HEAD") == target
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_fork_sync_round_trip_is_not_misclassified_as_noop(repo, monkeypatch):
    old = commit(repo, "old", "old")
    fork_tip = commit(repo, "fork", "fork")
    upstream_tip = commit(repo, "upstream", "upstream")

    git(repo, "checkout", "-q", "--detach", upstream_tip)
    git(repo, "branch", "-f", "main", old)
    git(repo, "update-ref", "refs/remotes/origin/main", fork_tip)
    git(repo, "update-ref", "refs/remotes/upstream/main", upstream_tip)

    plan = prepare(repo, is_fork=True)
    assert plan.commit_count != 0
    assert plan.pre_sync_sha == upstream_tip
    assert git(repo, "rev-parse", "HEAD") == old

    def sync_upstream(*_args, **_kwargs):
        git(repo, "merge", "--ff-only", "refs/remotes/upstream/main")
        return True

    monkeypatch.setattr(
        update_cmd._m(), "_sync_with_upstream_if_needed", sync_upstream
    )

    pull(plan, sync_upstream=True)

    assert git(repo, "rev-parse", "HEAD") == upstream_tip
    assert git(repo, "rev-parse", "main") == upstream_tip
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
