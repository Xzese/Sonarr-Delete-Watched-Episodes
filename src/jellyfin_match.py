"""Conservative, read-only Sonarr lookups for missing Jellyfin TVDB IDs."""

from pathlib import PurePosixPath

from cleanup_plan import CleanupError, positive_id


def tvdb_id(value):
    try:
        return positive_id(value)
    except ValueError:
        return None


def relative_path(path, root):
    if not isinstance(path, str) or not isinstance(root, str):
        return None
    path, root = PurePosixPath(path), PurePosixPath(root)
    if not path.is_absolute() or not root.is_absolute() or ".." in (*path.parts, *root.parts):
        return None
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    return relative.as_posix() if relative.parts else None


def records(data):
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise CleanupError("Sonarr fallback lookup returned incomplete metadata.")
    return data


class JellyfinMatcher:
    """Cache only within one discovery; every apply revalidation starts fresh."""

    def __init__(self, client):
        self.client = client
        self.catalog = None
        self.series_cache = {}
        self.episode_cache = {}
        self.file_cache = {}

    def series(self, show):
        if not isinstance(show, dict):
            return None
        providers = show.get("ProviderIds") or {}
        tvdb = tvdb_id(providers.get("Tvdb"))
        imdb = providers.get("Imdb")
        key = (show["Id"], tvdb, imdb)
        if key in self.series_cache:
            return self.series_cache[key]
        if tvdb is None and (
            not isinstance(imdb, str)
            or not imdb.startswith("tt")
            or not imdb[2:].isascii()
            or not imdb[2:].isdigit()
        ):
            return None
        if self.catalog is None:
            self.catalog = records(self.client.get_series())
        matches = [
            item
            for item in self.catalog
            if (tvdb_id(item.get("tvdbId")) == tvdb if tvdb else item.get("imdbId") == imdb)
        ]
        match = matches[0] if len(matches) == 1 else None
        if match is not None and (
            tvdb_id(match.get("id")) is None
            or tvdb_id(match.get("tvdbId")) is None
            or (tvdb is not None and imdb and match.get("imdbId") != imdb)
        ):
            match = None
        self.series_cache[key] = match
        return match

    def match(self, show, episode):
        """Require a unique series, numbering, relative file path and byte size."""
        season, number = episode.get("ParentIndexNumber"), episode.get("IndexNumber")
        if (
            type(season) is not int
            or type(number) is not int
            or season < 0
            or number < 0
            or episode.get("IndexNumberEnd", number) not in (None, number)
        ):
            return None
        relative = relative_path(episode.get("Path"), show.get("Path"))
        sources = episode.get("MediaSources")
        if not relative or not isinstance(sources, list) or len(sources) != 1:
            return None
        source = sources[0]
        if (
            not isinstance(source, dict)
            or source.get("Path") != episode.get("Path")
            or source.get("IsRemote") is not False
            or type(source.get("Size")) is not int
            or source["Size"] <= 0
        ):
            return None
        series = self.series(show)
        if series is None:
            return None
        series_id = positive_id(series["id"])
        if series_id not in self.episode_cache:
            self.episode_cache[series_id] = records(
                self.client.get_episode(id_=series_id, series=True)
            )
        episodes = self.episode_cache[series_id]
        matches = [
            item
            for item in episodes
            if type(item.get("seasonNumber")) is int
            and type(item.get("episodeNumber")) is int
            and item["seasonNumber"] == season
            and item["episodeNumber"] == number
        ]
        if len(matches) != 1:
            return None
        match = matches[0]
        episode_id, file_id = tvdb_id(match.get("tvdbId")), tvdb_id(match.get("episodeFileId"))
        existing_id = tvdb_id((episode.get("ProviderIds") or {}).get("Tvdb"))
        if (
            not episode_id
            or not file_id
            or match.get("hasFile") is not True
            or tvdb_id(match.get("seriesId")) != series_id
            or (existing_id is not None and existing_id != episode_id)
        ):
            return None
        # Combined files/range entries need complete watch evidence for all
        # members; do not infer that evidence from one Jellyfin episode.
        members = [item for item in episodes if tvdb_id(item.get("episodeFileId")) == file_id]
        if len(members) != 1:
            return None
        if series_id not in self.file_cache:
            self.file_cache[series_id] = records(
                self.client.get_episode_file(series_id, series=True)
            )
        files = self.file_cache[series_id]
        path_matches = [item for item in files if item.get("relativePath") == relative]
        id_matches = [item for item in files if tvdb_id(item.get("id")) == file_id]
        if len(path_matches) != 1 or len(id_matches) != 1 or path_matches[0] != id_matches[0]:
            return None
        file = path_matches[0]
        if (
            tvdb_id(file.get("seriesId")) != series_id
            or type(file.get("size")) is not int
            or file["size"] != source["Size"]
            or relative_path(file.get("path"), series.get("path")) != relative
        ):
            return None
        return positive_id(series["tvdbId"]), episode_id
