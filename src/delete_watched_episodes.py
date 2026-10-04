#!/usr/bin/env python3
"""Preview watched-episode cleanup; use --apply to permit file deletion."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from uuid import UUID

from cleanup_plan import CleanupError, attach_file_metadata, execute_plans, plan_series, positive_id
from jellyfin_match import JellyfinMatcher, relative_path, tvdb_id
from run_logging import configure_logging, log_report
from sonarr_client import create_sonarr_client


class Eligibility(dict):
    """Eligible episode TVDB IDs by series, with read-only exclusion reasons."""

    def __init__(self):
        super().__init__()
        self.exclusions = []


def require_env(env, name):
    value = env.get(name, "").strip()
    if not value:
        raise CleanupError(f"{name} is required.")
    return value


def require_url(env, name):
    value = require_env(env, name)
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise CleanupError(
            f"{name} must be an HTTP or HTTPS URL without credentials, query or fragment."
        )
    try:
        parsed.port
    except ValueError:
        raise CleanupError(f"{name} contains an invalid port.") from None
    return value.rstrip("/")


def retention_days(env):
    raw = env.get("DAYS_TO_DELETE", "2")
    if not raw.isascii() or not raw.isdigit() or not 0 <= int(raw) <= 36500:
        raise CleanupError("DAYS_TO_DELETE must be an integer between 0 and 36500.")
    return int(raw)


def watch_policy(env, service):
    policy = require_env(env, "WATCH_POLICY").lower()
    allowed = {"selected-user"} if service == "plex" else {"selected-user", "all-selected"}
    if policy not in allowed:
        raise CleanupError(f"WATCH_POLICY is unsupported for {service}.")
    return policy


def jellyfin_users(env):
    single, multiple = (
        env.get("JELLYFIN_USER_ID", "").strip(),
        env.get("JELLYFIN_USER_IDS", "").strip(),
    )
    if bool(single) == bool(multiple):
        raise CleanupError("Set exactly one of JELLYFIN_USER_ID or JELLYFIN_USER_IDS.")
    raw_users = [single] if single else [part.strip() for part in multiple.split(",")]
    try:
        users = [UUID(item).hex for item in raw_users]
    except ValueError:
        raise CleanupError("Jellyfin user IDs must be UUIDs.") from None
    if len(set(users)) != len(users):
        raise CleanupError("Duplicate Jellyfin users are not permitted.")
    policy = watch_policy(env, "jellyfin")
    if policy == "selected-user" and len(users) != 1:
        raise CleanupError("selected-user requires exactly one Jellyfin user.")
    return users


def validate_config(env):
    require_url(env, "SONARR_URL")
    require_env(env, "SONARR_KEY")
    retention_days(env)
    service = env.get("MEDIA_SERVICE", "plex").lower()
    if service not in {"plex", "jellyfin"}:
        raise CleanupError("MEDIA_SERVICE must be plex or jellyfin.")
    watch_policy(env, service)
    require_url(env, f"{service.upper()}_URL")
    require_env(env, f"{service.upper()}_TOKEN")
    if service == "jellyfin":
        jellyfin_users(env)
    elif env.get("DEFAULT_DELETE", "false").lower() not in {"true", "false"}:
        raise CleanupError("DEFAULT_DELETE must be true or false.")
    elif not env.get("PLEX_LIBRARY", "TV Shows").strip():
        raise CleanupError("PLEX_LIBRARY cannot be empty.")


def get_last_played_timestamp(user_data):
    if not isinstance(user_data, dict):
        return None
    raw = next(
        (
            user_data.get(k)
            for k in ("LastPlayedDate", "DatePlayed", "LastPlayedDateUtc", "LastPlayedAt")
            if user_data.get(k)
        ),
        None,
    )
    if not isinstance(raw, str):
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        return stamp.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def get_last_played_date(user_data):
    stamp = get_last_played_timestamp(user_data)
    return stamp.date() if stamp is not None else None


def _tvdb(guids):
    identifiers = set()
    for guid in guids or []:
        text = getattr(guid, "id", "")
        if isinstance(text, str) and text.startswith("tvdb://"):
            try:
                identifiers.add(positive_id(text.removeprefix("tvdb://")))
            except ValueError:
                return None
    return identifiers.pop() if len(identifiers) == 1 else None


def discover_plex(env, days):
    watch_policy(env, "plex")
    delete_default = env.get("DEFAULT_DELETE", "false").lower()
    if delete_default not in {"true", "false"}:
        raise CleanupError("DEFAULT_DELETE must be true or false.")
    url, token = require_url(env, "PLEX_URL"), require_env(env, "PLEX_TOKEN")
    from plexapi.server import PlexServer

    plex = PlexServer(url, token, timeout=30)
    library = plex.library.section(env.get("PLEX_LIBRARY", "TV Shows"))
    eligible = Eligibility()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    seen, duplicates = set(), set()
    show_handles = {}
    # Evaluate episode timestamps locally. Unprefixed Plex playback filters can
    # use show-level watch state; querying all episodes also exposes duplicates.
    for episode in library.search(libtype="episode"):
        show = episode.show()
        series_id, episode_id = _tvdb(show.guids), _tvdb(episode.guids)
        if not series_id or not episode_id:
            eligible.exclusions.append(
                f"Skipping Plex episode '{episode.title}' in series '{show.title}': "
                "missing or ambiguous TVDB identifiers."
            )
            continue
        key = (series_id, episode_id)
        show_handles.setdefault(series_id, set()).add(positive_id(show.ratingKey))
        if key in seen:
            duplicates.add(key)
        seen.add(key)
        genres = {genre.tag for genre in show.genres}
        if "Keep" in genres or (delete_default == "false" and "Delete" not in genres):
            eligible.exclusions.append(
                f"Plex series {series_id}, episode {episode_id} retained: genre policy."
            )
            continue
        try:
            raw = episode._data.attrib.get("lastViewedAt")
            played = datetime.fromtimestamp(positive_id(raw), timezone.utc)
        except (ValueError, OverflowError, OSError):
            played = None
        if episode.viewCount <= 0 or episode.viewOffset != 0 or played is None or played >= cutoff:
            eligible.exclusions.append(
                f"Plex series {series_id}, episode {episode_id} retained: unwatched, in progress or inside retention."
            )
            continue
        eligible.setdefault(series_id, set()).add(episode_id)
    for series_id, episode_id in duplicates:
        eligible.get(series_id, set()).discard(episode_id)
        eligible.exclusions.append(
            f"Plex series {series_id}, episode {episode_id} retained: duplicate TVDB mapping."
        )
    for series_id, handles in show_handles.items():
        if len(handles) > 1:
            eligible.pop(series_id, None)
            eligible.exclusions.append(
                f"Plex series {series_id} retained: duplicate series TVDB mapping."
            )
    return eligible


def jellyfin_items(client, user_id, params):
    """Bounded pagination: stable counts, valid offsets and unique item IDs."""
    offset, expected_total, seen = 0, None, set()
    while True:
        page = client.jellyfin._get(
            f"Users/{user_id}/Items",
            params={
                **params,
                "Limit": "100",
                "StartIndex": str(offset),
                "EnableUserData": "true",
                "EnableTotalRecordCount": "true",
                "SortBy": "SortName",
                "SortOrder": "Ascending",
            },
        )
        if not isinstance(page, dict):
            raise CleanupError("Invalid Jellyfin page.")
        batch, total = page.get("Items"), page.get("TotalRecordCount")
        if (
            not isinstance(batch, list)
            or isinstance(total, bool)
            or not isinstance(total, int)
            or total < 0
        ):
            raise CleanupError("Invalid Jellyfin page.")
        if expected_total is not None and total != expected_total:
            raise CleanupError("Jellyfin item count changed during pagination.")
        expected_total = total
        if (
            page.get("StartIndex", offset) != offset
            or len(batch) > 100
            or offset + len(batch) > total
            or (not batch and offset < total)
        ):
            raise CleanupError("Incomplete or inconsistent Jellyfin page.")
        for item in batch:
            item_id = item.get("Id") if isinstance(item, dict) else None
            if not isinstance(item_id, str) or not item_id or item_id in seen:
                raise CleanupError("Missing or duplicate Jellyfin item ID.")
            seen.add(item_id)
            yield item
        offset += len(batch)
        if offset == total:
            break


def _jellyfin_user_eligibility(client, user_id, cutoff, fallback=None):
    series = list(
        jellyfin_items(
            client,
            user_id,
            {"Recursive": "true", "IncludeItemTypes": "Series", "Fields": "ProviderIds,Path"},
        )
    )
    series_by_id = {show["Id"]: show for show in series}
    resolved, inferred = {}, set()

    def mapping(episode):
        if episode["Id"] not in resolved:
            show = series_by_id.get(episode.get("SeriesId"))
            series_id = tvdb_id((show.get("ProviderIds") or {}).get("Tvdb")) if show else None
            episode_id = tvdb_id((episode.get("ProviderIds") or {}).get("Tvdb"))
            if series_id and episode_id:
                result = (series_id, episode_id)
            else:
                result = fallback.match(show, episode) if fallback and show else None
                if result is not None:
                    inferred.add(episode["Id"])
            resolved[episode["Id"]] = result
        return resolved[episode["Id"]]

    eligible = Eligibility()
    episodes = list(
        jellyfin_items(
            client,
            user_id,
            {
                "Recursive": "true",
                "IncludeItemTypes": "Episode",
                "Fields": "ProviderIds,Path,MediaSources",
                "IsMissing": "false",
            },
        )
    )
    for episode in episodes:
        identity = f"Jellyfin item {episode['Id']}"
        data = episode.get("UserData")
        show_data = series_by_id.get(episode.get("SeriesId"))
        show_user_data = show_data.get("UserData") if show_data is not None else None
        if not isinstance(data, dict) or not isinstance(show_user_data, dict):
            eligible.exclusions.append(f"{identity} retained: missing user metadata.")
            continue
        played = get_last_played_timestamp(data)
        if (
            show_user_data.get("IsFavorite") is not False
            or data.get("IsFavorite") is not False
            or data.get("Played") is not True
            or type(data.get("PlaybackPositionTicks")) is not int
            or data.get("PlaybackPositionTicks") != 0
            or played is None
            or played >= cutoff
        ):
            eligible.exclusions.append(
                f"{identity} retained: ambiguous, protected, in progress or outside watch policy."
            )
            continue

        # Check watch eligibility before validating TVDB metadata or warning.
        match = mapping(episode)
        if match is None:
            episode_name = episode.get("Name") or episode["Id"]
            season, number = episode.get("ParentIndexNumber"), episode.get("IndexNumber")
            if type(season) is int and type(number) is int:
                episode_name = f"{episode_name} (S{season:02}E{number:02})"
            series_name = (
                show_data.get("Name")
                or episode.get("SeriesName")
                or episode.get("SeriesId")
                or "unknown series"
            )
            episode_id = tvdb_id((episode.get("ProviderIds") or {}).get("Tvdb"))
            if episode_id is None:
                eligible.exclusions.append(
                    f"Skipping Jellyfin episode '{episode_name}' in series '{series_name}': "
                    "missing episode TVDB identifier; no unambiguous Sonarr file fallback."
                )
            else:
                eligible.exclusions.append(
                    f"Jellyfin episode {episode_id} retained: no series TVDB mapping "
                    "or unambiguous Sonarr file fallback."
                )
            continue
        series_id, episode_id = match
        eligible.setdefault(series_id, set()).add(episode_id)
    if not eligible:
        return eligible
    # Audit every mapping, including unwatched copies, so an eligible episode
    # cannot bypass duplicate protection. Only blocked candidates need warnings.
    seen, duplicates = set(), set()
    for episode in episodes:
        match = mapping(episode)
        if match is None:
            continue
        if match in seen:
            duplicates.add(match)
        seen.add(match)
    seen_series, ambiguous_series = set(), set()
    for show in series:
        series_id = tvdb_id((show.get("ProviderIds") or {}).get("Tvdb"))
        if series_id is None and fallback:
            match = fallback.series(show)
            series_id = tvdb_id(match.get("tvdbId")) if match else None
        if series_id is not None:
            if series_id in seen_series:
                ambiguous_series.add(series_id)
            seen_series.add(series_id)
    for series_id in ambiguous_series:
        if eligible.pop(series_id, None):
            eligible.exclusions.append(
                f"Jellyfin series {series_id} retained: duplicate series TVDB mapping."
            )
    for series_id, episode_id in duplicates:
        candidates = eligible.get(series_id, set())
        if episode_id in candidates:
            candidates.discard(episode_id)
            eligible.exclusions.append(
                f"Jellyfin series {series_id}, episode {episode_id} retained: duplicate TVDB mapping."
            )
    # A conflicting/unmapped copy must not disappear from the duplicate
    # audit simply because its provider IDs or numbering cannot be resolved.
    seen_paths, duplicate_paths = set(), set()
    for episode in episodes:
        path = relative_path(episode.get("Path"), "/")
        if path is not None:
            if path in seen_paths:
                duplicate_paths.add(path)
            seen_paths.add(path)
    for episode in episodes:
        if episode["Id"] not in inferred:
            continue
        if relative_path(episode.get("Path"), "/") in duplicate_paths:
            series_id, episode_id = resolved[episode["Id"]]
            candidates = eligible.get(series_id, set())
            if episode_id in candidates:
                candidates.discard(episode_id)
                eligible.exclusions.append(
                    f"Jellyfin series {series_id}, episode {episode_id} retained: "
                    "duplicate Jellyfin file mapping."
                )
    return eligible


def discover_jellyfin(env, days, *, sonarr_client=None):
    users = jellyfin_users(env)
    url, token = require_url(env, "JELLYFIN_URL"), require_env(env, "JELLYFIN_TOKEN")
    from jellyfin_apiclient_python import JellyfinClient

    client = JellyfinClient()
    client.config.data["auth.ssl"] = url.lower().startswith("https://")
    client.config.data["app.name"] = "sonarr_cleanup"
    client.config.data["app.version"] = "0.2.0"
    client.authenticate({"Servers": [{"AccessToken": token, "address": url}]}, discover=False)
    if not client.config.data.get("auth.server"):
        raise CleanupError("Jellyfin authentication failed.")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    combined = None
    fallback = JellyfinMatcher(sonarr_client) if sonarr_client is not None else None
    try:
        for user_id in users:
            selected = _jellyfin_user_eligibility(client, user_id, cutoff, fallback)
            if combined is None:
                combined = selected
            else:
                combined.exclusions.extend(selected.exclusions)
                for series_id in combined:
                    combined[series_id].intersection_update(selected.get(series_id, set()))
    finally:
        client.http.stop_session()
    return combined


def discover_eligible(env, *, sonarr_client=None):
    days = retention_days(env)
    service = env.get("MEDIA_SERVICE", "plex").lower()
    if service == "plex":
        return discover_plex(env, days)
    if service == "jellyfin":
        return discover_jellyfin(env, days, sonarr_client=sonarr_client)
    raise CleanupError("MEDIA_SERVICE must be plex or jellyfin.")


def sonarr_match(client, tvdb_id):
    matches = client.get_series(id_=positive_id(tvdb_id), tvdb=True)
    if not isinstance(matches, list):
        raise CleanupError("Sonarr TVDB lookup must return a list.")
    if len(matches) != 1:
        return None
    match = matches[0]
    if positive_id(match.get("tvdbId")) != tvdb_id:
        raise CleanupError("Sonarr returned a different series TVDB identifier.")
    return positive_id(match.get("id"))


def deletion_limit(cli_value, env, name, default):
    if cli_value is not None:
        if cli_value < 0:
            raise CleanupError(f"{name} must be a non-negative integer; 0 disables that limit.")
        return cli_value
    raw = env.get(name, str(default))
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit():
        raise CleanupError(f"{name} must be a non-negative integer; 0 disables that limit.")
    return int(raw)


def main(argv=None, *, env=None, discover=None, client_factory=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Permit unmonitoring and deletion. Default is read-only.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full JSON report instead of activity messages.",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        help="Maximum files in one apply run (MAX_FILES or 10). 0 disables this limit.",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        help="Maximum bytes in one apply run (MAX_BYTES or 10 GB). 0 disables this limit.",
    )
    args = parser.parse_args(argv)
    mode = "apply" if args.apply else "preview"
    client = None
    logger, log_handler = None, None

    def report_result(report):
        if args.json:
            print(json.dumps(report, indent=2))
        log_report(logger, report, console=not args.json)

    try:
        if env is None:
            from dotenv import load_dotenv

            load_dotenv()
            env = os.environ
        env = dict(env)
        logger, log_handler = configure_logging(env)
        logger.info("Cleanup %s started.", mode)
        if not args.json:
            print(f"[INFO] Cleanup {mode} started.")
        max_files = deletion_limit(args.max_files, env, "MAX_FILES", 10)
        max_bytes = deletion_limit(args.max_bytes, env, "MAX_BYTES", 10_000_000_000)
        validate_config(env)
        client = (client_factory or create_sonarr_client)(
            require_url(env, "SONARR_URL"), require_env(env, "SONARR_KEY")
        )
        if discover is None:

            def discover(env):
                return discover_eligible(env, sonarr_client=client)

        eligible = discover(env)
        plans, exclusions, series_tvdb = [], list(getattr(eligible, "exclusions", [])), {}
        for tvdb_id, episode_ids in sorted(eligible.items()):
            tvdb_id = positive_id(tvdb_id)
            if not episode_ids:
                continue
            series_id = sonarr_match(client, tvdb_id)
            if series_id is None:
                exclusions.append(f"Series {tvdb_id} skipped: no unique Sonarr match.")
                continue
            series_tvdb[series_id] = tvdb_id
            selected, reasons = plan_series(
                series_id, client.get_episode(id_=series_id, series=True), episode_ids
            )
            selected, file_reasons = attach_file_metadata(client, selected)
            plans.extend(selected)
            exclusions.extend(reasons + file_reasons)

        def revalidate(plan):
            fresh = discover(env)
            tvdb_id = series_tvdb[plan.series_id]
            return (
                all(tvdb in fresh.get(tvdb_id, set()) for _, tvdb in plan.episodes)
                and sonarr_match(client, tvdb_id) == plan.series_id
            )

        results = execute_plans(
            client,
            plans,
            apply=args.apply,
            max_files=max_files,
            max_bytes=max_bytes,
            revalidate=revalidate,
        )
        report_result(
            {
                "mode": mode,
                "planned_files": len(plans),
                "planned_bytes": sum(p.identity.size for p in plans),
                "limits": {"max_files": max_files, "max_bytes": max_bytes},
                "results": results,
                "exclusions": exclusions,
            }
        )
        return (
            1
            if any(r["status"] in {"blocked", "unconfirmed", "not_attempted"} for r in results)
            else 0
        )
    except CleanupError as error:
        report_result({"mode": mode, "status": "stopped", "reason": str(error)})
        return 1
    except Exception as error:
        report_result(
            {
                "mode": mode,
                "status": "stopped",
                "error_type": type(error).__name__,
                "reason": "Discovery or planning failed. No automatic mutation retry was made.",
            }
        )
        return 1
    finally:
        try:
            if client is not None:
                client.session.close()
        finally:
            if log_handler is not None:
                logger.removeHandler(log_handler)
                log_handler.close()


if __name__ == "__main__":
    raise SystemExit(main())
