import json
import xml.etree.ElementTree as ET
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

import delete_watched_episodes as runner
from cleanup_plan import attach_file_metadata, execute_plans, plan_series
from sonarr_client import create_sonarr_client

NOW = datetime(2026, 1, 3, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz)

    monkeypatch.setattr(runner, "datetime", Clock)


@pytest.fixture
def plex_client(monkeypatch):
    from plexapi.video import Episode, Show

    root = ET.parse(Path(__file__).parent / "fixtures" / "plex.xml").getroot()
    server = Mock()
    show = Show(server, root[0], initpath=root[0].get("key"))
    show._initpath = show._details_key
    episodes = [Episode(server, item, initpath=item.get("key")) for item in root[1:]]
    for episode in episodes:
        monkeypatch.setattr(episode, "show", lambda: show)
    server.library.section.return_value.search.return_value = episodes
    factory = Mock(return_value=server)
    monkeypatch.setattr("plexapi.server.PlexServer", factory)
    return server, episodes, show, factory


def test_plex_xml_fixture_and_scoped_search(env, plex_client):
    server, _, _, factory = plex_client
    assert runner.discover_plex(env, 2) == {99: {101, 102}}
    factory.assert_called_once_with("https://plex.invalid", "fixture-token", timeout=30)
    server.library.section.return_value.search.assert_called_once_with(libtype="episode")


@pytest.mark.parametrize(
    "age,qualifies",
    [(timedelta(days=2, seconds=1), True), (timedelta(days=2), False), (timedelta(days=1), False)],
)
def test_plex_retention_uses_episode_timestamp(env, plex_client, age, qualifies):
    _, episodes, _, _ = plex_client
    episodes[0]._data.set("lastViewedAt", str(int((NOW - age).timestamp())))
    eligible = runner.discover_plex(env, 2)
    assert (101 in eligible.get(99, set())) is qualifies


@pytest.mark.parametrize(
    "attribute,value", [("viewCount", "0"), ("viewOffset", "1000"), ("lastViewedAt", "bad")]
)
def test_plex_missing_or_incomplete_watch_state_is_retained(env, plex_client, attribute, value):
    _, episodes, _, _ = plex_client
    episodes[0]._data.set(attribute, value)
    episodes[0]._loadData(episodes[0]._data)
    assert 101 not in runner.discover_plex(env, 2).get(99, set())


def test_plex_keep_genre_wins_even_with_delete(env, plex_client):
    _, _, show, _ = plex_client
    show.genres.append(
        type(show.genres[0])(show._server, ET.fromstring('<Genre tag="Keep" />'), parent=show)
    )
    assert not runner.discover_plex(env, 2)


def test_plex_opt_in_and_opt_out_genre_policy(env, plex_client):
    _, _, show, _ = plex_client
    show.genres = []
    assert not runner.discover_plex(env, 2)
    env["DEFAULT_DELETE"] = "true"
    assert runner.discover_plex(env, 2) == {99: {101, 102}}


def test_plex_duplicate_episode_mapping_is_retained(env, plex_client):
    server, episodes, _, _ = plex_client
    server.library.section.return_value.search.return_value += [episodes[0]]
    assert runner.discover_plex(env, 2) == {99: {102}}


def test_plex_duplicate_series_mapping_is_retained(env, plex_client, monkeypatch):
    from plexapi.video import Show

    _, episodes, show, _ = plex_client
    duplicate_data = deepcopy(show._data)
    duplicate_data.set("ratingKey", "6")
    duplicate = Show(show._server, duplicate_data, initpath=duplicate_data.get("key"))
    duplicate._initpath = duplicate._details_key
    monkeypatch.setattr(episodes[1], "show", lambda: duplicate)
    assert not runner.discover_plex(env, 2)


def test_plex_ambiguous_guid_is_retained(env, plex_client):
    _, episodes, _, _ = plex_client
    episodes[0].guids.append(
        type(episodes[0].guids[0])(episodes[0]._server, ET.fromstring('<Guid id="tvdb://999" />'))
    )
    assert runner.discover_plex(env, 2) == {99: {102}}


def jellyfin_client(monkeypatch, data_by_user):
    client = Mock()
    client.config.data = {"auth.server": "https://jellyfin.invalid"}

    def get(path, params):
        user = path.split("/")[1]
        data = data_by_user[user]
        return deepcopy(data["series" if params["IncludeItemTypes"] == "Series" else "episodes"])

    client.jellyfin._get.side_effect = get
    monkeypatch.setattr("jellyfin_apiclient_python.JellyfinClient", Mock(return_value=client))
    return client


@pytest.fixture
def fallback_library(env, client, sonarr_data, jellyfin_data, monkeypatch):
    env["MEDIA_SERVICE"] = "jellyfin"
    show = jellyfin_data["series"]["Items"][0]
    show.update(Path="/jellyfin/Fixture", Name="Fixture")
    show["ProviderIds"]["Imdb"] = "tt1234567"
    series = sonarr_data["series"][0]
    series.update(path="/sonarr/Fixture", imdbId="tt1234567")
    files = []
    for number, (episode, sonarr_episode) in enumerate(
        zip(jellyfin_data["episodes"]["Items"], sonarr_data["episodes"]), 1
    ):
        relative = f"Season 1/Fixture S01E{number:02}.mkv"
        path = f"/jellyfin/Fixture/{relative}"
        episode.update(
            Name=f"Episode {number}",
            ParentIndexNumber=1,
            IndexNumber=number,
            Path=path,
            MediaSources=[{"Path": path, "Size": 1000, "IsRemote": False}],
        )
        sonarr_episode.update(seasonNumber=1, episodeNumber=number, episodeFileId=9 + number)
        files.append(
            {
                **sonarr_data["file"],
                "id": 9 + number,
                "path": f"/sonarr/Fixture/{relative}",
                "relativePath": relative,
            }
        )
    jellyfin_data["episodes"]["Items"][0]["ProviderIds"].pop("Tvdb")
    client.get_series.side_effect = lambda **kwargs: deepcopy(sonarr_data["series"])
    client.get_episode.side_effect = lambda **kwargs: deepcopy(sonarr_data["episodes"])
    client.get_episode_file.side_effect = lambda id_, series=False: (
        deepcopy(files) if series else deepcopy(next(f for f in files if f["id"] == id_))
    )
    jellyfin_client(monkeypatch, {"a" * 32: jellyfin_data})
    return jellyfin_data, sonarr_data, files


@pytest.mark.parametrize("missing", ["episode", "series", "both"])
def test_jellyfin_fallback_preview_matches_files_across_container_roots(
    env, client, fallback_library, capsys, missing
):
    data, _, _ = fallback_library
    if missing in {"series", "both"}:
        data["series"]["Items"][0]["ProviderIds"].pop("Tvdb")
    if missing == "series":
        data["episodes"]["Items"][0]["ProviderIds"]["Tvdb"] = "101"
    assert runner.main(["--json"], env=env, client_factory=lambda *args: client) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["planned_files"] == 2
    assert {r["file_id"] for r in report["results"]} == {10, 11}
    assert all(r["status"] == "preview" for r in report["results"])
    assert not report["exclusions"]
    client.upd_episode.assert_not_called()
    client.del_episode_file.assert_not_called()


@pytest.mark.parametrize(
    "problem",
    [
        "size",
        "path",
        "number",
        "range",
        "series-duplicate",
        "episode-duplicate",
        "file-duplicate",
        "shared-file",
        "conflicting-id",
    ],
)
def test_jellyfin_fallback_refuses_incomplete_or_ambiguous_identity(
    env, client, fallback_library, capsys, problem
):
    data, sonarr, files = fallback_library
    episode = data["episodes"]["Items"][0]
    data["episodes"]["Items"][1]["UserData"]["Played"] = False
    if problem == "size":
        episode["MediaSources"][0]["Size"] += 1
    elif problem == "path":
        episode["Path"] = episode["MediaSources"][0]["Path"] = "/jellyfin/Fixture/other.mkv"
    elif problem == "number":
        episode["IndexNumber"] = 2
    elif problem == "range":
        episode["IndexNumberEnd"] = 2
    elif problem == "series-duplicate":
        sonarr["series"].append({**sonarr["series"][0], "id": 6})
    elif problem == "episode-duplicate":
        sonarr["episodes"].append({**sonarr["episodes"][0], "id": 3, "tvdbId": 103})
    elif problem == "file-duplicate":
        files.append({**files[0], "id": 12})
    elif problem == "shared-file":
        sonarr["episodes"][1]["episodeFileId"] = 10
    else:
        data["series"]["Items"][0]["ProviderIds"].pop("Tvdb")
        episode["ProviderIds"]["Tvdb"] = "999"
    assert runner.main(["--json", "--apply"], env=env, client_factory=lambda *args: client) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["planned_files"] == 0
    assert report["results"] == []
    client.upd_episode.assert_not_called()
    client.del_episode_file.assert_not_called()


@pytest.mark.parametrize(
    "duplicate_identity",
    ["missing-id", "tvdb", "conflicting-number", "conflicting-id", "incomplete-source"],
)
def test_jellyfin_fallback_unwatched_duplicate_blocks_deletion(
    env, client, fallback_library, capsys, duplicate_identity
):
    data, _, _ = fallback_library
    data["episodes"]["Items"][1]["UserData"]["Played"] = False
    duplicate = deepcopy(data["episodes"]["Items"][0])
    duplicate["Id"] = "duplicate-item"
    duplicate["UserData"]["Played"] = False
    if duplicate_identity == "tvdb":
        duplicate["ProviderIds"]["Tvdb"] = "101"
    elif duplicate_identity == "conflicting-number":
        duplicate["IndexNumber"] = 2
    elif duplicate_identity == "conflicting-id":
        duplicate["ProviderIds"]["Tvdb"] = "999"
    elif duplicate_identity == "incomplete-source":
        duplicate.pop("MediaSources")
    data["episodes"]["Items"].append(duplicate)
    data["episodes"]["TotalRecordCount"] = 3
    assert runner.main(["--json", "--apply"], env=env, client_factory=lambda *args: client) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["planned_files"] == 0
    assert any("duplicate" in reason for reason in report["exclusions"])
    client.upd_episode.assert_not_called()
    client.del_episode_file.assert_not_called()


@pytest.mark.parametrize("second_state", ["watched", "unwatched", "conflicting-series"])
def test_jellyfin_fallback_all_selected_requires_every_user(
    env, client, fallback_library, monkeypatch, capsys, second_state
):
    data, _, _ = fallback_library
    data["series"]["Items"][0]["ProviderIds"].pop("Tvdb")
    data["episodes"]["Items"][1]["UserData"]["Played"] = False
    second = deepcopy(data)
    second["episodes"]["Items"][0]["UserData"]["Played"] = second_state != "unwatched"
    if second_state == "conflicting-series":
        second["series"]["Items"][0]["ProviderIds"]["Imdb"] = "tt7654321"
    env.pop("JELLYFIN_USER_ID")
    env.update(WATCH_POLICY="all-selected", JELLYFIN_USER_IDS=f"{'a' * 32},{'b' * 32}")
    jellyfin_client(monkeypatch, {"a" * 32: data, "b" * 32: second})
    assert runner.main(["--json"], env=env, client_factory=lambda *args: client) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["planned_files"] == int(second_state == "watched")
    client.upd_episode.assert_not_called()
    client.del_episode_file.assert_not_called()


@pytest.mark.parametrize("when", ["before-unmonitor", "before-delete"])
def test_jellyfin_fallback_revalidates_file_evidence_before_each_mutation(
    env, client, fallback_library, capsys, when
):
    data, _, _ = fallback_library
    episode = data["episodes"]["Items"][0]
    data["episodes"]["Items"][1]["UserData"]["Played"] = False
    if when == "before-unmonitor":
        original_get = client.get_episode_file.side_effect

        def get_file(id_, series=False):
            result = original_get(id_, series=series)
            if not series:
                episode["MediaSources"][0]["Size"] += 1
            return result

        client.get_episode_file.side_effect = get_file
    else:

        def update(id_, payload):
            episode["MediaSources"][0]["Size"] += 1
            return {"id": id_, **payload}

        client.upd_episode.side_effect = update
    assert runner.main(["--json", "--apply"], env=env, client_factory=lambda *args: client) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["results"][0]["status"] == (
        "blocked" if when == "before-unmonitor" else "unconfirmed"
    )
    client.del_episode_file.assert_not_called()
    if when == "before-unmonitor":
        client.upd_episode.assert_not_called()
    else:
        assert report["results"][0]["unmonitor_calls_confirmed"] == 1


def test_jellyfin_fixture_and_user_specific_query(env, jellyfin_data, monkeypatch):
    client = jellyfin_client(monkeypatch, {"a" * 32: jellyfin_data})
    assert runner.discover_jellyfin(env, 2) == {99: {101, 102}}
    for call in client.jellyfin._get.call_args_list:
        assert call.args[0] == f"Users/{'a' * 32}/Items"
        assert call.kwargs["params"]["EnableUserData"] == "true"
    client.http.stop_session.assert_called_once()
    episode = jellyfin_data["episodes"]["Items"][0]
    episode.update(Name="Pilot", ParentIndexNumber=2, IndexNumber=1, SeriesName="Fixture Show")
    episode["ProviderIds"].pop("Tvdb")
    eligible = runner.discover_jellyfin(env, 2)
    assert eligible == {99: {102}}
    assert any(
        "'Pilot (S02E01)'" in reason and "missing episode TVDB identifier" in reason
        for reason in eligible.exclusions
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("Played", False),
        ("Played", None),
        ("IsFavorite", True),
        ("IsFavorite", None),
        ("PlaybackPositionTicks", 50),
        ("PlaybackPositionTicks", None),
        ("PlaybackPositionTicks", False),
        ("LastPlayedDate", "2026-01-01T12:00:00Z"),
        ("LastPlayedDate", "2025-01-01T12:00:00"),
        ("LastPlayedDate", "bad"),
    ],
)
def test_jellyfin_conservative_watch_metadata(env, jellyfin_data, monkeypatch, field, value):
    jellyfin_data["episodes"]["Items"][0]["UserData"][field] = value
    jellyfin_client(monkeypatch, {"a" * 32: jellyfin_data})
    assert runner.discover_jellyfin(env, 2) == {99: {102}}
    jellyfin_data["episodes"]["Items"][0]["ProviderIds"].pop("Tvdb")
    eligible = runner.discover_jellyfin(env, 2)
    assert eligible == {99: {102}}
    assert not any("missing episode TVDB identifier" in reason for reason in eligible.exclusions)


@pytest.mark.parametrize("watched", [False, True])
@pytest.mark.parametrize(
    "problem", ["missing-episode-id", "missing-series-id", "duplicate-series", "duplicate-episode"]
)
def test_jellyfin_activity_warns_only_for_watch_eligible_episodes(
    env, client, jellyfin_data, monkeypatch, capsys, watched, problem
):
    env["MEDIA_SERVICE"] = "jellyfin"
    for episode in jellyfin_data["episodes"]["Items"]:
        episode["UserData"]["Played"] = watched
    if problem == "missing-episode-id":
        jellyfin_data["episodes"]["Items"][0]["ProviderIds"].pop("Tvdb")
        warning = "missing episode TVDB identifier"
    elif problem == "missing-series-id":
        jellyfin_data["series"]["Items"][0]["ProviderIds"].pop("Tvdb")
        warning = "no series TVDB mapping"
    elif problem == "duplicate-series":
        duplicate = deepcopy(jellyfin_data["series"]["Items"][0])
        duplicate["Id"] = "duplicate-series"
        jellyfin_data["series"]["Items"].append(duplicate)
        jellyfin_data["series"]["TotalRecordCount"] = 2
        warning = "duplicate series TVDB mapping"
    else:
        duplicate = deepcopy(jellyfin_data["episodes"]["Items"][0])
        duplicate["Id"] = "duplicate-episode"
        jellyfin_data["episodes"]["Items"].append(duplicate)
        jellyfin_data["episodes"]["TotalRecordCount"] = 3
        warning = "duplicate TVDB mapping"
    jellyfin_client(monkeypatch, {"a" * 32: jellyfin_data})
    assert runner.main([], env=env, client_factory=lambda *args: client) == 0
    for output in (capsys.readouterr().out, Path(env["LOG_FILE"]).read_text()):
        assert (warning in output) is watched
        assert ("[WARNING]" in output) is watched
        assert "Dry run complete: 0 file(s)" in output
    client.upd_episode.assert_not_called()
    client.del_episode_file.assert_not_called()


def test_jellyfin_series_favourite_protects_every_episode(env, jellyfin_data, monkeypatch):
    jellyfin_data["series"]["Items"][0]["UserData"]["IsFavorite"] = True
    jellyfin_client(monkeypatch, {"a" * 32: jellyfin_data})
    assert not runner.discover_jellyfin(env, 2)


def test_jellyfin_duplicate_tvdb_episode_mapping_is_retained(env, jellyfin_data, monkeypatch):
    duplicate = deepcopy(jellyfin_data["episodes"]["Items"][0])
    duplicate["Id"] = "duplicate-episode"
    duplicate["UserData"]["Played"] = False
    jellyfin_data["episodes"]["Items"].append(duplicate)
    jellyfin_data["episodes"]["TotalRecordCount"] = 3
    jellyfin_client(monkeypatch, {"a" * 32: jellyfin_data})
    eligible = runner.discover_jellyfin(env, 2)
    assert eligible == {99: {102}}
    assert any("duplicate TVDB mapping" in reason for reason in eligible.exclusions)


def test_jellyfin_duplicate_series_mapping_is_retained(env, jellyfin_data, monkeypatch):
    duplicate = deepcopy(jellyfin_data["series"]["Items"][0])
    duplicate["Id"] = "duplicate-series"
    jellyfin_data["series"]["Items"].append(duplicate)
    jellyfin_data["series"]["TotalRecordCount"] = 2
    jellyfin_client(monkeypatch, {"a" * 32: jellyfin_data})
    eligible = runner.discover_jellyfin(env, 2)
    assert not eligible
    assert any("duplicate series TVDB mapping" in reason for reason in eligible.exclusions)


@pytest.mark.parametrize("protected", ["unwatched", "favourite", "invisible"])
def test_jellyfin_all_selected_requires_every_user(env, jellyfin_data, monkeypatch, protected):
    second = deepcopy(jellyfin_data)
    if protected == "unwatched":
        second["episodes"]["Items"][0]["UserData"]["Played"] = False
    elif protected == "favourite":
        second["episodes"]["Items"][0]["UserData"]["IsFavorite"] = True
    else:
        second["episodes"]["Items"].pop(0)
        second["episodes"]["TotalRecordCount"] = 1
    env.pop("JELLYFIN_USER_ID")
    env.update(WATCH_POLICY="all-selected", JELLYFIN_USER_IDS=f"{'a' * 32},{'b' * 32}")
    jellyfin_client(monkeypatch, {"a" * 32: jellyfin_data, "b" * 32: second})
    assert runner.discover_jellyfin(env, 2) == {99: {102}}


def test_jellyfin_all_selected_happy_path(env, jellyfin_data, monkeypatch):
    env.pop("JELLYFIN_USER_ID")
    env.update(WATCH_POLICY="all-selected", JELLYFIN_USER_IDS=f"{'a' * 32},{'b' * 32}")
    jellyfin_client(monkeypatch, {"a" * 32: jellyfin_data, "b" * 32: jellyfin_data})
    assert runner.discover_jellyfin(env, 2) == {99: {101, 102}}


@pytest.mark.parametrize(
    "users,policy",
    [
        ("bad", "selected-user"),
        (f"{'a' * 32},{'a' * 32}", "all-selected"),
        (f"{'a' * 32},{'b' * 32}", "selected-user"),
        ("a" * 32 + ",", "all-selected"),
    ],
)
def test_invalid_jellyfin_user_policy(env, users, policy):
    env.pop("JELLYFIN_USER_ID")
    env.update(JELLYFIN_USER_IDS=users, WATCH_POLICY=policy)
    with pytest.raises(ValueError):
        runner.jellyfin_users(env)


def test_jellyfin_pagination_uses_actual_page_length():
    client = Mock()
    client.jellyfin._get.side_effect = [
        {"Items": [{"Id": "a"}, {"Id": "b"}], "TotalRecordCount": 3, "StartIndex": 0},
        {"Items": [{"Id": "c"}], "TotalRecordCount": 3, "StartIndex": 2},
    ]
    assert len(list(runner.jellyfin_items(client, "user", {}))) == 3
    assert client.jellyfin._get.call_args.kwargs["params"]["StartIndex"] == "2"


@pytest.mark.parametrize(
    "pages",
    [
        [None],
        [{"Items": [], "TotalRecordCount": 1}],
        [{"Items": [], "TotalRecordCount": True}],
        [{"Items": [{"Id": "a"}], "TotalRecordCount": 0}],
        [{"Items": [{"Id": "a"}], "TotalRecordCount": 1, "StartIndex": 2}],
        [
            {"Items": [{"Id": "a"}], "TotalRecordCount": 2},
            {"Items": [{"Id": "a"}], "TotalRecordCount": 2},
        ],
        [
            {"Items": [{"Id": "a"}], "TotalRecordCount": 2},
            {"Items": [{"Id": "b"}], "TotalRecordCount": 3},
        ],
    ],
)
def test_jellyfin_inconsistent_pagination_stops(pages):
    client = Mock()
    client.jellyfin._get.side_effect = pages
    with pytest.raises(ValueError):
        list(runner.jellyfin_items(client, "user", {}))


def response(data, status=200):
    result = requests.Response()
    result.status_code = status
    result.headers.update(
        {"Content-Type": "application/json", "Date": "Sat, 03 Jan 2026 12:00:00 GMT"}
    )
    result._content = json.dumps(data).encode() if status != 204 else b""
    if status == 204:
        result.headers.pop("Content-Type")
    return result


def test_sonarr_sdk_contract_applies_one_shared_file(monkeypatch, sonarr_data):
    calls = []

    def request(session, method, url, **kwargs):
        calls.append((method, url, kwargs))
        assert kwargs["timeout"] == (5, 30)
        assert kwargs["allow_redirects"] is False
        if "series?tvdbId=99" in url:
            return response(sonarr_data["series"])
        if method == "GET" and url.endswith("/episode"):
            assert kwargs["params"] == {"seriesId": 5}
            return response(sonarr_data["episodes"])
        if method == "GET" and url.endswith("/episodefile/10"):
            return response(sonarr_data["file"])
        if method == "PUT" and "/episode/" in url:
            return response({"id": int(url.rsplit("/", 1)[1]), **kwargs["json"]})
        if method == "DELETE" and url.endswith("/episodefile/10"):
            return response(None, status=204)
        raise AssertionError(f"Unexpected HTTP call: {method} {url}")

    monkeypatch.setattr(requests.Session, "request", request)
    client = create_sonarr_client("https://sonarr.invalid", "fixture-key")
    assert runner.sonarr_match(client, 99) == 5
    plans, _ = plan_series(5, client.get_episode(5, series=True), {101, 102})
    plans, _ = attach_file_metadata(client, plans)
    results = execute_plans(client, plans, apply=True, revalidate=lambda p: True)
    assert results[0]["status"] == "deleted"
    mutations = [(method, url) for method, url, _ in calls if method != "GET"]
    assert mutations == [
        ("PUT", "https://sonarr.invalid/api/v3/episode/1"),
        ("PUT", "https://sonarr.invalid/api/v3/episode/2"),
        ("DELETE", "https://sonarr.invalid/api/v3/episodefile/10"),
    ]


@pytest.mark.parametrize("status", [202, 206, 302, 409, 429, 503, 504])
def test_sonarr_unhandled_sdk_http_statuses_are_rejected(monkeypatch, status):
    transport = Mock(return_value=response({}, status=status))
    monkeypatch.setattr(requests.Session, "request", transport)
    client = create_sonarr_client("https://sonarr.invalid", "fixture-key")
    with pytest.raises(requests.HTTPError):
        client.del_episode_file(10)
    assert transport.call_count == 1
    assert client.session.get_adapter("https://").max_retries.total == 0


def test_jellyfin_sdk_user_page_contract(monkeypatch, jellyfin_data):
    from jellyfin_apiclient_python import JellyfinClient

    client = JellyfinClient(allow_multiple_clients=True)
    client.config.data.update(
        {
            "auth.server": "https://jellyfin.invalid",
            "auth.token": "fixture-token",
            "http.timeout": 30,
        }
    )
    transport = Mock(return_value=response(jellyfin_data["episodes"]))
    monkeypatch.setattr(requests, "get", transport)
    items = list(runner.jellyfin_items(client, "a" * 32, {"IncludeItemTypes": "Episode"}))
    assert len(items) == 2
    params = transport.call_args.kwargs["params"]
    assert params["EnableUserData"] == "true" and params["StartIndex"] == "0"
    assert transport.call_args.kwargs["url"] == f"https://jellyfin.invalid/Users/{'a' * 32}/Items"
