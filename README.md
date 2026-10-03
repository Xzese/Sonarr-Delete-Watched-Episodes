# Watched episode cleanup for Sonarr

Build a file-level cleanup plan from Plex or Jellyfin watch evidence. The default command only reads library state. Deleting files and unmonitoring episodes requires `--apply`.

## Setup and preview

Use Python 3.11 or later:

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# Configure Sonarr and one media provider in .env.
.venv/bin/python src/delete_watched_episodes.py
```

The JSON report includes proposed file paths, sizes, episode identifiers, exclusion reasons and total bytes. Importing the module does not create clients or perform cleanup. Configuration is validated before network discovery; invalid values stop the run rather than changing the retention policy.

`DAYS_TO_DELETE` defaults to 2 and accepts whole numbers from 0 to 36500. An episode must have a valid playback timestamp strictly older than that many complete 24-hour periods. Episodes with playback in progress are retained.

## Choose whose watch state authorises deletion

`WATCH_POLICY` is required, including for preview. Selecting users is a decision about shared files: deleting a file removes it for everyone who uses that library.

| Provider | Policy | Behaviour |
| --- | --- | --- |
| Plex | `selected-user` | Use the user represented by `PLEX_TOKEN`. Other users' watch state is not checked. |
| Jellyfin | `selected-user` | Require exactly one explicitly selected user. |
| Jellyfin | `all-selected` | Require every selected user to have eligible watch evidence and no protection on each episode. Missing or inaccessible evidence blocks eligibility. |

For Jellyfin, set exactly one of `JELLYFIN_USER_ID` or comma-separated `JELLYFIN_USER_IDS`. IDs must be UUIDs; duplicate IDs are rejected. The runner never picks the first user or automatically discovers the users whose state should count. Include every relevant user when using `all-selected`; users outside that list are not checked.

Plex uses `PLEX_LIBRARY` (default: `TV Shows`). With `DEFAULT_DELETE=false`, only shows with the `Delete` genre qualify. With `DEFAULT_DELETE=true`, shows qualify unless protected by `Keep`. **Keep always wins**, including when a show also has `Delete`. Episode playback state is evaluated locally rather than relying on show-level filters. Duplicate or ambiguous TVDB mappings are retained.

Jellyfin requires explicit `IsFavorite=false` on both the series and episode, `Played=true`, a timezone-aware played timestamp, and a zero playback position. Missing metadata is conservative. Paging requires consistent counts, offsets and unique item IDs; inconsistent responses stop the run. Favourite protection applies to every selected user.

## Apply a bounded plan

Review a preview before running:

```sh
.venv/bin/python src/delete_watched_episodes.py --apply --max-files 10 --max-bytes 10000000000
```

The default limits are 10 files and 10 GB (decimal bytes). If the complete plan exceeds either limit, the run makes no mutations; it never silently deletes a truncated subset. Preview remains available when the plan exceeds apply limits.

Each physical file is planned once. Every episode in that file must qualify. Incomplete, duplicate or contradictory Sonarr episode metadata skips the entire series. A valid file ID, series ID, path, size and import date are required for planning.

Before unmonitoring, and again before deletion, the runner refreshes media watch eligibility, the Sonarr series mapping, every episode's file membership and the file fingerprint. Changed or unavailable evidence stops further operations. Full media discovery during these checks favours conservative decisions and can be slow for large libraries.

Only the selected file's episodes are unmonitored. The runner never unmonitors a whole season, empties library trash or triggers a library-wide refresh. It does not infer season completion from the last numbered episode.

## Outcomes and remaining limits

The command prints JSON to stdout and exits 1 on configuration errors, discovery failures, blocked applies or partial results. Apply results include `deleted`, `blocked` (no mutation attempted for that file), `unconfirmed` (a mutation was attempted), and `not_attempted`. Partial reports identify the phase and number of confirmed unmonitor calls. Sonarr calls have bounded timeouts, reject unexpected HTTP statuses and do not retry or follow redirects.

Unmonitoring and deletion are separate remote actions. An error or a changed eligibility check after unmonitoring can leave episodes unmonitored with their file still present. A timeout can happen after Sonarr completed a deletion. Inspect Sonarr and the media server before any later apply; the runner does not automatically restore monitoring or retry mutations.

This PR remains a draft for supervised use. It does not provide a cross-provider transaction, a persistent operation journal, a process lock, or crash recovery. Another process or user can change state after the final check. Do not run concurrent applies or add apply to an unattended schedule yet. No live library compatibility check has been performed.

Apply performs new discovery rather than applying a saved, approved plan. The set of proposed files can differ from an earlier preview. Existing schedules that invoke the script without arguments now preview only, and need the explicit watch-policy configuration.

Output goes to the console; legacy `LOG_FILE` and rotating-log settings are not used. Capture the JSON report if you need a record of a supervised run. Do not enable SDK debug logging with production credentials.

## Container

```sh
docker build -t sonarr-cleanup .
docker run --rm --env-file .env sonarr-cleanup
docker run --rm --env-file .env sonarr-cleanup --apply --max-files 10 --max-bytes 10000000000
```

The container defaults to preview. Credentials, local environments, logs and tests are excluded from the image; configuration is supplied at runtime.

## Development

Application code lives in `src/`. Automated tests and their provider fixtures live in `test/` and `test/fixtures/`. The small root-level `delete_watched_episodes.py` launcher preserves existing script commands and schedules.

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q test
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Tests use synthetic Plex XML and Jellyfin/Sonarr JSON fixtures. They exercise the pinned clients at their parsing or HTTP boundary without querying real libraries. Coverage includes shared files, retention boundaries, favourites, all-selected users, duplicate mappings, inconsistent pagination, replaced files, changed watch state, deletion limits and uncertain outcomes. CI runs tests and formatting on Python 3.11–3.14, plus a container startup smoke test.

Provider references: [PlexAPI library filtering](https://python-plexapi.readthedocs.io/en/latest/modules/library.html), [Jellyfin user data](https://typescript-sdk.jellyfin.org/interfaces/generated-client.UserItemDataDto.html), and [Sonarr API](https://sonarr.tv/docs/api/).

The existing licence is unchanged. See [LICENSE.md](LICENSE.md).
