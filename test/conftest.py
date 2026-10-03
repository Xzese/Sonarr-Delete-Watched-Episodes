import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sonarr_data():
    return json.loads((FIXTURES / "sonarr.json").read_text())


@pytest.fixture
def jellyfin_data():
    return json.loads((FIXTURES / "jellyfin.json").read_text())


@pytest.fixture
def env(tmp_path):
    return {
        "SONARR_URL": "https://sonarr.invalid",
        "SONARR_KEY": "fixture-key",
        "MEDIA_SERVICE": "plex",
        "WATCH_POLICY": "selected-user",
        "PLEX_URL": "https://plex.invalid",
        "PLEX_TOKEN": "fixture-token",
        "JELLYFIN_URL": "https://jellyfin.invalid",
        "JELLYFIN_TOKEN": "fixture-token",
        "JELLYFIN_USER_ID": "a" * 32,
        "LOG_FILE": str(tmp_path / "output" / "log.txt"),
    }


@pytest.fixture
def client(sonarr_data):
    client = Mock()
    client.get_series.return_value = deepcopy(sonarr_data["series"])
    client.get_episode.return_value = deepcopy(sonarr_data["episodes"])
    client.get_episode_file.return_value = deepcopy(sonarr_data["file"])
    client.upd_episode.side_effect = lambda id_, payload: {"id": id_, **payload}
    return client
