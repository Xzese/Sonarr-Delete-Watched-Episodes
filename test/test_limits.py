import json
from unittest.mock import Mock

import pytest

from cleanup_plan import attach_file_metadata, execute_plans, plan_series
from delete_watched_episodes import main


@pytest.fixture
def large_plan(client, sonarr_data):
    episodes = [
        {**sonarr_data["episodes"][0], "id": i + 1, "tvdbId": 101 + i, "episodeFileId": 10 + i}
        for i in range(11)
    ]
    client.get_episode.return_value = episodes
    client.get_episode_file.side_effect = lambda file_id: {
        **sonarr_data["file"],
        "id": file_id,
        "path": f"/tv/episode-{file_id}.mkv",
    }
    plans, _ = plan_series(5, episodes, set(range(101, 112)))
    return attach_file_metadata(client, plans)[0]


def test_zero_file_limit_allows_more_than_default_cap(client, large_plan):
    with pytest.raises(ValueError, match="limit"):
        execute_plans(client, large_plan, apply=True, revalidate=lambda p: True)
    client.upd_episode.assert_not_called()
    results = execute_plans(client, large_plan, apply=True, max_files=0, revalidate=lambda p: True)
    assert all(result["status"] == "deleted" for result in results)
    assert client.del_episode_file.call_count == 11


def test_zero_byte_limit_allows_more_than_default_cap(client, sonarr_data):
    client.get_episode_file.return_value["size"] = 20_000_000_000
    plans, _ = plan_series(5, sonarr_data["episodes"], {101, 102})
    plans, _ = attach_file_metadata(client, plans)
    with pytest.raises(ValueError, match="limit"):
        execute_plans(client, plans, apply=True, revalidate=lambda p: True)
    client.upd_episode.assert_not_called()
    results = execute_plans(client, plans, apply=True, max_bytes=0, revalidate=lambda p: True)
    assert results[0]["status"] == "deleted"


@pytest.mark.parametrize(
    "limits", [{"max_files": 0, "max_bytes": 1000}, {"max_files": 1, "max_bytes": 0}]
)
def test_disabling_one_limit_preserves_the_other(client, large_plan, limits):
    with pytest.raises(ValueError, match="limit"):
        execute_plans(client, large_plan, apply=True, revalidate=lambda p: True, **limits)
    client.upd_episode.assert_not_called()
    client.del_episode_file.assert_not_called()


def test_disabled_caps_still_require_fresh_eligibility(client, large_plan):
    results = execute_plans(
        client,
        large_plan,
        apply=True,
        max_files=0,
        max_bytes=0,
        revalidate=lambda p: False,
    )
    assert results[0]["status"] == "blocked"
    assert all(result["status"] == "not_attempted" for result in results[1:])
    client.upd_episode.assert_not_called()
    client.del_episode_file.assert_not_called()


@pytest.mark.parametrize(
    "argv,settings",
    [
        (["--apply"], {"MAX_FILES": "0"}),
        (["--apply", "--max-files", "0"], {"MAX_FILES": "1"}),
    ],
)
def test_file_limit_zero_from_environment_or_cli(env, client, large_plan, capsys, argv, settings):
    env.update(settings)
    assert (
        main(
            ["--json", *argv],
            env=env,
            discover=lambda env: {99: set(range(101, 112))},
            client_factory=lambda *args: client,
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["limits"] == {"max_files": 0, "max_bytes": 10_000_000_000}
    assert client.del_episode_file.call_count == 11


@pytest.mark.parametrize(
    "argv,settings",
    [
        (["--apply"], {"MAX_BYTES": "0"}),
        (["--apply", "--max-bytes", "0"], {"MAX_BYTES": "1"}),
    ],
)
def test_byte_limit_zero_from_environment_or_cli(env, client, capsys, argv, settings):
    env.update(settings)
    client.get_episode_file.return_value["size"] = 20_000_000_000
    assert (
        main(
            ["--json", *argv],
            env=env,
            discover=lambda env: {99: {101, 102}},
            client_factory=lambda *args: client,
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["limits"] == {"max_files": 10, "max_bytes": 0}
    client.del_episode_file.assert_called_once_with(10)


def test_cli_can_reenable_a_disabled_environment_limit(env, client, capsys):
    env["MAX_BYTES"] = "0"
    assert (
        main(
            ["--json", "--apply", "--max-bytes", "999"],
            env=env,
            discover=lambda env: {99: {101, 102}},
            client_factory=lambda *args: client,
        )
        == 1
    )
    assert "limit" in json.loads(capsys.readouterr().out)["reason"]
    client.upd_episode.assert_not_called()
    client.del_episode_file.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("MAX_FILES", "-1"),
        ("MAX_FILES", ""),
        ("MAX_FILES", "1.5"),
        ("MAX_BYTES", "bad"),
        ("MAX_BYTES", "-1"),
        ("MAX_BYTES", ""),
    ],
)
def test_invalid_environment_limits_stop_before_network(env, capsys, field, value):
    env[field] = value
    discover, factory = Mock(), Mock()
    assert (
        main(
            [
                "--json",
            ],
            env=env,
            discover=discover,
            client_factory=factory,
        )
        == 1
    )
    assert field in json.loads(capsys.readouterr().out)["reason"]
    discover.assert_not_called()
    factory.assert_not_called()


@pytest.mark.parametrize("flag", ["--max-files", "--max-bytes"])
def test_negative_cli_limits_stop_before_network(env, capsys, flag):
    discover, factory = Mock(), Mock()
    assert main(["--json", flag, "-1"], env=env, discover=discover, client_factory=factory) == 1
    assert "non-negative" in json.loads(capsys.readouterr().out)["reason"]
    discover.assert_not_called()
    factory.assert_not_called()


def test_preview_reports_both_disabled_limits(env, client, capsys):
    env.update(MAX_FILES="0", MAX_BYTES="0")
    assert (
        main(
            [
                "--json",
            ],
            env=env,
            discover=lambda env: {99: {101, 102}},
            client_factory=lambda *args: client,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["limits"] == {"max_files": 0, "max_bytes": 0}
    client.upd_episode.assert_not_called()
    client.del_episode_file.assert_not_called()
