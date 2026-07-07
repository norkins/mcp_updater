from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

from .config import InfrastructureSmokeConfig, ProjectConfig, load_project_config
from .constants import ExitCode, REPORT_FILE_NAME
from .docker_ops import default_docker_runner, ensure_docker_available, write_container_logs
from .fingerprints import compute_report_hash, compute_source_fingerprint
from .git_ops import clean_untracked_changes, determine_target_commit, discard_tracked_changes, ensure_repo_available, validate_repo
from .lock import LockManager
from .mcp_container import start_build_container
from .metadata_repair import run_metadata_index_repair
from .notifications import NotificationPayload, cleanup_old_logs, send_notification
from .parser_runner import run_parser
from .report_validator import validate_report
from .rollback import perform_manual_rollback
from .smoke_infrastructure import InfrastructureSmokeContext, run_infrastructure_smoke_test
from .smoke_tool import run_tool_smoke_test
from .source_detector import SourceDetectionResult, detect_sources
from .staging import (
    BuildPaths,
    copy_native_report,
    generate_parser_config,
    prepare_build_code_directory,
    prepare_build_staging,
    write_parser_config,
)
from .state import StateSnapshot, StateStore
from .switcher import perform_switch, run_production_smoke_test
from .errors import UpdaterError
from .logging_setup import setup_logging


@dataclass(slots=True)
class CliOptions:
    config_path: Path
    force: bool = False
    no_git_pull: bool = False
    rollback: bool = False
    promote_existing_build: bool = False
    storage_migration: bool = False
    repair_metadata_index: bool = False
    promote_commit: str | None = None
    promote_source_fingerprint: str | None = None
    promote_report_hash: str | None = None
    verbose: bool = False
    dry_run: bool = False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Update MCP project index from a Git repository.")
    parser.add_argument("--config", required=True, help="Path to project.json")
    parser.add_argument("--force", action="store_true", help="Reindex the current commit even if unchanged.")
    parser.add_argument(
        "--storage-migration",
        action="store_true",
        help="Run ChromaDB to zvec storage cutover without seeding build storage from current storage.",
    )
    parser.add_argument(
        "--repair-metadata-index",
        action="store_true",
        help="Rebuild metadata vector index from current storage without reindexing code.",
    )
    parser.add_argument("--no-git-pull", action="store_true", help="Use current HEAD without git fetch/pull.")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--rollback", action="store_true", help="Run manual rollback current <-> previous.")
    mode_group.add_argument(
        "--promote-existing-build",
        action="store_true",
        help="Accept existing staging/build and index storage build without rerunning parser or starting a new build container.",
    )
    parser.add_argument("--promote-commit", help="Commit to record when promoting an existing build.")
    parser.add_argument("--promote-source-fingerprint", help="Source fingerprint to record when promoting an existing build.")
    parser.add_argument("--promote-report-hash", help="Report hash to record when promoting an existing build.")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging.")
    parser.add_argument("--dry-run", action="store_true", help="Validate config and show planned actions.")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> CliOptions:
    parser = build_parser()
    namespace = parser.parse_args(argv)
    if namespace.storage_migration and namespace.force:
        parser.error("--storage-migration cannot be combined with --force.")
    if namespace.storage_migration and namespace.rollback:
        parser.error("--storage-migration cannot be combined with --rollback.")
    if namespace.storage_migration and namespace.promote_existing_build:
        parser.error("--storage-migration cannot be combined with --promote-existing-build.")
    if namespace.repair_metadata_index and namespace.force:
        parser.error("--repair-metadata-index cannot be combined with --force.")
    if namespace.repair_metadata_index and namespace.storage_migration:
        parser.error("--repair-metadata-index cannot be combined with --storage-migration.")
    if namespace.repair_metadata_index and namespace.rollback:
        parser.error("--repair-metadata-index cannot be combined with --rollback.")
    if namespace.repair_metadata_index and namespace.promote_existing_build:
        parser.error("--repair-metadata-index cannot be combined with --promote-existing-build.")
    if namespace.repair_metadata_index and namespace.dry_run:
        parser.error("--repair-metadata-index cannot be combined with --dry-run.")
    return CliOptions(
        config_path=Path(namespace.config),
        force=namespace.force,
        no_git_pull=namespace.no_git_pull,
        rollback=namespace.rollback,
        promote_existing_build=namespace.promote_existing_build,
        storage_migration=namespace.storage_migration,
        repair_metadata_index=namespace.repair_metadata_index,
        promote_commit=namespace.promote_commit,
        promote_source_fingerprint=namespace.promote_source_fingerprint,
        promote_report_hash=namespace.promote_report_hash,
        verbose=namespace.verbose,
        dry_run=namespace.dry_run,
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        options = parse_args(argv)
        config = load_project_config(options.config_path)
        log_path = setup_logging(config.paths.logs_root, options.verbose)
        logger = logging.getLogger(__name__)
        logger.info("Loaded config for project '%s'.", config.project)
        logger.info("Log file: %s", log_path)
        return run_update(config, options, log_path=log_path)
    except UpdaterError as exc:
        if logging.getLogger().handlers:
            logging.getLogger(__name__).error(str(exc))
        else:
            print(str(exc))
        return int(exc.exit_code)


def run_update(config: ProjectConfig, options: CliOptions, *, log_path: Path) -> int:
    logger = logging.getLogger(__name__)
    state_store = StateStore(config.paths.state_root)
    lock_manager = LockManager(
        state_store.lock_path,
        config.project,
        (
            "rollback"
            if options.rollback
            else (
                "promote"
                if options.promote_existing_build
                else (
                    "storage-migration"
                    if options.storage_migration
                    else (
                        "metadata-repair"
                        if options.repair_metadata_index
                        else ("dry-run" if options.dry_run else "update")
                    )
                )
            )
        ),
    )

    lock_manager.acquire()
    logger.info("Lock acquired: %s", state_store.lock_path)
    stage = "startup"
    target_commit: str | None = None
    source_fingerprint: str | None = None
    report_hash: str | None = None
    last_indexed_commit_at_start = state_store.read_last_indexed_commit()
    production_untouched = True
    rollback_attempted = False
    rollback_success: bool | None = None

    try:
        if options.rollback:
            return run_rollback(
                config,
                state_store,
                log_path=log_path,
                last_indexed_commit_at_start=last_indexed_commit_at_start,
            )
        if options.promote_existing_build:
            return run_promote_existing_build(
                config,
                options,
                state_store,
                log_path=log_path,
                last_indexed_commit_at_start=last_indexed_commit_at_start,
            )

        stage = "git_prepare"
        ensure_repo_available(
            config.repo,
            no_git_pull=options.no_git_pull,
            env=config.secrets_values,
        )

        stage = "git_validation"
        repo_validation = validate_repo(config.repo.path)
        if options.no_git_pull:
            if repo_validation.tracked_changes:
                logger.warning(
                    "Tracked Git changes detected and left in place because --no-git-pull is set: %s",
                    repo_validation.tracked_changes,
                )
            if repo_validation.untracked_changes:
                logger.warning(
                    "Untracked Git changes detected and left in place because --no-git-pull is set: %s",
                    repo_validation.untracked_changes,
                )
        else:
            if repo_validation.tracked_changes:
                logger.warning(
                    "Tracked Git changes detected in managed repository; discarding before pull: %s",
                    repo_validation.tracked_changes,
                )
                reset_output = discard_tracked_changes(config.repo.path)
                logger.info("Discarded tracked Git changes before pull: %s", reset_output or "<none>")
            if repo_validation.untracked_changes:
                logger.warning(
                    "Untracked Git changes detected in managed repository; cleaning before pull: %s",
                    repo_validation.untracked_changes,
                )
            cleaned_paths = clean_untracked_changes(config.repo.path)
            logger.info("Cleaned untracked/ignored Git paths before pull: %s", cleaned_paths or "<none>")

        stage = "git_target_commit"
        target_commit = determine_target_commit(
            config.repo,
            no_git_pull=options.no_git_pull,
            env=config.secrets_values,
        )
        state_snapshot = state_store.read_snapshot()
        last_indexed_commit_at_start = state_snapshot.last_indexed_commit
        stage = "source_detection"
        source_result = detect_sources(
            config.repo.path,
            config.sources.main_config_path,
            config.sources.main_config_required,
            config.sources.extension_path,
            config.sources.extension_required,
            config.sources.native_report_path,
        )

        logger.info("Target commit: %s", target_commit)
        logger.info("Last indexed commit: %s", state_snapshot.last_indexed_commit or "<none>")
        logger.info(
            "Detected sources: main=%s extension=%s",
            source_result.main_exists,
            source_result.extension_exists,
        )

        stage = "source_fingerprint"
        source_fingerprint = compute_source_fingerprint(source_result)
        logger.info("Source fingerprint: %s", source_fingerprint)
        current_report_exists = (config.paths.staging_root / "current" / "metadata" / "Report.txt").exists()
        current_index_storage_path = config.paths.index_storage_root / "current"
        current_index_storage_exists = current_index_storage_path.exists()
        if (
            target_commit == state_snapshot.last_indexed_commit
            and source_fingerprint == state_snapshot.last_source_fingerprint
            and not options.force
            and not options.storage_migration
            and not options.repair_metadata_index
            and current_report_exists
            and current_index_storage_exists
        ):
            logger.info("No changes detected. Update is skipped.")
            return ExitCode.SUCCESS
        if (
            source_fingerprint == state_snapshot.last_source_fingerprint
            and not options.force
            and not options.storage_migration
            and not options.repair_metadata_index
            and current_report_exists
            and current_index_storage_exists
        ):
            logger.info("No effective source changes detected. Update is skipped.")
            return ExitCode.SUCCESS

        if options.repair_metadata_index and not current_index_storage_exists:
            raise UpdaterError(
                f"Cannot repair metadata index: current index storage is missing: {current_index_storage_path}",
                ExitCode.INVALID_STATE,
            )

        if options.dry_run:
            _log_dry_run_summary(logger, config, options, state_snapshot, target_commit, source_result)
            return ExitCode.SUCCESS

        stage = "staging"
        build_paths = prepare_build_staging(config.paths.staging_root, config.project)
        if source_result.native_report_path is not None:
            stage = "report_copy"
            copied_report_path = copy_native_report(build_paths, source_result.native_report_path)
            logger.info("Using native report: %s -> %s", source_result.native_report_path, copied_report_path)
        else:
            parser_config_payload = generate_parser_config(config, build_paths, source_result)
            parser_config_path = write_parser_config(build_paths, parser_config_payload)
            stage = "parser"
            parser_result = run_parser(
                config.parser,
                parser_config_path,
                verbose=options.verbose,
                working_directory=config.repo.path,
            )
            logger.info("Parser exit code: %s", parser_result.returncode)

        stage = "report_validation"
        report_result = validate_report(
            build_paths.report_path,
            config.smoke_test.report_validation,
            build_paths.diagnostics,
        )
        logger.info("Validated report: %s (%s bytes)", report_result.report_path, report_result.report_size)
        report_hash = compute_report_hash(report_result.report_path)
        metadata_unchanged = (
            not options.force
            and not options.storage_migration
            and not options.repair_metadata_index
            and report_hash == state_snapshot.last_report_hash
            and current_report_exists
            and current_index_storage_exists
        )
        reuse_current_index_storage = (
            options.repair_metadata_index
            or (not options.force and not options.storage_migration and current_index_storage_exists)
        )
        logger.info("Report hash: %s", report_hash)
        logger.info("Metadata unchanged: %s", metadata_unchanged)
        logger.info("Reusing current MCP index storage baseline for build: %s", reuse_current_index_storage)

        stage = "code_prepare"
        prepare_build_code_directory(build_paths, source_result)
        logger.info("Prepared build code directory: %s", build_paths.code)

        stage = "docker_availability"
        docker_version = ensure_docker_available()
        logger.info("Docker available: %s", docker_version)

        stage = "build_container"
        build_container_result = start_build_container(
            config.mcp,
            build_paths,
            config.paths,
            runner=default_docker_runner,
            reset_database=False if reuse_current_index_storage else None,
            seed_index_storage_from=current_index_storage_path if reuse_current_index_storage else None,
            index_metadata=True if options.repair_metadata_index else (False if metadata_unchanged else None),
            index_code=False if options.repair_metadata_index else None,
            index_help=False if options.repair_metadata_index else None,
        )
        logger.info("Started build container: %s", config.mcp.build.container_name)

        stage = "build_infrastructure_smoke"
        build_infrastructure_smoke_config = _build_infrastructure_smoke_config(config)
        smoke_result = run_infrastructure_smoke_test(
            build_infrastructure_smoke_config,
            InfrastructureSmokeContext(
                container_name=config.mcp.build.container_name,
                host_port=config.mcp.build.host_port,
                url=config.mcp.build.url,
                index_storage_path=config.paths.index_storage_root / "build",
            ),
            runner=default_docker_runner,
        )
        logger.info("Infrastructure smoke-test passed with HTTP status %s", smoke_result.http_status_code)

        if options.repair_metadata_index:
            stage = "metadata_index_repair"
            repair_result = run_metadata_index_repair(
                config.mcp.build.url,
                timeout_seconds=_metadata_repair_timeout_seconds(config),
                retry_interval_seconds=config.smoke_test.tool_smoke_test.retry_interval_seconds,
                require_code_index=config.mcp.index_code,
            )
            logger.info(
                "Metadata index repair completed: metadata=%s code=%s",
                repair_result.metadata_count,
                repair_result.code_count,
            )

        if config.smoke_test.tool_smoke_test.enabled:
            stage = "build_tool_smoke"
            tool_smoke_config = _build_tool_smoke_config_for_update(
                config,
                last_indexed_commit_at_start=last_indexed_commit_at_start,
            )
            if tool_smoke_config.timeout_seconds <= 0:
                logger.info(
                    "Initial bootstrap detected: build tool smoke-test overall timeout disabled; retrying until MCP tools respond."
                )
            tool_smoke_result = run_tool_smoke_test(
                config,
                tool_smoke_config,
                working_directory=config.repo.path,
                url=config.mcp.build.url,
            )
            logger.info("Tool smoke-test passed: %s", tool_smoke_result.stdout.strip() or "<no output>")
        else:
            logger.warning("MCP tool smoke-test skipped")

        build_log_path = _derive_related_log_path(log_path, "mcp-build")
        write_container_logs(
            config.mcp.build.container_name,
            build_log_path,
            runner=default_docker_runner,
        )
        logger.info("Saved build container logs: %s", build_log_path)

        stage = "switch"
        production_untouched = False
        production_log_path = _derive_related_log_path(log_path, "mcp-production")
        switch_result = perform_switch(
            config,
            state_store,
            target_commit,
            production_log_path,
            docker_runner=default_docker_runner,
            storage_migration=options.storage_migration,
        )
        logger.info("Production switch completed for commit %s", switch_result.target_commit)
        if source_fingerprint is not None:
            state_store.write_last_source_fingerprint(source_fingerprint)
        if report_hash is not None:
            state_store.write_last_report_hash(report_hash)
        stage = "success"
        return _handle_success_notification_and_cleanup(
            config,
            NotificationPayload(
                project=config.project,
                status="success",
                stage=stage,
                targetCommit=target_commit,
                lastIndexedCommit=state_store.read_last_indexed_commit(),
                productionUntouched=False,
                rollbackAttempted=False,
                rollbackSuccess=None,
                logPath=str(log_path),
            ),
            log_path=log_path,
        )
    except UpdaterError as exc:
        rollback_attempted = getattr(
            exc,
            "rollback_attempted",
            exc.exit_code in {ExitCode.PRODUCTION_SMOKE_FAILED, ExitCode.ROLLBACK_FAILED},
        )
        rollback_success = (
            True
            if rollback_attempted and exc.exit_code == ExitCode.PRODUCTION_SMOKE_FAILED
            else (False if exc.exit_code == ExitCode.ROLLBACK_FAILED else None)
        )
        _handle_failure_notification_and_cleanup(
            config,
            NotificationPayload(
                project=config.project,
                status="rollback" if rollback_attempted else "failed",
                stage=stage,
                targetCommit=target_commit,
                lastIndexedCommit=last_indexed_commit_at_start,
                productionUntouched=production_untouched,
                rollbackAttempted=rollback_attempted,
                rollbackSuccess=rollback_success,
                logPath=str(log_path),
            ),
            logger=logger,
        )
        raise
    finally:
        lock_manager.release()
        logger.info("Lock released: %s", state_store.lock_path)


def run_rollback(
    config: ProjectConfig,
    state_store: StateStore,
    *,
    log_path: Path,
    last_indexed_commit_at_start: str | None,
) -> int:
    logger = logging.getLogger(__name__)
    production_log_path = _derive_related_log_path(log_path, "mcp-production")
    perform_manual_rollback(
        config,
        state_store,
        production_log_path,
        docker_runner=default_docker_runner,
        production_smoke_runner=lambda current_config: run_production_smoke_test(
            current_config,
            docker_runner=default_docker_runner,
        ),
    )
    logger.info("Manual rollback completed successfully.")
    return _handle_success_notification_and_cleanup(
        config,
        NotificationPayload(
            project=config.project,
            status="rollback",
            stage="manual_rollback",
            targetCommit=None,
            lastIndexedCommit=last_indexed_commit_at_start,
            productionUntouched=False,
            rollbackAttempted=True,
            rollbackSuccess=True,
            logPath=str(log_path),
        ),
        log_path=log_path,
    )


def run_promote_existing_build(
    config: ProjectConfig,
    options: CliOptions,
    state_store: StateStore,
    *,
    log_path: Path,
    last_indexed_commit_at_start: str | None,
) -> int:
    logger = logging.getLogger(__name__)
    stage = "promote_existing_build"

    build_root = config.paths.staging_root / "build"
    build_paths = _existing_build_paths(config)
    build_index_storage = config.paths.index_storage_root / "build"
    if not build_root.exists() or not build_index_storage.exists():
        raise UpdaterError("Existing build artifacts are missing; cannot promote staging/build.", ExitCode.PRODUCTION_SWITCH_FAILED)

    ensure_repo_available(config.repo, no_git_pull=options.no_git_pull, env=config.secrets_values)
    target_commit = options.promote_commit or determine_target_commit(config.repo, no_git_pull=options.no_git_pull, env=config.secrets_values)
    source_fingerprint = options.promote_source_fingerprint
    if source_fingerprint is None:
        source_result = detect_sources(
            config.repo.path,
            config.sources.main_config_path,
            config.sources.main_config_required,
            config.sources.extension_path,
            config.sources.extension_required,
        )
        source_fingerprint = compute_source_fingerprint(source_result)

    report_result = validate_report(
        build_paths.report_path,
        config.smoke_test.report_validation,
        build_paths.diagnostics,
    )
    report_hash = options.promote_report_hash or compute_report_hash(report_result.report_path)

    logger.info("Promoting existing build for commit: %s", target_commit)
    logger.info("Existing build report: %s (%s bytes)", report_result.report_path, report_result.report_size)
    logger.info("Source fingerprint: %s", source_fingerprint)
    logger.info("Report hash: %s", report_hash)

    if options.dry_run:
        logger.info("Dry-run mode enabled. Existing build promotion is skipped.")
        return ExitCode.SUCCESS

    docker_version = ensure_docker_available()
    logger.info("Docker available: %s", docker_version)

    existing_build_smoke_config = _build_infrastructure_smoke_config(config)
    smoke_result = run_infrastructure_smoke_test(
        existing_build_smoke_config,
        InfrastructureSmokeContext(
            container_name=config.mcp.build.container_name,
            host_port=config.mcp.build.host_port,
            url=config.mcp.build.url,
            index_storage_path=build_index_storage,
        ),
        runner=default_docker_runner,
    )
    logger.info("Existing build infrastructure smoke-test passed with HTTP status %s", smoke_result.http_status_code)

    if config.smoke_test.tool_smoke_test.enabled:
        tool_smoke_result = run_tool_smoke_test(
            config,
            config.smoke_test.tool_smoke_test,
            working_directory=config.repo.path,
            url=config.mcp.build.url,
        )
        logger.info("Existing build tool smoke-test passed: %s", tool_smoke_result.stdout.strip() or "<no output>")
    else:
        logger.warning("Existing build MCP tool smoke-test skipped")

    build_log_path = _derive_related_log_path(log_path, "mcp-build")
    write_container_logs(config.mcp.build.container_name, build_log_path, runner=default_docker_runner)
    logger.info("Saved existing build container logs: %s", build_log_path)

    production_log_path = _derive_related_log_path(log_path, "mcp-production")
    switch_result = perform_switch(
        config,
        state_store,
        target_commit,
        production_log_path,
        docker_runner=default_docker_runner,
    )
    logger.info("Existing build promoted to production for commit %s", switch_result.target_commit)
    state_store.write_last_source_fingerprint(source_fingerprint)
    state_store.write_last_report_hash(report_hash)

    return _handle_success_notification_and_cleanup(
        config,
        NotificationPayload(
            project=config.project,
            status="success",
            stage=stage,
            targetCommit=target_commit,
            lastIndexedCommit=state_store.read_last_indexed_commit(),
            productionUntouched=False,
            rollbackAttempted=False,
            rollbackSuccess=None,
            logPath=str(log_path),
        ),
        log_path=log_path,
    )


def _log_dry_run_summary(
    logger: logging.Logger,
    config: ProjectConfig,
    options: CliOptions,
    state_snapshot: StateSnapshot,
    target_commit: str,
    source_result: SourceDetectionResult,
) -> None:
    logger.info("Dry-run mode enabled.")
    logger.info(
        "Options: force=%s storage_migration=%s repair_metadata_index=%s no_git_pull=%s rollback=%s verbose=%s",
        options.force,
        options.storage_migration,
        options.repair_metadata_index,
        options.no_git_pull,
        options.rollback,
        options.verbose,
    )
    logger.info("Project root: %s", config.paths.root)
    logger.info("Repository path: %s", config.repo.path)
    logger.info("MCP index storage root: %s", config.paths.index_storage_root)
    logger.info("Production container: %s", config.mcp.production.container_name)
    logger.info("Build container: %s", config.mcp.build.container_name)
    logger.info("Production host port: %s", config.mcp.production.host_port)
    logger.info("Build host port: %s", config.mcp.build.host_port)
    logger.info("Branch: %s", config.repo.branch)
    logger.info("Target commit: %s", target_commit)
    logger.info("Last indexed commit: %s", state_snapshot.last_indexed_commit or "<none>")
    logger.info("Current commit: %s", state_snapshot.current_commit or "<none>")
    logger.info("Previous commit: %s", state_snapshot.previous_commit or "<none>")
    logger.info("Last source fingerprint: %s", state_snapshot.last_source_fingerprint or "<none>")
    logger.info("Last report hash: %s", state_snapshot.last_report_hash or "<none>")
    logger.info("Main source exists: %s", source_result.main_exists)
    logger.info("Extension source exists: %s", source_result.extension_exists)
    logger.info("Build URL: %s", config.mcp.build.url)
    logger.info("Production URL: %s", config.mcp.production.url)
    logger.info("Smoke profile: %s", config.smoke_test.profile)
    logger.info("Tool smoke-test enabled: %s", config.smoke_test.tool_smoke_test.enabled)


def _derive_related_log_path(update_log_path: Path, suffix: str) -> Path:
    file_name = update_log_path.name
    if file_name.endswith("-update.log"):
        return update_log_path.with_name(file_name.replace("-update.log", f"-{suffix}.log"))
    return update_log_path.with_name(f"{update_log_path.stem}-{suffix}.log")


def _existing_build_paths(config: ProjectConfig) -> BuildPaths:
    build_root = config.paths.staging_root / "build"
    metadata = build_root / "metadata"
    code = build_root / "code"
    diagnostics = build_root / "diagnostics"
    logs = build_root / "logs"
    settings = build_root / "settings"
    return BuildPaths(
        root=build_root,
        metadata=metadata,
        code=code,
        diagnostics=diagnostics,
        logs=logs,
        settings=settings,
        parser_config_path=build_root / "parser-config.json",
        report_path=metadata / REPORT_FILE_NAME,
        generator_settings_path=settings / f"{config.project}.xml-overrides.json",
    )


def _build_tool_smoke_config_for_update(
    config: ProjectConfig,
    *,
    last_indexed_commit_at_start: str | None,
):
    if last_indexed_commit_at_start is None and config.smoke_test.tool_smoke_test.timeout_seconds > 0:
        return replace(config.smoke_test.tool_smoke_test, timeout_seconds=0)
    return config.smoke_test.tool_smoke_test


def _build_infrastructure_smoke_config(config: ProjectConfig) -> InfrastructureSmokeConfig:
    extended_timeout = config.smoke_test.tool_smoke_test.attempt_timeout_seconds * 10
    return replace(
        config.smoke_test.infrastructure,
        timeout_seconds=max(config.smoke_test.infrastructure.timeout_seconds, extended_timeout),
        log_ready_patterns=[],
    )


def _metadata_repair_timeout_seconds(config: ProjectConfig) -> int:
    if config.smoke_test.tool_smoke_test.timeout_seconds > 0:
        return config.smoke_test.tool_smoke_test.timeout_seconds
    return max(
        config.smoke_test.tool_smoke_test.attempt_timeout_seconds,
        config.smoke_test.infrastructure.timeout_seconds,
        1,
    )


def _handle_success_notification_and_cleanup(
    config: ProjectConfig,
    payload: NotificationPayload,
    *,
    log_path: Path,
) -> int:
    logger = logging.getLogger(__name__)
    removed_logs = cleanup_old_logs(config.paths.logs_root, config.retention.keep_logs_days)
    if removed_logs:
        logger.info("Removed old logs: %s", [str(path) for path in removed_logs])

    if not config.notifications.enabled or (not config.notifications.on_success and payload.status == "success"):
        return ExitCode.SUCCESS
    if payload.status == "rollback" and not config.notifications.on_rollback:
        return ExitCode.SUCCESS

    try:
        send_notification(config.notifications, payload)
    except Exception as exc:
        logger.warning("Notification warning: %s", exc)
        return ExitCode.SUCCESS_WITH_WARNINGS
    return ExitCode.SUCCESS


def _handle_failure_notification_and_cleanup(
    config: ProjectConfig,
    payload: NotificationPayload,
    *,
    logger: logging.Logger,
) -> None:
    removed_logs = cleanup_old_logs(config.paths.logs_root, config.retention.keep_logs_days)
    if removed_logs:
        logger.info("Removed old logs: %s", [str(path) for path in removed_logs])

    if payload.status == "rollback":
        enabled = config.notifications.enabled and config.notifications.on_rollback
    else:
        enabled = config.notifications.enabled and config.notifications.on_failure
    if not enabled:
        return

    try:
        send_notification(config.notifications, payload)
    except Exception as exc:
        logger.warning("Notification warning: %s", exc)
