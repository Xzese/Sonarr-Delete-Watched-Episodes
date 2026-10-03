#!/usr/bin/env python3
"""Preview watched-episode cleanup; use --apply to permit file deletion."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from urllib.parse import urlparse

from cleanup_plan import execute_plans, plan_series, positive_id


def require_env(env, name):
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required.")
    return value


def retention_days(env):
    raw = env.get("DAYS_TO_DELETE", "2")
    if not raw.isascii() or not raw.isdigit():
        raise ValueError("DAYS_TO_DELETE must be a non-negative integer.")
    return int(raw)


def get_last_played_date(user_data):
    if not isinstance(user_data, dict):
        return None
    raw = next((user_data.get(k) for k in ("LastPlayedDate", "DatePlayed", "LastPlayedDateUtc", "LastPlayedAt") if user_data.get(k)), None)
    if not isinstance(raw, str):
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        # Undated or timezone-free watch evidence cannot authorise deletion.
        if stamp.tzinfo is None:
            return None
        return stamp.astimezone(timezone.utc).date()
    except ValueError:
        return None


def _tvdb(guids):
    for guid in guids:
        text = getattr(guid, "id", "")
        if isinstance(text, str) and text.startswith("tvdb://"):
            try:
                return positive_id(text.removeprefix("tvdb://"))
            except ValueError:
                pass
    return None


def discover_plex(env, days):
    from plexapi.server import PlexServer

    delete_default = env.get("DEFAULT_DELETE", "false").lower()
    if delete_default not in {"true", "false"}:
        raise ValueError("DEFAULT_DELETE must be true or false.")
    plex = PlexServer(require_env(env, "PLEX_URL"), require_env(env, "PLEX_TOKEN"))
    library = plex.library.section(env.get("PLEX_LIBRARY", "TV Shows"))
    filters = {"lastViewedAt<<": f"{days}d", "genre=" if delete_default == "false" else "genre!=": "Delete" if delete_default == "false" else "Keep"}
    eligible = {}
    for episode in library.search(unwatched=False, libtype="episode", filters=filters):
        series_id = _tvdb(episode.season().show().guids)
        episode_id = _tvdb(episode.guids)
        if series_id and episode_id:
            eligible.setdefault(series_id, set()).add(episode_id)
    return eligible


def discover_jellyfin(env, days):
    # Resolve the selected user before creating a network client.
    user_id = require_env(env, "JELLYFIN_USER_ID")
    if not user_id.isascii() or not user_id.isalnum():
        raise ValueError("JELLYFIN_USER_ID must be an explicit alphanumeric user ID.")
    from jellyfin_apiclient_python import JellyfinClient

    url = require_env(env, "JELLYFIN_URL").rstrip("/")
    client = JellyfinClient()
    client.config.data["auth.ssl"] = url.lower().startswith("https://")
    client.config.data["app.name"] = "sonarr_cleanup"
    client.config.data["app.version"] = "0.1.0"
    client.authenticate({"Servers": [{"AccessToken": require_env(env, "JELLYFIN_TOKEN"), "address": url}]}, discover=False)
    if not client.config.data.get("auth.server"):
        raise ValueError("Jellyfin authentication failed.")

    def items(params):
        offset = 0
        while True:
            page = client.jellyfin._get(f"Users/{user_id}/Items", params={**params, "Limit": "100", "StartIndex": str(offset)})
            batch = page["Items"]
            total = int(page["TotalRecordCount"])
            if not isinstance(batch, list) or total < 0:
                raise ValueError("Invalid Jellyfin page.")
            if not batch and offset < total:
                raise ValueError("Incomplete Jellyfin page; cleanup was cancelled.")
            yield from batch
            offset += len(batch)
            if offset >= total:
                break

    series = {item["Id"]: item for item in items({"recursive": "true", "includeItemTypes": "series", "fields": "ProviderIds,UserData"})}
    eligible = {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
    for episode in items({"recursive": "true", "includeItemTypes": "episode", "fields": "ProviderIds,UserData", "IsMissing": "false", "filters": "IsPlayed"}):
        show = series.get(episode.get("SeriesId"))
        data = episode.get("UserData", {})
        if not show or show.get("UserData", {}).get("IsFavorite") is not False or data.get("IsFavorite") is not False:
            continue
        played = get_last_played_date(data)
        if data.get("Played") is not True or played is None or played >= cutoff:
            continue
        try:
            show_id = positive_id(show.get("ProviderIds", {}).get("Tvdb"))
            episode_id = positive_id(episode.get("ProviderIds", {}).get("Tvdb"))
        except ValueError:
            continue
        eligible.setdefault(show_id, set()).add(episode_id)
    return eligible


def discover_eligible(env):
    days = retention_days(env)
    service = env.get("MEDIA_SERVICE", "plex").lower()
    if service == "plex":
        return discover_plex(env, days)
    if service == "jellyfin":
        return discover_jellyfin(env, days)
    raise ValueError("MEDIA_SERVICE must be plex or jellyfin.")


def main(argv=None, *, env=None, discover=None, client_factory=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Permit unmonitoring and deletion. Default is read-only.")
    parser.add_argument("--max-files", type=int, default=10, help="Maximum files allowed in one apply run.")
    args = parser.parse_args(argv)
    try:
        if args.max_files < 1:
            raise ValueError("--max-files must be positive.")
        if env is None:
            from dotenv import load_dotenv
            load_dotenv()
            env = os.environ
        url = require_env(env, "SONARR_URL")
        if urlparse(url).scheme not in {"http", "https"} or not urlparse(url).hostname:
            raise ValueError("SONARR_URL must be an HTTP or HTTPS URL.")
        key = require_env(env, "SONARR_KEY")
        retention_days(env)
        eligible = (discover or discover_eligible)(env)
        if client_factory is None:
            from pyarr import SonarrAPI
            client_factory = SonarrAPI
        client = client_factory(url, key)
        plans, exclusions = [], []
        for tvdb_id, episode_ids in eligible.items():
            matches = client.get_series(id_=str(positive_id(tvdb_id)), tvdb=True)
            if len(matches) != 1:
                exclusions.append(f"Series {tvdb_id} skipped: no unique Sonarr match.")
                continue
            series_id = positive_id(matches[0].get("id"))
            selected, reasons = plan_series(series_id, client.get_episode(id_=series_id, series=True), episode_ids)
            plans.extend(selected)
            exclusions.extend(reasons)
        results = execute_plans(client, plans, apply=args.apply, max_files=args.max_files)
        print(json.dumps({"mode": "apply" if args.apply else "preview", "results": results, "exclusions": exclusions}, indent=2))
        return 1 if any(r["status"] in {"blocked", "unconfirmed", "not_attempted"} for r in results) else 0
    except ValueError as error:
        print(f"Cleanup stopped: {error}")
        return 1
    except Exception as error:
        print(f"Cleanup stopped ({type(error).__name__}). No automatic retry was made.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
