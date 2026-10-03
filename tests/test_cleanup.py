from unittest.mock import Mock
import importlib
import sys

import pytest
from cleanup_plan import execute_plans, plan_series
from delete_watched_episodes import main, retention_days, get_last_played_date, discover_jellyfin


def episode(id=1, tvdb=101, file=10):
    return {"id": id, "tvdbId": tvdb, "hasFile": True, "episodeFileId": file}


def test_preview_has_no_api_calls():
    plans, _ = plan_series(5, [episode()], {101})
    client = Mock()
    assert execute_plans(client, plans) == [{"file_id": 10, "status": "preview"}]
    assert client.mock_calls == []


def test_shared_file_requires_every_episode():
    plans, reasons = plan_series(5, [episode(), episode(2, 102)], {101})
    assert not plans and reasons


def test_shared_file_is_deleted_once():
    episodes = [episode(), episode(2, 102)]
    plans, _ = plan_series(5, episodes, {101, 102})
    client = Mock()
    client.get_episode.return_value = episodes
    assert execute_plans(client, plans, apply=True)[0]["status"] == "deleted"
    client.del_episode_file.assert_called_once_with(10)
    assert client.upd_episode.call_count == 2
    client.upd_series.assert_not_called()


def test_stale_membership_blocks_deletion():
    plans, _ = plan_series(5, [episode()], {101})
    client = Mock()
    client.get_episode.return_value = [episode(), episode(2, 102)]
    assert execute_plans(client, plans, apply=True)[0]["status"] == "blocked"
    client.del_episode_file.assert_not_called()
    client.upd_episode.assert_not_called()


@pytest.mark.parametrize("field,value", [("id", None), ("tvdbId", None), ("episodeFileId", 0), ("episodeFileId", True)])
def test_missing_identifiers_fail_closed(field, value):
    item = episode()
    item[field] = value
    plans, reasons = plan_series(5, [item], {101})
    assert not plans and reasons


def test_duplicate_episode_metadata_fails_closed():
    assert not plan_series(5, [episode(), episode()], {101})[0]


def test_limit_checked_before_any_mutation():
    plans, _ = plan_series(5, [episode(), episode(2, 102, 11)], {101, 102})
    client = Mock()
    with pytest.raises(ValueError):
        execute_plans(client, plans, apply=True, max_files=1)
    assert client.mock_calls == []


def test_partial_failure_stops_remaining_operations():
    episodes = [episode(), episode(2, 102, 11)]
    plans, _ = plan_series(5, episodes, {101, 102})
    client = Mock()
    client.get_episode.return_value = episodes
    client.del_episode_file.side_effect = TimeoutError()
    results = execute_plans(client, plans, apply=True)
    assert [r["status"] for r in results] == ["unconfirmed", "not_attempted"]
    assert results[0]["unmonitor_calls_confirmed"] == 1
    assert client.del_episode_file.call_count == 1


@pytest.mark.parametrize("value", ["-1", "bad", "", "1.5"])
def test_invalid_retention_is_rejected(value):
    with pytest.raises(ValueError):
        retention_days({"DAYS_TO_DELETE": value})


def test_timestamp_without_timezone_is_not_evidence():
    assert get_last_played_date({"LastPlayedDate": "2026-01-01T12:00:00"}) is None
    assert str(get_last_played_date({"LastPlayedDate": "2026-01-01T12:00:00Z"})) == "2026-01-01"


def test_jellyfin_requires_explicit_user_before_client_import():
    with pytest.raises(ValueError, match="JELLYFIN_USER_ID"):
        discover_jellyfin({}, 2)


def test_cli_defaults_to_preview(capsys):
    client = Mock()
    client.get_series.return_value = [{"id": 5}]
    client.get_episode.return_value = [episode()]
    assert main([], env={"SONARR_URL": "https://sonarr.invalid", "SONARR_KEY": "fixture"}, discover=lambda env: {99: {101}}, client_factory=lambda *args: client) == 0
    client.del_episode_file.assert_not_called()
    client.upd_episode.assert_not_called()
    assert '"preview"' in capsys.readouterr().out


def test_import_does_not_load_network_sdks():
    before = set(sys.modules)
    importlib.reload(sys.modules["delete_watched_episodes"])
    assert not ({"plexapi", "pyarr", "jellyfin_apiclient_python"} & (set(sys.modules) - before))
