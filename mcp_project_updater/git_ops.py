from __future__ import annotations

import base64
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .config import RepoAuthConfig, RepoConfig
from .constants import ExitCode
from .errors import UpdaterError


class GitOperationError(UpdaterError):
    pass


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[Sequence[str], Path], CommandResult]


def default_command_runner(command: Sequence[str], cwd: Path) -> CommandResult:
    completed = subprocess.run(
        list(command),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


@dataclass(slots=True)
class RepoValidationResult:
    inside_work_tree: bool
    tracked_changes: list[str]
    untracked_changes: list[str]


def ensure_repo_available(
    repo: RepoConfig,
    *,
    no_git_pull: bool,
    runner: CommandRunner = default_command_runner,
    env: dict[str, str] | None = None,
) -> None:
    if repo.path.exists():
        if _is_git_repository(repo.path, runner):
            return

        if no_git_pull:
            raise GitOperationError(
                f"Repository path exists but is not a Git repository and '--no-git-pull' forbids cloning: {repo.path}",
                ExitCode.GIT_REPOSITORY_NOT_FOUND,
            )

        if not repo.clone_url:
            raise GitOperationError(
                f"Repository path exists but is not a Git repository and no clone URL is configured: {repo.path}",
                ExitCode.GIT_REPOSITORY_NOT_FOUND,
            )

        if any(repo.path.iterdir()):
            raise GitOperationError(
                f"Repository path exists but is not a Git repository and is not empty: {repo.path}",
                ExitCode.GIT_REPOSITORY_NOT_FOUND,
            )

        _clone_repo(repo, runner=runner, env=env)
        return

    if no_git_pull:
        raise GitOperationError(
            f"Repository path does not exist and '--no-git-pull' forbids cloning: {repo.path}",
            ExitCode.GIT_REPOSITORY_NOT_FOUND,
        )

    if not repo.clone_url:
        raise GitOperationError(
            f"Repository path does not exist and no clone URL is configured: {repo.path}",
            ExitCode.GIT_REPOSITORY_NOT_FOUND,
        )

    _clone_repo(repo, runner=runner, env=env)


def validate_repo(repo_path: Path, runner: CommandRunner = default_command_runner) -> RepoValidationResult:
    inside = _run_git(repo_path, ["git", "rev-parse", "--is-inside-work-tree"], runner, ExitCode.GIT_REPOSITORY_NOT_FOUND)
    if inside.stdout.strip().lower() != "true":
        raise GitOperationError(f"Path is not a Git work tree: {repo_path}", ExitCode.GIT_REPOSITORY_NOT_FOUND)

    status = _run_git(repo_path, ["git", "status", "--porcelain"], runner, ExitCode.GIT_PULL_FAILED)
    tracked_changes: list[str] = []
    untracked_changes: list[str] = []

    for line in [item for item in status.stdout.splitlines() if item.strip()]:
        if line.startswith("??"):
            untracked_changes.append(line)
        else:
            tracked_changes.append(line)

    if tracked_changes:
        raise GitOperationError(
            _format_tracked_changes_error(repo_path, tracked_changes),
            ExitCode.GIT_TRACKED_CHANGES,
        )

    return RepoValidationResult(
        inside_work_tree=True,
        tracked_changes=tracked_changes,
        untracked_changes=untracked_changes,
    )


def clean_untracked_changes(
    repo_path: Path,
    runner: CommandRunner = default_command_runner,
) -> list[str]:
    result = _run_git(repo_path, ["git", "clean", "-ffdx"], runner, ExitCode.GIT_PULL_FAILED)
    return [line for line in result.stdout.splitlines() if line.strip()]


def determine_target_commit(
    repo: RepoConfig,
    *,
    no_git_pull: bool,
    runner: CommandRunner = default_command_runner,
    env: dict[str, str] | None = None,
) -> str:
    if no_git_pull:
        result = _run_git(repo.path, ["git", "rev-parse", "HEAD"], runner, ExitCode.GIT_PULL_FAILED)
        return result.stdout.strip()

    fetch_command = _with_auth_options(
        ["git", "fetch", repo.remote, repo.branch],
        repo.auth,
        env=env,
    )
    _run_git(repo.path, fetch_command, runner, ExitCode.GIT_PULL_FAILED)
    _run_git(repo.path, ["git", "checkout", repo.branch], runner, ExitCode.GIT_PULL_FAILED)
    clean_untracked_changes(repo.path, runner=runner)

    pull_command = _with_auth_options(
        ["git", "pull", _render_pull_mode_flag(repo.pull_mode), repo.remote, repo.branch],
        repo.auth,
        env=env,
    )
    try:
        _run_git(repo.path, pull_command, runner, ExitCode.GIT_PULL_FAILED)
    except GitOperationError as exc:
        removed_paths = remove_pull_blocking_untracked_paths(repo.path, str(exc))
        if not removed_paths:
            raise
        logger.warning(
            "Removed Git pull blocking untracked paths before retry: %s",
            [str(path) for path in removed_paths],
        )
        _run_git(repo.path, pull_command, runner, ExitCode.GIT_PULL_FAILED)
    result = _run_git(repo.path, ["git", "rev-parse", f"{repo.remote}/{repo.branch}"], runner, ExitCode.GIT_PULL_FAILED)
    return result.stdout.strip()


def remove_pull_blocking_untracked_paths(repo_path: Path, git_error: str) -> list[Path]:
    removed_paths: list[Path] = []
    for relative_path in _parse_pull_blocking_untracked_paths(git_error):
        candidate = _resolve_repo_child(repo_path, relative_path)
        if not candidate.exists():
            continue
        if candidate.is_dir() and not candidate.is_symlink():
            shutil.rmtree(candidate)
        else:
            candidate.unlink()
        removed_paths.append(candidate)
    return removed_paths


def _parse_pull_blocking_untracked_paths(git_error: str) -> list[str]:
    marker = "untracked working tree files would be overwritten by merge:"
    paths: list[str] = []
    collecting = False
    for line in git_error.splitlines():
        if marker in line:
            collecting = True
            continue
        if not collecting:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("Please move or remove them before you merge.") or stripped == "Aborting":
            break
        if line.startswith((" ", "\t")):
            paths.append(stripped)
            continue
        break
    return paths


def _resolve_repo_child(repo_path: Path, relative_path: str) -> Path:
    candidate_relative = Path(relative_path)
    if candidate_relative.is_absolute() or ".." in candidate_relative.parts:
        raise GitOperationError(
            f"Refusing to remove unsafe Git cleanup path: {relative_path}",
            ExitCode.GIT_PULL_FAILED,
        )
    repo_root = repo_path.resolve()
    candidate = (repo_path / candidate_relative).resolve()
    try:
        candidate.relative_to(repo_root)
    except ValueError as exc:
        raise GitOperationError(
            f"Refusing to remove Git cleanup path outside repository: {relative_path}",
            ExitCode.GIT_PULL_FAILED,
        ) from exc
    if candidate == repo_root:
        raise GitOperationError(
            f"Refusing to remove repository root during Git cleanup: {relative_path}",
            ExitCode.GIT_PULL_FAILED,
        )
    return candidate


def _render_pull_mode_flag(pull_mode: str) -> str:
    return pull_mode if pull_mode.startswith("--") else f"--{pull_mode}"


def _format_tracked_changes_error(repo_path: Path, tracked_changes: list[str]) -> str:
    max_lines = 50
    visible_changes = tracked_changes[:max_lines]
    lines = [
        f"Tracked Git changes detected in repository: {repo_path}",
        "Changed tracked paths from git status --porcelain:",
        *[f"  {change}" for change in visible_changes],
    ]
    hidden_count = len(tracked_changes) - len(visible_changes)
    if hidden_count > 0:
        lines.append(f"  ... and {hidden_count} more tracked changes")
    lines.append("Updater does not discard tracked changes automatically. Review the repository and reset or commit changes before rerun.")
    return "\n".join(lines)


def _with_auth_options(
    command: Sequence[str],
    auth: RepoAuthConfig,
    *,
    env: dict[str, str] | None,
) -> list[str]:
    header_value = _build_auth_header(auth, env=env)
    if header_value is None:
        return list(command)
    return ["git", "-c", f"http.extraHeader={header_value}", *list(command)[1:]]


def _build_auth_header(auth: RepoAuthConfig, *, env: dict[str, str] | None) -> str | None:
    if auth.type == "none":
        return None
    if auth.type != "gitlab-token":
        raise GitOperationError(f"Unsupported repo auth type: {auth.type}", ExitCode.GIT_PULL_FAILED)

    token_secret = auth.token_secret or ""
    env_map = env or {}
    token = env_map.get(token_secret)
    if not token:
        raise GitOperationError(
            f"Required Git token secret is missing: {token_secret}",
            ExitCode.GIT_PULL_FAILED,
        )

    username = auth.username or "oauth2"
    basic_value = base64.b64encode(f"{username}:{token}".encode("utf-8")).decode("ascii")
    return f"AUTHORIZATION: Basic {basic_value}"


def _run_git(repo_path: Path, command: Sequence[str], runner: CommandRunner, error_code: int) -> CommandResult:
    result = runner(command, repo_path)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        details = stderr or stdout or "Git command failed."
        raise GitOperationError(details, error_code)
    return result


def _is_git_repository(repo_path: Path, runner: CommandRunner) -> bool:
    result = runner(["git", "rev-parse", "--is-inside-work-tree"], repo_path)
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def _clone_repo(
    repo: RepoConfig,
    *,
    runner: CommandRunner,
    env: dict[str, str] | None,
) -> None:
    repo.path.parent.mkdir(parents=True, exist_ok=True)
    command = _with_auth_options(
        [
            "git",
            "clone",
            "--branch",
            repo.branch,
            "--single-branch",
            repo.clone_url,
            str(repo.path),
        ],
        repo.auth,
        env=env,
    )
    _run_git(repo.path.parent, command, runner, ExitCode.GIT_PULL_FAILED)
