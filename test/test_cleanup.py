import importlib
import json
import sys
from dataclasses import replace
from unittest.mock import Mock

import pytest

from cleanup_plan import attach_file_metadata, execute_plans, plan_series
from delete_watched_episodes import discover_jellyfin, get_last_played_date, main, retention_days


def episode(id=1, tvdb=101, file=10):
    return {"id": id, "tvdbId": tvdb, "hasFile": True, "episodeFileId": file}


def plans_for(client, sonarr_data):
    plans, _ = plan_series(5, sonarr_data["episodes"], {101, 102})
    return attach_file_metadata(client, plans)[0]


def apply(client, plans, **kwargs):
    return execute_plans(client, plans, apply=True, revalidate=lambda p: True, **kwargs)


def test_preview_has_no_api_calls():
    plans, _ = plan_series(5, [episode()], {101})
    client = Mock()
    report = execute_plans(client, plans)
    assert report[0]["status"] == "preview"
    assert report[0]["episodes"] == ((1, 101),)
    assert report[0]["reason"]
    assert client.mock_calls == []


def test_shared_file_requires_every_episode():
    plans, reasons = plan_series(5, [episode(), episode(2, 102)], {101})
    assert not plans and reasons


def test_shared_file_is_deleted_once(client, sonarr_data):
    assert apply(client, plans_for(client, sonarr_data))[0]["status"] == "deleted"
    client.del_episode_file.assert_called_once_with(10)
    assert client.upd_episode.call_count == 2
    client.upd_series.assert_not_called()


@pytest.mark.parametrize("when", ["before_unmonitor", "before_delete"])
def test_stale_membership_blocks_deletion(client, sonarr_data, when):
    plans = plans_for(client, sonarr_data)
    changed = sonarr_data["episodes"] + [episode(3, 103)]
    client.get_episode.side_effect = (
        [changed] if when == "before_unmonitor" else [sonarr_data["episodes"], changed]
    )
    report = apply(client, plans)[0]
    assert report["status"] == ("blocked" if when == "before_unmonitor" else "unconfirmed")
    client.del_episode_file.assert_not_called()
    if when == "before_unmonitor":
        client.upd_episode.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", None),
        ("tvdbId", None),
        ("episodeFileId", 0),
        ("episodeFileId", True),
        ("hasFile", 1),
        ("hasFile", None),
        ("hasFile", False),
        ("seriesId", 6),
    ],
)
def test_missing_or_inconsistent_identifiers_fail_closed(field, value):
    item = episode()
    item[field] = value
    plans, reasons = plan_series(5, [item], {101})
    assert not plans and reasons


def test_no_file_episode_is_retained_without_invalidating_series():
    assert plan_series(5, [{**episode(), "hasFile": False, "episodeFileId": 0}], {101}) == ([], [])


def test_no_file_episode_with_duplicate_tvdb_id_blocks_series():
    items = [episode(), {**episode(2), "hasFile": False, "episodeFileId": 0}]
    assert not plan_series(5, items, {101})[0]


@pytest.mark.parametrize("items", [[episode(), episode()], [episode(), episode(2)]])
def test_duplicate_episode_metadata_fails_closed(items):
    assert not plan_series(5, items, {101})[0]


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", 11),
        ("seriesId", 6),
        ("path", None),
        ("dateAdded", ""),
        ("dateAdded", "bad"),
        ("dateAdded", "2025-01-01T00:00:00"),
        ("path", "relative.mkv"),
        ("size", -1),
        ("size", True),
        ("size", "1000"),
    ],
)
def test_invalid_file_fingerprint_is_excluded(client, sonarr_data, field, value):
    client.get_episode_file.return_value[field] = value
    plans, _ = plan_series(5, sonarr_data["episodes"], {101, 102})
    selected, reasons = attach_file_metadata(client, plans)
    assert not selected and reasons


@pytest.mark.parametrize(
    "field,value",
    [("path", "/replacement.mkv"), ("size", 2000), ("dateAdded", "2026-01-01T00:00:00Z")],
)
def test_replaced_file_blocks_deletion(client, sonarr_data, field, value):
    plans = plans_for(client, sonarr_data)
    client.get_episode_file.return_value[field] = value
    assert apply(client, plans)[0]["status"] == "blocked"
    client.upd_episode.assert_not_called()
    client.del_episode_file.assert_not_called()


def test_limit_checked_before_any_mutation(client, sonarr_data):
    plan = plans_for(client, sonarr_data)[0]
    plans = [plan, replace(plan, file_id=11)]
    client.reset_mock()
    with pytest.raises(ValueError, match="limit"):
        apply(client, plans, max_files=1)
    assert client.mock_calls == []


def test_byte_limit_checked_before_any_mutation(client, sonarr_data):
    plans = plans_for(client, sonarr_data)
    client.reset_mock()
    with pytest.raises(ValueError, match="limit"):
        apply(client, plans, max_bytes=999)
    assert client.mock_calls == []


@pytest.mark.parametrize("limit", [True, False, -1, 1.5])
def test_invalid_limits_rejected(limit):
    with pytest.raises(ValueError):
        execute_plans(Mock(), [], max_files=limit)
    with pytest.raises(ValueError):
        execute_plans(Mock(), [], max_bytes=limit)


def test_duplicate_operations_rejected(client, sonarr_data):
    plan = plans_for(client, sonarr_data)[0]
    with pytest.raises(ValueError, match="Duplicate"):
        apply(client, [plan, plan])
    client.upd_episode.assert_not_called()


def test_apply_requires_revalidator_and_fingerprint(client, sonarr_data):
    plans = plans_for(client, sonarr_data)
    with pytest.raises(ValueError, match="revalidation"):
        execute_plans(client, plans, apply=True)
    with pytest.raises(ValueError, match="fingerprint"):
        apply(client, [replace(plans[0], identity=None)])


@pytest.mark.parametrize("fresh", [False, None, 1])
def test_changed_watch_state_blocks_all_mutation(client, sonarr_data, fresh):
    plans = plans_for(client, sonarr_data)
    results = execute_plans(client, plans, apply=True, revalidate=lambda p: fresh)
    assert results[0]["status"] == "blocked"
    client.upd_episode.assert_not_called()
    client.del_episode_file.assert_not_called()


def test_changed_watch_state_after_unmonitor_blocks_delete(client, sonarr_data):
    plans = plans_for(client, sonarr_data)
    revalidate = Mock(side_effect=[True, False])
    results = execute_plans(client, plans, apply=True, revalidate=revalidate)
    assert results[0]["status"] == "unconfirmed"
    assert results[0]["unmonitor_calls_confirmed"] == 2
    assert results[0]["phase"] == "revalidation_before_delete"
    client.del_episode_file.assert_not_called()


def test_partial_failure_stops_remaining_operations(client, sonarr_data):
    plan = plans_for(client, sonarr_data)[0]
    plans = [plan, replace(plan, file_id=11)]
    client.del_episode_file.side_effect = TimeoutError("sensitive token")
    results = apply(client, plans)
    assert [r["status"] for r in results] == ["unconfirmed", "not_attempted"]
    assert results[0]["unmonitor_calls_confirmed"] == 2
    assert "sensitive token" not in json.dumps(results)
    client.del_episode_file.assert_called_once()


@pytest.mark.parametrize(
    "response", [None, {}, {"id": 1, "monitored": True}, {"id": 7, "monitored": False}]
)
def test_unmonitor_must_be_confirmed(client, sonarr_data, response):
    plans = plans_for(client, sonarr_data)
    client.upd_episode.side_effect = None
    client.upd_episode.return_value = response
    assert apply(client, plans)[0]["status"] == "unconfirmed"
    client.del_episode_file.assert_not_called()


@pytest.mark.parametrize("value", ["-1", "bad", "", "1.5", "36501"])
def test_invalid_retention_is_rejected(value):
    with pytest.raises(ValueError):
        retention_days({"DAYS_TO_DELETE": value})


def test_timestamp_without_timezone_is_not_evidence():
    assert get_last_played_date({"LastPlayedDate": "2026-01-01T12:00:00"}) is None
    assert str(get_last_played_date({"LastPlayedDate": "2026-01-01T12:00:00Z"})) == "2026-01-01"


def test_jellyfin_requires_explicit_user_before_client_import():
    with pytest.raises(ValueError, match="JELLYFIN_USER_ID"):
        discover_jellyfin({}, 2)


def test_cli_defaults_to_preview(capsys, env, client):
    assert (
        main(
            [], env=env, discover=lambda env: {99: {101, 102}}, client_factory=lambda *args: client
        )
        == 0
    )
    client.del_episode_file.assert_not_called()
    client.upd_episode.assert_not_called()
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "preview"
    assert report["planned_bytes"] == 1000
    assert report["results"][0]["identity"]["path"]


def test_cli_apply_refreshes_discovery(capsys, env, client):
    discover = Mock(side_effect=[{99: {101, 102}}, {}])
    assert main(["--apply"], env=env, discover=discover, client_factory=lambda *args: client) == 1
    assert json.loads(capsys.readouterr().out)["results"][0]["status"] == "blocked"
    assert discover.call_count == 2
    client.upd_episode.assert_not_called()


def test_cli_changed_series_mapping_blocks_apply(capsys, env, client):
    client.get_series.side_effect = [[{"id": 5, "tvdbId": 99}], [{"id": 6, "tvdbId": 99}]]
    assert (
        main(
            ["--apply"],
            env=env,
            discover=lambda env: {99: {101, 102}},
            client_factory=lambda *args: client,
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["results"][0]["status"] == "blocked"
    client.upd_episode.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("SONARR_URL", "ftp://bad"),
        ("PLEX_URL", "https://user:password@host"),
        ("WATCH_POLICY", ""),
        ("WATCH_POLICY", "any-user"),
        ("DEFAULT_DELETE", "yes"),
        ("MEDIA_SERVICE", "unknown"),
        ("DAYS_TO_DELETE", "invalid"),
    ],
)
def test_invalid_configuration_stops_before_network(env, client, capsys, field, value):
    env[field] = value
    discover = Mock()
    factory = Mock(return_value=client)
    assert main([], env=env, discover=discover, client_factory=factory) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "stopped"
    discover.assert_not_called()
    factory.assert_not_called()


def test_no_unique_sonarr_match_is_excluded(capsys, env, client):
    client.get_series.return_value *= 2
    assert (
        main([], env=env, discover=lambda env: {99: {101}}, client_factory=lambda *args: client)
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["results"] == [] and report["exclusions"]


def test_import_does_not_load_network_sdks():
    before = set(sys.modules)
    importlib.reload(sys.modules["delete_watched_episodes"])
    assert not ({"plexapi", "pyarr", "jellyfin_apiclient_python"} & (set(sys.modules) - before))
