from __future__ import annotations

import json
from pathlib import Path

from mcp_project_updater.cli import main, parse_args
from mcp_project_updater.config import load_project_config
from mcp_project_updater.constants import ExitCode
from mcp_project_updater.errors import UpdaterError
from mcp_project_updater.fingerprints import compute_source_fingerprint
from mcp_project_updater.git_ops import RepoValidationResult
from mcp_project_updater.source_detector import detect_sources
from tests.config_helpers import strip_global_project_blocks, write_runtime_files


def _write_config(tmp_path: Path) -> Path:
    repo_path = tmp_path / "repo"
    (repo_path / "src" / "cf").mkdir(parents=True)
    parser_path = tmp_path / "generate_config_report.py"
    parser_path.write_text("print('ok')\n", encoding="utf-8")
    tool_path = tmp_path / "mcp_smoke_test.py"
    tool_path.write_text("print('ok')\n", encoding="utf-8")
    write_runtime_files(tmp_path, parser_path=parser_path, tool_path=tool_path)

    payload = {
        "project": "orders",
        "repo": {
            "branch": "master",
            "remote": "origin",
            "pullMode": "ff-only",
        },
        "sources": {
            "mainConfigPath": "src/cf",
            "mainConfigRequired": False,
            "extensionPath": "src/cfe",
            "extensionRequired": False,
        },
        "parser": {
            "toolPath": str(parser_path),
            "encoding": "utf-8",
            "warningsAsErrors": False,
            "buildXmlOverrides": True,
            "allowedExitCodes": [0, 1],
        },
        "mcp": {
            "image": "comol/1c_code_metadata_mcp:light",
            "indexStorageRoot": str(tmp_path / "index-storage"),
            "containerPort": 8000,
            "production": {
                "containerName": "mcp-orders",
                "hostPort": 8100,
                "url": "http://localhost:8100/mcp",
            },
            "build": {
                "containerName": "mcp-orders-build",
                "hostPort": 18100,
                "url": "http://localhost:18100/mcp",
            },
            "indexCode": True,
            "indexMetadata": True,
            "indexHelp": False,
            "resetDatabaseOnBuild": True,
            "resetCache": False,
            "useSse": False,
            "useGpu": False,
            "env": {},
            "secretEnv": {},
        },
        "paths": {
            "root": str(tmp_path),
        },
        "smokeTest": {
            "enabled": True,
            "profile": "dev",
            "reportValidation": {
                "enabled": True,
                "requiredReportPatterns": ['Имя: "', 'Синоним: "'],
                "forbiddenReportPatterns": [],
            },
            "infrastructure": {
                "enabled": True,
                "timeoutSeconds": 60,
                "checkIntervalSeconds": 5,
                "httpReadyUrl": "http://localhost:18100/mcp",
                "acceptableHttpStatusCodes": [200],
                "requireIndexStorageNotEmpty": True,
                "logTailLines": 100,
                "logErrorPatterns": ["Traceback"],
                "logReadyPatterns": ["Started"],
            },
            "toolSmokeTest": {
                "enabled": True,
                "toolPath": str(tmp_path / "mcp_smoke_test.py"),
                "url": "http://localhost:18100/mcp",
                "timeoutSeconds": 60,
                "metadataToolName": "metadatasearch",
                "metadataQueryArgument": "query",
                "metadataQueries": ["Конфигурации"],
                "codeToolName": "codesearch",
                "codeQueryArgument": "query",
                "codeQueries": ["Процедура"],
            },
        },
        "notifications": {
            "enabled": True,
            "onSuccess": False,
            "onFailure": True,
            "onRollback": True,
            "webhookUrlSecret": "MCP_UPDATE_WEBHOOK_URL",
        },
        "retention": {
            "keepPreviousIndexes": 1,
            "keepLogsDays": 30,
            "keepStagingBuilds": 2,
        },
        "rollback": {
            "preserveFailedIndex": True,
        },
    }

    strip_global_project_blocks(payload)

    config_path = tmp_path / "project.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    return config_path


def test_parse_args() -> None:
    options = parse_args(["--config", "project.json", "--force", "--dry-run"])

    assert options.config_path.name == "project.json"
    assert options.force is True
    assert options.storage_migration is False
    assert options.dry_run is True
    assert options.promote_existing_build is False


def test_parse_args_storage_migration() -> None:
    options = parse_args(["--config", "project.json", "--storage-migration"])

    assert options.storage_migration is True
    assert options.force is False


def test_parse_args_repair_metadata_index() -> None:
    options = parse_args(["--config", "project.json", "--repair-metadata-index"])

    assert options.repair_metadata_index is True
    assert options.force is False


def test_parse_args_storage_migration_rejects_force() -> None:
    try:
        parse_args(["--config", "project.json", "--storage-migration", "--force"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("Expected argparse SystemExit")


def test_parse_args_storage_migration_rejects_rollback() -> None:
    try:
        parse_args(["--config", "project.json", "--storage-migration", "--rollback"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("Expected argparse SystemExit")


def test_parse_args_storage_migration_rejects_promote_existing_build() -> None:
    try:
        parse_args(["--config", "project.json", "--storage-migration", "--promote-existing-build"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("Expected argparse SystemExit")


def test_parse_args_repair_metadata_index_rejects_conflicts() -> None:
    for flag in ("--force", "--storage-migration", "--rollback", "--promote-existing-build", "--dry-run"):
        try:
            parse_args(["--config", "project.json", "--repair-metadata-index", flag])
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError(f"Expected argparse SystemExit for {flag}")


def test_parse_args_promote_existing_build() -> None:
    options = parse_args(
        [
            "--config",
            "project.json",
            "--promote-existing-build",
            "--promote-commit",
            "abc123",
            "--promote-source-fingerprint",
            "fp",
            "--promote-report-hash",
            "hash",
        ]
    )

    assert options.promote_existing_build is True
    assert options.promote_commit == "abc123"
    assert options.promote_source_fingerprint == "fp"
    assert options.promote_report_hash == "hash"


def test_main_dry_run_returns_success(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    _mock_phase2_dependencies(monkeypatch)

    result = main(["--config", str(config_path), "--dry-run"])

    assert result == ExitCode.SUCCESS
    log_path = next((tmp_path / "logs").glob("*-update.log"))
    log_text = log_path.read_text(encoding="utf-8")
    assert f"Project root: {tmp_path}" in log_text
    assert f"MCP index storage root: {tmp_path / 'index-storage'}" in log_text
    assert "Production container: mcp-orders" in log_text
    assert "Build container: mcp-orders-build" in log_text
    assert "Production host port: 8100" in log_text
    assert "Build host port: 18100" in log_text
    assert "Production URL: http://localhost:8100/mcp" in log_text
    assert "Build URL: http://localhost:18100/mcp" in log_text


def test_main_cleans_untracked_repo_before_pull(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    order = []

    monkeypatch.setattr("mcp_project_updater.cli.ensure_repo_available", lambda repo, no_git_pull, env=None: None)
    monkeypatch.setattr(
        "mcp_project_updater.cli.validate_repo",
        lambda repo_path: RepoValidationResult(
            inside_work_tree=True,
            tracked_changes=[],
            untracked_changes=["?? generated.xml"],
        ),
    )

    def _clean(repo_path):
        order.append("clean")
        return ["Removing generated.xml"]

    def _determine(repo, no_git_pull, env=None):
        order.append("determine")
        return "abc123"

    monkeypatch.setattr("mcp_project_updater.cli.clean_untracked_changes", _clean)
    monkeypatch.setattr("mcp_project_updater.cli.determine_target_commit", _determine)

    result = main(["--config", str(config_path), "--dry-run"])

    assert result == ExitCode.SUCCESS
    assert order == ["clean", "determine"]


def test_main_discards_tracked_repo_changes_before_pull(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    order = []

    monkeypatch.setattr("mcp_project_updater.cli.ensure_repo_available", lambda repo, no_git_pull, env=None: None)
    monkeypatch.setattr(
        "mcp_project_updater.cli.validate_repo",
        lambda repo_path: RepoValidationResult(
            inside_work_tree=True,
            tracked_changes=[" M tracked.xml"],
            untracked_changes=["?? generated.xml"],
        ),
    )

    def _discard(repo_path):
        order.append("reset")
        return ["HEAD is now at abc123 commit"]

    def _clean(repo_path):
        order.append("clean")
        return ["Removing generated.xml"]

    def _determine(repo, no_git_pull, env=None):
        order.append("determine")
        return "abc123"

    monkeypatch.setattr("mcp_project_updater.cli.discard_tracked_changes", _discard)
    monkeypatch.setattr("mcp_project_updater.cli.clean_untracked_changes", _clean)
    monkeypatch.setattr("mcp_project_updater.cli.determine_target_commit", _determine)

    result = main(["--config", str(config_path), "--dry-run"])

    assert result == ExitCode.SUCCESS
    assert order == ["reset", "clean", "determine"]


def test_main_cleans_ignored_repo_files_before_pull_even_when_status_is_clean(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    order = []

    monkeypatch.setattr("mcp_project_updater.cli.ensure_repo_available", lambda repo, no_git_pull, env=None: None)
    monkeypatch.setattr(
        "mcp_project_updater.cli.validate_repo",
        lambda repo_path: RepoValidationResult(
            inside_work_tree=True,
            tracked_changes=[],
            untracked_changes=[],
        ),
    )

    def _clean(repo_path):
        order.append("clean")
        return ["Removing ignored.bin"]

    def _determine(repo, no_git_pull, env=None):
        order.append("determine")
        return "abc123"

    monkeypatch.setattr("mcp_project_updater.cli.clean_untracked_changes", _clean)
    monkeypatch.setattr("mcp_project_updater.cli.determine_target_commit", _determine)

    result = main(["--config", str(config_path), "--dry-run"])

    assert result == ExitCode.SUCCESS
    assert order == ["clean", "determine"]


def test_main_no_git_pull_leaves_untracked_repo_in_place(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)

    monkeypatch.setattr("mcp_project_updater.cli.ensure_repo_available", lambda repo, no_git_pull, env=None: None)
    monkeypatch.setattr(
        "mcp_project_updater.cli.validate_repo",
        lambda repo_path: RepoValidationResult(
            inside_work_tree=True,
            tracked_changes=[" M tracked.xml"],
            untracked_changes=["?? generated.xml"],
        ),
    )
    monkeypatch.setattr(
        "mcp_project_updater.cli.discard_tracked_changes",
        lambda repo_path: (_ for _ in ()).throw(AssertionError("reset must not run")),
    )
    monkeypatch.setattr(
        "mcp_project_updater.cli.clean_untracked_changes",
        lambda repo_path: (_ for _ in ()).throw(AssertionError("cleanup must not run")),
    )
    monkeypatch.setattr(
        "mcp_project_updater.cli.determine_target_commit",
        lambda repo, no_git_pull, env=None: "abc123",
    )

    result = main(["--config", str(config_path), "--dry-run", "--no-git-pull"])

    assert result == ExitCode.SUCCESS


def test_main_returns_success_for_mocked_full_workflow(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    _mock_phase2_dependencies(
        monkeypatch,
        create_report=True,
        complete_phase4=True,
        complete_phase5=True,
        complete_phase6=True,
    )

    result = main(["--config", str(config_path)])

    assert result == ExitCode.SUCCESS


def test_main_build_infrastructure_smoke_ignores_ready_patterns(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    expected_error_patterns = load_project_config(config_path).smoke_test.infrastructure.log_error_patterns
    captured = {}
    _mock_phase2_dependencies(
        monkeypatch,
        create_report=True,
        complete_phase4=True,
        complete_phase5=True,
        complete_phase6=True,
    )

    def _fake_infrastructure_smoke(smoke_config, context, runner):
        captured["ready_patterns"] = smoke_config.log_ready_patterns
        captured["error_patterns"] = smoke_config.log_error_patterns
        captured["timeout_seconds"] = smoke_config.timeout_seconds
        return type("SmokeResult", (), {"http_status_code": 404})()

    monkeypatch.setattr("mcp_project_updater.cli.run_infrastructure_smoke_test", _fake_infrastructure_smoke)

    result = main(["--config", str(config_path)])

    assert result == ExitCode.SUCCESS
    assert captured["ready_patterns"] == []
    assert captured["error_patterns"] == expected_error_patterns
    assert captured["timeout_seconds"] == 600


def test_main_returns_success_when_no_changes_and_not_forced(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    source_result = detect_sources(tmp_path / "repo", "src/cf", False, "src/cfe", False)
    source_fingerprint = compute_source_fingerprint(source_result)
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "last_indexed_commit").write_text("same-commit\n", encoding="utf-8")
    (state_root / "last_source_fingerprint").write_text(f"{source_fingerprint}\n", encoding="utf-8")
    current_report_path = tmp_path / "staging" / "current" / "metadata" / "Report.txt"
    current_report_path.parent.mkdir(parents=True, exist_ok=True)
    current_report_path.write_text("ok", encoding="utf-8")
    current_index_storage = tmp_path / "index-storage" / "current"
    current_index_storage.mkdir(parents=True, exist_ok=True)
    (current_index_storage / "db.bin").write_text("ok", encoding="utf-8")

    _mock_phase2_dependencies(monkeypatch, commit="same-commit")

    result = main(["--config", str(config_path)])

    assert result == ExitCode.SUCCESS


def test_main_repair_metadata_index_does_not_skip_unchanged_state_and_reuses_code(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    source_result = detect_sources(tmp_path / "repo", "src/cf", False, "src/cfe", False)
    source_fingerprint = compute_source_fingerprint(source_result)
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "last_indexed_commit").write_text("same-commit\n", encoding="utf-8")
    (state_root / "last_source_fingerprint").write_text(f"{source_fingerprint}\n", encoding="utf-8")
    current_report_path = tmp_path / "staging" / "current" / "metadata" / "Report.txt"
    current_report_path.parent.mkdir(parents=True, exist_ok=True)
    current_report_path.write_text("ok", encoding="utf-8")
    current_index_storage = tmp_path / "index-storage" / "current"
    current_index_storage.mkdir(parents=True, exist_ok=True)
    (current_index_storage / "db.bin").write_text("seed", encoding="utf-8")

    captured = {"repair_called": False}
    _mock_phase2_dependencies(monkeypatch, commit="same-commit", create_report=True)
    monkeypatch.setattr("mcp_project_updater.cli.ensure_docker_available", lambda: "26.1.0")

    def _fake_start_build_container(mcp_config, build_paths, paths_config, runner, **kwargs):
        captured.update(kwargs)
        return type("BuildContainerResult", (), {"command": ["docker", "run"], "container_id": "cid"})()

    def _fake_repair(url, timeout_seconds, retry_interval_seconds, require_code_index):
        captured["repair_called"] = True
        captured["repair_url"] = url
        captured["repair_require_code_index"] = require_code_index
        return type("RepairResult", (), {"metadata_count": 5, "code_count": 10})()

    monkeypatch.setattr("mcp_project_updater.cli.start_build_container", _fake_start_build_container)
    monkeypatch.setattr(
        "mcp_project_updater.cli.run_infrastructure_smoke_test",
        lambda smoke_config, context, runner: type("SmokeResult", (), {"http_status_code": 404})(),
    )
    monkeypatch.setattr("mcp_project_updater.cli.run_metadata_index_repair", _fake_repair)
    monkeypatch.setattr(
        "mcp_project_updater.cli.run_tool_smoke_test",
        lambda config, tool_smoke_config, working_directory, url: type("ToolSmokeResult", (), {"stdout": '{"ok":true}'})(),
    )
    monkeypatch.setattr("mcp_project_updater.cli.write_container_logs", lambda container_name, output_path, runner: output_path)
    monkeypatch.setattr(
        "mcp_project_updater.cli.perform_switch",
        lambda config, state_store, target_commit, production_log_path, docker_runner, storage_migration=False: type(
            "SwitchResult",
            (),
            {"target_commit": target_commit, "production_log_path": production_log_path},
        )(),
    )

    result = main(["--config", str(config_path), "--repair-metadata-index"])

    assert result == ExitCode.SUCCESS
    assert captured["seed_index_storage_from"] == current_index_storage
    assert captured["reset_database"] is False
    assert captured["index_metadata"] is True
    assert captured["index_code"] is False
    assert captured["index_help"] is False
    assert captured["repair_called"] is True
    assert captured["repair_url"] == "http://localhost:18100/mcp"
    assert captured["repair_require_code_index"] is True


def test_main_repair_metadata_index_requires_current_storage(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    _mock_phase2_dependencies(monkeypatch, create_report=True)
    monkeypatch.setattr(
        "mcp_project_updater.cli.start_build_container",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("build must not start")),
    )

    result = main(["--config", str(config_path), "--repair-metadata-index"])

    assert result == ExitCode.INVALID_STATE


def test_main_disables_build_tool_smoke_timeout_on_initial_bootstrap(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    seen = {"timeout_seconds": None}

    _mock_phase2_dependencies(
        monkeypatch,
        create_report=True,
        complete_phase4=True,
        complete_phase6=True,
    )

    def _fake_tool_smoke(config, tool_smoke_config, working_directory, url):
        seen["timeout_seconds"] = tool_smoke_config.timeout_seconds
        return type("ToolSmokeResult", (), {"stdout": '{"ok":true}'})()

    monkeypatch.setattr("mcp_project_updater.cli.run_tool_smoke_test", _fake_tool_smoke)

    result = main(["--config", str(config_path)])

    assert result == ExitCode.SUCCESS
    assert seen["timeout_seconds"] == 0


def test_main_reuses_current_chroma_for_build_when_metadata_changed(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    current_report = tmp_path / "staging" / "current" / "metadata" / "Report.txt"
    current_report.parent.mkdir(parents=True, exist_ok=True)
    current_report.write_text(
        '\t- РљРѕРЅС„РёРіСѓСЂР°С†РёРё.Orders\nРРјСЏ: "Orders"\nРЎРёРЅРѕРЅРёРј: "Orders"\n',
        encoding="utf-8",
    )
    current_index_storage = tmp_path / "index-storage" / "current"
    current_index_storage.mkdir(parents=True, exist_ok=True)
    (current_index_storage / "db.bin").write_text("seed", encoding="utf-8")
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "last_report_hash").write_text("different-report-hash\n", encoding="utf-8")

    captured = {}
    _mock_phase2_dependencies(monkeypatch, create_report=True)
    monkeypatch.setattr("mcp_project_updater.cli.ensure_docker_available", lambda: "26.1.0")

    def _fake_start_build_container(mcp_config, build_paths, paths_config, runner, **kwargs):
        captured.update(kwargs)
        return type("BuildContainerResult", (), {"command": ["docker", "run"], "container_id": "cid"})()

    monkeypatch.setattr("mcp_project_updater.cli.start_build_container", _fake_start_build_container)
    monkeypatch.setattr(
        "mcp_project_updater.cli.run_infrastructure_smoke_test",
        lambda smoke_config, context, runner: type("SmokeResult", (), {"http_status_code": 404})(),
    )
    monkeypatch.setattr(
        "mcp_project_updater.cli.run_tool_smoke_test",
        lambda config, tool_smoke_config, working_directory, url: type("ToolSmokeResult", (), {"stdout": '{"ok":true}'})(),
    )
    monkeypatch.setattr(
        "mcp_project_updater.cli.write_container_logs",
        lambda container_name, output_path, runner: output_path,
    )
    monkeypatch.setattr(
        "mcp_project_updater.cli.perform_switch",
        lambda config, state_store, target_commit, production_log_path, docker_runner, storage_migration=False: type(
            "SwitchResult",
            (),
            {"target_commit": target_commit, "production_log_path": production_log_path},
        )(),
    )

    result = main(["--config", str(config_path)])

    assert result == ExitCode.SUCCESS
    assert captured["reset_database"] is False
    assert captured["seed_index_storage_from"] == current_index_storage
    assert captured["index_metadata"] is None


def test_main_storage_migration_does_not_seed_from_current_storage(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    current_report = tmp_path / "staging" / "current" / "metadata" / "Report.txt"
    current_report.parent.mkdir(parents=True, exist_ok=True)
    current_report.write_text("ok", encoding="utf-8")
    current_index_storage = tmp_path / "index-storage" / "current"
    current_index_storage.mkdir(parents=True, exist_ok=True)
    (current_index_storage / "db.bin").write_text("seed", encoding="utf-8")

    captured = {"switch_storage_migration": None}
    _mock_phase2_dependencies(monkeypatch, create_report=True)
    monkeypatch.setattr("mcp_project_updater.cli.ensure_docker_available", lambda: "26.1.0")

    def _fake_start_build_container(mcp_config, build_paths, paths_config, runner, **kwargs):
        captured.update(kwargs)
        return type("BuildContainerResult", (), {"command": ["docker", "run"], "container_id": "cid"})()

    def _fake_switch(config, state_store, target_commit, production_log_path, docker_runner, storage_migration=False):
        captured["switch_storage_migration"] = storage_migration
        return type("SwitchResult", (), {"target_commit": target_commit, "production_log_path": production_log_path})()

    monkeypatch.setattr("mcp_project_updater.cli.start_build_container", _fake_start_build_container)
    monkeypatch.setattr(
        "mcp_project_updater.cli.run_infrastructure_smoke_test",
        lambda smoke_config, context, runner: type("SmokeResult", (), {"http_status_code": 404})(),
    )
    monkeypatch.setattr(
        "mcp_project_updater.cli.run_tool_smoke_test",
        lambda config, tool_smoke_config, working_directory, url: type("ToolSmokeResult", (), {"stdout": '{"ok":true}'})(),
    )
    monkeypatch.setattr("mcp_project_updater.cli.write_container_logs", lambda container_name, output_path, runner: output_path)
    monkeypatch.setattr("mcp_project_updater.cli.perform_switch", _fake_switch)

    result = main(["--config", str(config_path), "--storage-migration"])

    assert result == ExitCode.SUCCESS
    assert captured["seed_index_storage_from"] is None
    assert captured["reset_database"] is None
    assert captured["index_metadata"] is None
    assert captured["switch_storage_migration"] is True


def test_main_force_rebuild_does_not_seed_from_current_storage(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    current_report = tmp_path / "staging" / "current" / "metadata" / "Report.txt"
    current_report.parent.mkdir(parents=True, exist_ok=True)
    current_report.write_text("ok", encoding="utf-8")
    current_index_storage = tmp_path / "index-storage" / "current"
    current_index_storage.mkdir(parents=True, exist_ok=True)
    (current_index_storage / "db.bin").write_text("seed", encoding="utf-8")

    captured = {}
    _mock_phase2_dependencies(monkeypatch, create_report=True)
    monkeypatch.setattr("mcp_project_updater.cli.ensure_docker_available", lambda: "26.1.0")

    def _fake_start_build_container(mcp_config, build_paths, paths_config, runner, **kwargs):
        captured.update(kwargs)
        return type("BuildContainerResult", (), {"command": ["docker", "run"], "container_id": "cid"})()

    monkeypatch.setattr("mcp_project_updater.cli.start_build_container", _fake_start_build_container)
    monkeypatch.setattr(
        "mcp_project_updater.cli.run_infrastructure_smoke_test",
        lambda smoke_config, context, runner: type("SmokeResult", (), {"http_status_code": 404})(),
    )
    monkeypatch.setattr(
        "mcp_project_updater.cli.run_tool_smoke_test",
        lambda config, tool_smoke_config, working_directory, url: type("ToolSmokeResult", (), {"stdout": '{"ok":true}'})(),
    )
    monkeypatch.setattr("mcp_project_updater.cli.write_container_logs", lambda container_name, output_path, runner: output_path)
    monkeypatch.setattr(
        "mcp_project_updater.cli.perform_switch",
        lambda config, state_store, target_commit, production_log_path, docker_runner, storage_migration=False: type(
            "SwitchResult",
            (),
            {"target_commit": target_commit, "production_log_path": production_log_path},
        )(),
    )

    result = main(["--config", str(config_path), "--force"])

    assert result == ExitCode.SUCCESS
    assert captured["seed_index_storage_from"] is None
    assert captured["reset_database"] is None


def test_main_promotes_existing_build(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    build_report = tmp_path / "staging" / "build" / "metadata" / "Report.txt"
    build_report.parent.mkdir(parents=True)
    build_report.write_text(
        '\t- РљРѕРЅС„РёРіСѓСЂР°С†РёРё.Orders\nРРјСЏ: "Orders"\nРЎРёРЅРѕРЅРёРј: "Orders"\n',
        encoding="utf-8",
    )
    (tmp_path / "staging" / "build" / "diagnostics").mkdir(parents=True)
    (tmp_path / "index-storage" / "build").mkdir(parents=True)

    called = {"switch_commit": None, "ready_patterns": None}
    _mock_phase2_dependencies(monkeypatch, complete_phase4=True, complete_phase5=True)
    monkeypatch.setattr("mcp_project_updater.cli.ensure_docker_available", lambda: "26.1.0")
    monkeypatch.setattr(
        "mcp_project_updater.cli.perform_switch",
        lambda config, state_store, target_commit, production_log_path, docker_runner, storage_migration=False: called.__setitem__(
            "switch_commit", target_commit
        )
        or type("SwitchResult", (), {"target_commit": target_commit, "production_log_path": production_log_path})(),
    )
    monkeypatch.setattr(
        "mcp_project_updater.cli.validate_report",
        lambda report_path, report_config, diagnostics_path: type(
            "ReportResult",
            (),
            {"report_path": report_path, "report_size": report_path.stat().st_size},
        )(),
    )
    monkeypatch.setattr(
        "mcp_project_updater.cli.run_infrastructure_smoke_test",
        lambda smoke_config, context, runner: called.__setitem__("ready_patterns", smoke_config.log_ready_patterns)
        or type("SmokeResult", (), {"http_status_code": 405})(),
    )

    result = main(
        [
            "--config",
            str(config_path),
            "--promote-existing-build",
            "--promote-commit",
            "promoted-commit",
            "--promote-source-fingerprint",
            "source-fp",
            "--promote-report-hash",
            "report-hash",
        ]
    )

    assert result == ExitCode.SUCCESS
    assert called["switch_commit"] == "promoted-commit"
    assert called["ready_patterns"] == []
    assert (tmp_path / "state" / "last_source_fingerprint").read_text(encoding="utf-8").strip() == "source-fp"
    assert (tmp_path / "state" / "last_report_hash").read_text(encoding="utf-8").strip() == "report-hash"


def test_main_returns_success_when_source_fingerprint_matches_and_not_forced(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    source_result = detect_sources(tmp_path / "repo", "src/cf", False, "src/cfe", False)
    source_fingerprint = compute_source_fingerprint(source_result)
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "last_source_fingerprint").write_text(f"{source_fingerprint}\n", encoding="utf-8")
    current_report_path = tmp_path / "staging" / "current" / "metadata" / "Report.txt"
    current_report_path.parent.mkdir(parents=True, exist_ok=True)
    current_report_path.write_text("ok", encoding="utf-8")
    current_index_storage = tmp_path / "index-storage" / "current"
    current_index_storage.mkdir(parents=True, exist_ok=True)
    (current_index_storage / "db.bin").write_text("ok", encoding="utf-8")

    _mock_phase2_dependencies(monkeypatch, commit="new-commit")

    result = main(["--config", str(config_path)])

    assert result == ExitCode.SUCCESS


def test_main_returns_warning_when_success_notification_fails(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["notifications"]["onSuccess"] = True
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    _mock_phase2_dependencies(
        monkeypatch,
        create_report=True,
        complete_phase4=True,
        complete_phase5=True,
        complete_phase6=True,
    )
    monkeypatch.setattr(
        "mcp_project_updater.cli.send_notification",
        lambda *args, **kwargs: (_ for _ in ()).throw(Exception("boom")),
    )

    result = main(["--config", str(config_path)])

    assert result == ExitCode.SUCCESS_WITH_WARNINGS


def test_main_uses_native_report_without_running_parser(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["sources"]["nativeReportPath"] = "native/Report.txt"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    native_report_path = tmp_path / "repo" / "native" / "Report.txt"
    native_report_path.parent.mkdir(parents=True, exist_ok=True)
    native_report_path.write_text(
        '\t- Конфигурации.Orders\nИмя: "Orders"\nСиноним: "Orders"\n',
        encoding="utf-8",
    )

    _mock_phase2_dependencies(
        monkeypatch,
        complete_phase4=True,
        complete_phase5=True,
        complete_phase6=True,
    )
    monkeypatch.setattr(
        "mcp_project_updater.cli.run_parser",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("run_parser must not be called")),
    )

    result = main(["--config", str(config_path)])

    assert result == ExitCode.SUCCESS
    assert (tmp_path / "staging" / "build" / "metadata" / "Report.txt").read_text(encoding="utf-8") == (
        native_report_path.read_text(encoding="utf-8")
    )


def test_main_does_not_skip_when_native_report_changes_but_commit_is_same(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["sources"]["nativeReportPath"] = "native/Report.txt"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    native_report_path = tmp_path / "repo" / "native" / "Report.txt"
    native_report_path.parent.mkdir(parents=True, exist_ok=True)
    native_report_path.write_text(
        '\t- Конфигурации.Orders\nИмя: "Orders"\nСиноним: "Orders"\n',
        encoding="utf-8",
    )

    previous_report_path = tmp_path / "repo" / "native" / "previous-Report.txt"
    previous_report_path.write_text(
        '\t- Конфигурации.Orders\nИмя: "OldOrders"\nСиноним: "OldOrders"\n',
        encoding="utf-8",
    )
    previous_source_result = detect_sources(
        tmp_path / "repo",
        "src/cf",
        False,
        "src/cfe",
        False,
        "native/previous-Report.txt",
    )
    previous_source_fingerprint = compute_source_fingerprint(previous_source_result)

    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "last_indexed_commit").write_text("same-commit\n", encoding="utf-8")
    (state_root / "last_source_fingerprint").write_text(f"{previous_source_fingerprint}\n", encoding="utf-8")
    current_report_path = tmp_path / "staging" / "current" / "metadata" / "Report.txt"
    current_report_path.parent.mkdir(parents=True, exist_ok=True)
    current_report_path.write_text("ok", encoding="utf-8")
    current_index_storage = tmp_path / "index-storage" / "current"
    current_index_storage.mkdir(parents=True, exist_ok=True)
    (current_index_storage / "db.bin").write_text("ok", encoding="utf-8")

    _mock_phase2_dependencies(
        monkeypatch,
        commit="same-commit",
        complete_phase4=True,
        complete_phase5=True,
        complete_phase6=True,
    )
    monkeypatch.setattr(
        "mcp_project_updater.cli.run_parser",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("run_parser must not be called")),
    )

    result = main(["--config", str(config_path)])

    assert result == ExitCode.SUCCESS
    assert (tmp_path / "staging" / "build" / "metadata" / "Report.txt").read_text(encoding="utf-8") == (
        native_report_path.read_text(encoding="utf-8")
    )


def test_main_preserves_update_error_when_failure_notification_fails(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    _mock_phase2_dependencies(monkeypatch)
    monkeypatch.setattr(
        "mcp_project_updater.cli.run_parser",
        lambda *args, **kwargs: (_ for _ in ()).throw(UpdaterError("parser failed", ExitCode.PARSER_FAILED)),
    )
    monkeypatch.setattr(
        "mcp_project_updater.cli.send_notification",
        lambda *args, **kwargs: (_ for _ in ()).throw(Exception("boom")),
    )

    result = main(["--config", str(config_path)])

    assert result == ExitCode.PARSER_FAILED


def _mock_phase2_dependencies(
    monkeypatch,
    commit: str = "abc123",
    create_report: bool = False,
    complete_phase4: bool = False,
    complete_phase5: bool = False,
    complete_phase6: bool = False,
) -> None:
    monkeypatch.setattr(
        "mcp_project_updater.cli.ensure_repo_available",
        lambda repo, no_git_pull, env=None: None,
    )
    monkeypatch.setattr(
        "mcp_project_updater.cli.validate_repo",
        lambda repo_path: RepoValidationResult(
            inside_work_tree=True,
            tracked_changes=[],
            untracked_changes=[],
        ),
    )
    monkeypatch.setattr("mcp_project_updater.cli.clean_untracked_changes", lambda repo_path: [])
    monkeypatch.setattr(
        "mcp_project_updater.cli.determine_target_commit",
        lambda repo, no_git_pull, env=None: commit,
    )
    if create_report:
        def _fake_run_parser(parser_config, parser_config_path, *, verbose, working_directory):
            report_path = parser_config_path.parent / "metadata" / "Report.txt"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                '\t- Конфигурации.Orders\nИмя: "Orders"\nСиноним: "Orders"\n',
                encoding="utf-8",
            )
            return type("ParserResult", (), {"returncode": 0})()

        monkeypatch.setattr("mcp_project_updater.cli.run_parser", _fake_run_parser)
    if complete_phase4:
        monkeypatch.setattr("mcp_project_updater.cli.ensure_docker_available", lambda: "26.1.0")
        monkeypatch.setattr(
            "mcp_project_updater.cli.start_build_container",
            lambda mcp_config, build_paths, paths_config, runner, **kwargs: type(
                "BuildContainerResult",
                (),
                {"command": ["docker", "run"], "container_id": "cid"},
            )(),
        )
        monkeypatch.setattr(
            "mcp_project_updater.cli.run_infrastructure_smoke_test",
            lambda smoke_config, context, runner: type(
                "SmokeResult",
                (),
                {"http_status_code": 404},
            )(),
        )
        monkeypatch.setattr(
            "mcp_project_updater.cli.write_container_logs",
            lambda container_name, output_path, runner: output_path,
        )
    if complete_phase5:
        monkeypatch.setattr(
            "mcp_project_updater.cli.run_tool_smoke_test",
            lambda config, tool_smoke_config, working_directory, url: type(
                "ToolSmokeResult",
                (),
                {"stdout": '{"ok":true}'},
            )(),
        )
    if complete_phase6:
        monkeypatch.setattr(
            "mcp_project_updater.cli.perform_switch",
            lambda config, state_store, target_commit, production_log_path, docker_runner, storage_migration=False: type(
                "SwitchResult",
                (),
                {"target_commit": target_commit, "production_log_path": production_log_path},
            )(),
        )
