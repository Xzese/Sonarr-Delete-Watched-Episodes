# Watched episode cleanup for Sonarr

Find watched Plex or Jellyfin episodes and build a file-level cleanup plan.

## Development status

This branch contains the first safety implementation. Keep the PR in draft. Nineteen focused tests passed with mocked clients. No real libraries were queried or changed. Provider contract tests, durable operation records, shared-library policy and concurrent-change handling remain incomplete.

## Preview first

Use Python 3.11 or later. Install `requirements.txt`, copy `.env.example` to `.env`, and configure Sonarr plus one media provider.

```sh
python delete_watched_episodes.py
```

The default is read-only. Output is a JSON report with proposed file operations and exclusion reasons. Importing the module no longer starts clients, creates logs or performs cleanup.

Only an explicit apply invocation permits unmonitoring and deletion:

```sh
python delete_watched_episodes.py --apply --max-files 10
```

Do not add this command to an unattended schedule before reviewing the remaining safety work. A new apply invocation performs fresh discovery; it does not apply a saved, approved plan. Existing schedules that run without arguments now preview only.

## Current rules

Every episode associated with a file must have eligible watch evidence. Missing or duplicate episode identifiers block planning for that series. File operations are deduplicated. The apply limit is checked before any mutation. Sonarr file membership and identifiers are fetched again before each file operation.

Plex uses the authenticated token's watch state and the configured library. DEFAULT_DELETE now defaults to false: only shows with the Delete genre qualify. With DEFAULT_DELETE=true, the Keep genre is excluded. Shared-user aggregation is not implemented.

Jellyfin requires an explicit JELLYFIN_USER_ID. It never selects the first returned user. Both the episode and series must be explicitly non-favourites; missing favourite metadata is conservative and skips the item. Played dates require a timezone. Incomplete paging stops the run.

Season-wide unmonitoring and library trash emptying have been removed from the apply path. No library-wide refresh is performed in this first pass.

## Failure boundaries

Unmonitoring and deletion are separate remote actions, not one transaction. On an API exception, the run reports an unconfirmed result, records the number of completed unmonitor calls and stops further operations. Inspect remote state before another apply run. There is not yet a persistent block preventing a later process from retrying.

The membership recheck does not lock Sonarr or refresh watch state across all providers. A later change remains possible. Do not describe this implementation as race-free or safe for every shared library.

Output goes to the console. The earlier LOG_FILE and rotating-log configuration are not used by this first-pass runner; capture console output when testing.

## Tests

Install pytest and run `python -m pytest -q tests`. The unfinished exploratory `test.py` has been replaced with regression tests. Tests cover preview without mutations, shared episode files, missing IDs, changed membership, limits, partial failure, retention validation and explicit user selection.

The existing licence is unchanged. See LICENSE.md.
