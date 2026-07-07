from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mcp_project_updater.config import RepoAuthConfig, RepoConfig
from mcp_project_updater.constants import ExitCode
from mcp_project_updater.git_ops import (
    CommandResult,
    GitOperationError,
    clean_untracked_changes,
    default_command_runner,
    determine_target_commit,
    ensure_repo_available,
    validate_repo,
)


def _repo(path: Path) -> RepoConfig:
    return RepoConfig(
        path=path,
        branch="master",
        remote="origin",
        pull_mode="ff-only",
        clone_url="https://gitlab.example.com/team/orders.git",
        auth=RepoAuthConfig(
            type="none",
            token_secret=None,
            username="oauth2",
        ),
    )


def test_validate_repo_without_changes() -> None:
    calls = []

    def runner(command, cwd):
        calls.append(command)
        if command[2] == "--is-inside-work-tree":
            return CommandResult(0, "true\n", "")
        return CommandResult(0, "", "")

    result = validate_repo(cwd := Path("."), runner)

    assert result.inside_work_tree is True
    assert result.tracked_changes == []
    assert result.untracked_changes == []
    assert len(calls) == 2


def test_validate_repo_with_tracked_changes_raises() -> None:
    def runner(command, cwd):
        if command[2] == "--is-inside-work-tree":
            return CommandResult(0, "true\n", "")
        return CommandResult(0, " M tracked.txt\n", "")

    with pytest.raises(GitOperationError) as exc:
        validate_repo(Path("."), runner)

    assert exc.value.exit_code == ExitCode.GIT_TRACKED_CHANGES
    assert "Tracked Git changes detected in repository: ." in str(exc.value)
    assert "Changed tracked paths from git status --porcelain:" in str(exc.value)
    assert "  M tracked.txt" in str(exc.value)
    assert "does not discard tracked changes automatically" in str(exc.value)


def test_validate_repo_collects_untracked_changes() -> None:
    def runner(command, cwd):
        if command[2] == "--is-inside-work-tree":
            return CommandResult(0, "true\n", "")
        return CommandResult(0, "?? new.txt\n", "")

    result = validate_repo(Path("."), runner)

    assert result.untracked_changes == ["?? new.txt"]


def test_clean_untracked_changes_runs_git_clean() -> None:
    calls = []

    def runner(command, cwd):
        calls.append((command, cwd))
        return CommandResult(0, "Removing new.txt\nRemoving generated/\n", "")

    removed = clean_untracked_changes(cwd := Path("."), runner)

    assert removed == ["Removing new.txt", "Removing generated/"]
    assert calls == [(["git", "clean", "-ffdx"], cwd)]


def test_default_command_runner_decodes_git_output_as_utf8(monkeypatch) -> None:
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="РегламентированныйОтчет\n", stderr="")

    monkeypatch.setattr("mcp_project_updater.git_ops.subprocess.run", fake_run)

    result = default_command_runner(["git", "status"], Path("."))

    assert result.stdout == "РегламентированныйОтчет\n"
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"


def test_ensure_repo_available_clones_missing_repo(tmp_path: Path) -> None:
    calls = []
    repo = _repo(tmp_path / "repo")

    def runner(command, cwd):
        calls.append((command, cwd))
        return CommandResult(0, "", "")

    ensure_repo_available(repo, no_git_pull=False, runner=runner)

    assert calls == [
        (
            ["git", "clone", "--branch", "master", "--single-branch", "https://gitlab.example.com/team/orders.git", str(repo.path)],
            repo.path.parent,
        )
    ]


def test_ensure_repo_available_returns_for_existing_git_repo(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    repo.path.mkdir()
    calls = []

    def runner(command, cwd):
        calls.append((command, cwd))
        if command[2] == "--is-inside-work-tree":
            return CommandResult(0, "true\n", "")
        return CommandResult(0, "", "")

    ensure_repo_available(repo, no_git_pull=False, runner=runner)

    assert calls == [(["git", "rev-parse", "--is-inside-work-tree"], repo.path)]


def test_ensure_repo_available_clones_into_existing_empty_directory(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    repo.path.mkdir()
    calls = []

    def runner(command, cwd):
        calls.append((command, cwd))
        if command[2] == "--is-inside-work-tree":
            return CommandResult(128, "", "fatal: not a git repository")
        return CommandResult(0, "", "")

    ensure_repo_available(repo, no_git_pull=False, runner=runner)

    assert calls == [
        (["git", "rev-parse", "--is-inside-work-tree"], repo.path),
        (
            ["git", "clone", "--branch", "master", "--single-branch", "https://gitlab.example.com/team/orders.git", str(repo.path)],
            repo.path.parent,
        ),
    ]


def test_ensure_repo_available_rejects_existing_non_git_non_empty_directory(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    repo.path.mkdir()
    (repo.path / "junk.txt").write_text("x", encoding="utf-8")

    def runner(command, cwd):
        if command[2] == "--is-inside-work-tree":
            return CommandResult(128, "", "fatal: not a git repository")
        return CommandResult(0, "", "")

    with pytest.raises(GitOperationError) as exc:
        ensure_repo_available(repo, no_git_pull=False, runner=runner)

    assert exc.value.exit_code == ExitCode.GIT_REPOSITORY_NOT_FOUND
    assert "is not empty" in str(exc.value)


def test_ensure_repo_available_rejects_missing_repo_with_no_git_pull(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")

    with pytest.raises(GitOperationError) as exc:
        ensure_repo_available(repo, no_git_pull=True, runner=lambda command, cwd: CommandResult(0, "", ""))

    assert exc.value.exit_code == ExitCode.GIT_REPOSITORY_NOT_FOUND


def test_determine_target_commit_with_no_git_pull() -> None:
    calls = []
    repo = _repo(Path("."))

    def runner(command, cwd):
        calls.append(command)
        return CommandResult(0, "abc123\n", "")

    commit = determine_target_commit(
        repo,
        no_git_pull=True,
        runner=runner,
    )

    assert commit == "abc123"
    assert calls == [["git", "rev-parse", "HEAD"]]


def test_determine_target_commit_with_git_pull_flow() -> None:
    calls = []
    repo = _repo(Path("."))

    def runner(command, cwd):
        calls.append(command)
        if command[:2] == ["git", "rev-parse"]:
            return CommandResult(0, "def456\n", "")
        return CommandResult(0, "", "")

    commit = determine_target_commit(
        repo,
        no_git_pull=False,
        runner=runner,
    )

    assert commit == "def456"
    assert calls == [
        ["git", "fetch", "origin", "master"],
        ["git", "checkout", "master"],
        ["git", "clean", "-ffdx"],
        ["git", "pull", "--ff-only", "origin", "master"],
        ["git", "rev-parse", "origin/master"],
    ]


def test_determine_target_commit_retries_pull_after_removing_blocking_untracked_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    blocker = repo.path / "generated" / "blocked.txt"
    blocker.parent.mkdir(parents=True)
    blocker.write_text("stale", encoding="utf-8")
    calls = []
    pull_attempts = {"count": 0}

    def runner(command, cwd):
        calls.append(command)
        if command[:2] == ["git", "pull"]:
            pull_attempts["count"] += 1
            if pull_attempts["count"] == 1:
                return CommandResult(
                    1,
                    "",
                    "\n".join(
                        [
                            "error: The following untracked working tree files would be overwritten by merge:",
                            "\tgenerated/blocked.txt",
                            "Please move or remove them before you merge.",
                            "Aborting",
                        ]
                    ),
                )
            return CommandResult(0, "", "")
        if command[:2] == ["git", "rev-parse"]:
            return CommandResult(0, "def456\n", "")
        return CommandResult(0, "", "")

    commit = determine_target_commit(repo, no_git_pull=False, runner=runner)

    assert commit == "def456"
    assert blocker.exists() is False
    assert pull_attempts["count"] == 2
    assert calls == [
        ["git", "fetch", "origin", "master"],
        ["git", "checkout", "master"],
        ["git", "clean", "-ffdx"],
        ["git", "pull", "--ff-only", "origin", "master"],
        ["git", "pull", "--ff-only", "origin", "master"],
        ["git", "rev-parse", "origin/master"],
    ]


def test_determine_target_commit_does_not_retry_unrelated_pull_error(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    calls = []

    def runner(command, cwd):
        calls.append(command)
        if command[:2] == ["git", "pull"]:
            return CommandResult(1, "", "fatal: Not possible to fast-forward, aborting.")
        return CommandResult(0, "", "")

    with pytest.raises(GitOperationError) as exc:
        determine_target_commit(repo, no_git_pull=False, runner=runner)

    assert "Not possible to fast-forward" in str(exc.value)
    assert calls == [
        ["git", "fetch", "origin", "master"],
        ["git", "checkout", "master"],
        ["git", "clean", "-ffdx"],
        ["git", "pull", "--ff-only", "origin", "master"],
    ]


def test_gitlab_token_auth_is_applied_to_clone_and_fetch(tmp_path: Path) -> None:
    calls = []
    repo = RepoConfig(
        path=tmp_path / "repo",
        branch="master",
        remote="origin",
        pull_mode="ff-only",
        clone_url="https://gitlab.example.com/team/orders.git",
        auth=RepoAuthConfig(
            type="gitlab-token",
            token_secret="GITLAB_TOKEN",
            username="oauth2",
        ),
    )

    def runner(command, cwd):
        calls.append(command)
        if command[-2:] == ["rev-parse", "origin/master"]:
            return CommandResult(0, "fedcba\n", "")
        if command[-2:] == ["rev-parse", "HEAD"]:
            return CommandResult(0, "fedcba\n", "")
        return CommandResult(0, "", "")

    ensure_repo_available(
        repo,
        no_git_pull=False,
        runner=runner,
        env={"GITLAB_TOKEN": "token-value"},
    )
    determine_target_commit(
        RepoConfig(
            path=Path("."),
            branch=repo.branch,
            remote=repo.remote,
            pull_mode=repo.pull_mode,
            clone_url=repo.clone_url,
            auth=repo.auth,
        ),
        no_git_pull=False,
        runner=runner,
        env={"GITLAB_TOKEN": "token-value"},
    )

    assert calls[0][:3] == ["git", "-c", calls[0][2]]
    assert calls[0][3:7] == ["clone", "--branch", "master", "--single-branch"]
    assert "http.extraHeader=AUTHORIZATION: Basic " in calls[0][2]
    assert calls[1][:3] == ["git", "-c", calls[1][2]]
    assert calls[1][3:] == ["fetch", "origin", "master"]
    assert calls[3] == ["git", "clean", "-ffdx"]
    assert calls[4][:3] == ["git", "-c", calls[4][2]]
    assert calls[4][3:] == ["pull", "--ff-only", "origin", "master"]
