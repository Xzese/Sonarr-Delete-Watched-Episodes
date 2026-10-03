import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

import delete_watched_episodes as runner
from run_logging import configure_logging


def preview(env, client):
    return runner.main(
        [], env=env, discover=lambda env: {99: {101, 102}}, client_factory=lambda *args: client
    )


def test_file_log_preserves_json_console_report(env, client, capsys):
    assert preview(env, client) == 0
    report = json.loads(capsys.readouterr().out)
    contents = Path(env["LOG_FILE"]).read_text()
    assert "[INFO] Cleanup preview started." in contents
    assert json.dumps(report) in contents
    assert report["results"][0]["status"] == "preview"
    client.upd_episode.assert_not_called()
    client.del_episode_file.assert_not_called()


def test_partial_failure_is_logged_without_raw_sdk_credentials(env, client, capsys):
    client.del_episode_file.side_effect = TimeoutError("fixture-secret-token")
    assert (
        runner.main(
            ["--apply"],
            env=env,
            discover=lambda env: {99: {101, 102}},
            client_factory=lambda *args: client,
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    contents = Path(env["LOG_FILE"]).read_text()
    assert report["results"][0]["status"] == "unconfirmed"
    assert "[ERROR]" in contents and json.dumps(report) in contents
    assert "fixture-secret-token" not in contents
    assert env["SONARR_KEY"] not in contents and env["PLEX_TOKEN"] not in contents


def test_configuration_error_is_logged_before_network(env, capsys):
    env["DAYS_TO_DELETE"] = "invalid"
    discover = Mock()
    assert runner.main([], env=env, discover=discover) == 1
    report = json.loads(capsys.readouterr().out)
    contents = Path(env["LOG_FILE"]).read_text()
    assert "[ERROR]" in contents and json.dumps(report) in contents
    discover.assert_not_called()


@pytest.mark.parametrize("level", ["ERROR", "warning"])
def test_log_level_filters_file_messages_without_filtering_stdout(env, client, capsys, level):
    env["LOG_LEVEL"] = level
    assert preview(env, client) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "preview"
    assert Path(env["LOG_FILE"]).read_text() == ""
    env["DAYS_TO_DELETE"] = "invalid"
    assert preview(env, client) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "stopped"
    assert "[ERROR]" in Path(env["LOG_FILE"]).read_text()


@pytest.mark.parametrize(
    "field,value",
    [
        ("LOG_FILE", ""),
        ("LOG_LEVEL", "unknown"),
        ("LOG_RETENTION_WEEKS", "bad"),
        ("LOG_RETENTION_WEEKS", "-1"),
    ],
)
def test_invalid_logging_configuration_stops_before_network(env, capsys, field, value):
    env[field] = value
    discover, factory = Mock(), Mock()
    assert runner.main([], env=env, discover=discover, client_factory=factory) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "stopped" and field in report["reason"]
    discover.assert_not_called()
    factory.assert_not_called()


def test_unwritable_log_path_stops_before_network(env, tmp_path, capsys):
    parent = tmp_path / "not-a-directory"
    parent.write_text("fixture")
    env["LOG_FILE"] = str(parent / "log.txt")
    discover, factory = Mock(), Mock()
    assert runner.main([], env=env, discover=discover, client_factory=factory) == 1
    assert "Cannot create" in json.loads(capsys.readouterr().out)["reason"]
    discover.assert_not_called()
    factory.assert_not_called()


def test_repeated_runs_close_handlers_without_duplicate_records(env, client, capsys, monkeypatch):
    loggers = []

    def configure(env):
        pair = configure_logging(env)
        loggers.append(pair)
        return pair

    monkeypatch.setattr(runner, "configure_logging", configure)
    root_handlers = list(logging.getLogger().handlers)
    for _ in range(2):
        assert preview(env, client) == 0
        json.loads(capsys.readouterr().out)
    contents = Path(env["LOG_FILE"]).read_text()
    assert contents.count("Cleanup preview started.") == 2
    assert contents.count('"mode": "preview"') == 2
    assert all(not logger.handlers and handler.stream is None for logger, handler in loggers)
    assert logging.getLogger().handlers == root_handlers


@pytest.mark.parametrize("retention", [0, 2])
def test_weekly_rotation_preserves_records_and_honours_backup_retention(env, retention):
    env["LOG_RETENTION_WEEKS"] = str(retention)
    logger, handler = configure_logging(env)
    path = Path(env["LOG_FILE"])
    try:
        for suffix in ("2010-01-04", "2010-01-11", "2010-01-18"):
            path.with_name(f"{path.name}.{suffix}").write_text("old backup")
        logger.info("Before rotation.")
        handler.doRollover()
        logger.info("After rotation.")
        backups = list(path.parent.glob(f"{path.name}.*"))
        assert len(backups) == (4 if retention == 0 else retention)
        assert any("Before rotation." in backup.read_text() for backup in backups)
        assert "After rotation." in path.read_text()
        assert "Before rotation." not in path.read_text()
    finally:
        logger.removeHandler(handler)
        handler.close()


def test_imports_create_no_log_files_or_handlers(tmp_path):
    source = Path(__file__).resolve().parents[1] / "src"
    log_file = tmp_path / "output" / "log.txt"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import logging, sys; before = list(logging.getLogger().handlers); "
                "import run_logging, delete_watched_episodes; "
                "assert logging.getLogger().handlers == before; "
                "assert not {'plexapi', 'pyarr', 'jellyfin_apiclient_python'} & set(sys.modules)"
            ),
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(source), "LOG_FILE": str(log_file)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not log_file.parent.exists()


def test_help_does_not_create_logs(env, capsys):
    with pytest.raises(SystemExit) as result:
        runner.main(["--help"], env=env)
    assert result.value.code == 0
    assert "--apply" in capsys.readouterr().out
    assert not Path(env["LOG_FILE"]).parent.exists()
