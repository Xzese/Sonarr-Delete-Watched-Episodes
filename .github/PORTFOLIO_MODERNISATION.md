# Safety-first cleanup modernisation

Status: implementation started; keep this PR in draft.

## Implemented
- Import-safe command entry point and read-only default.
- Explicit --apply and a per-run file limit checked before mutation.
- Separate pure planner and executor.
- Physical-file grouping; all contained episodes must qualify.
- Conservative missing/duplicate ID handling and Sonarr membership rechecks.
- No season-wide unmonitoring, library trash emptying or automatic retry.
- Explicit Jellyfin user selection, conservative favourite handling and paging checks.
- Clear invalid-retention errors and structured partial outcomes.
- Nineteen mocked tests passed locally; exploratory test.py removed.

## Remaining before unattended use
- Fixture and live compatibility checks for Plex, Jellyfin and Sonarr clients.
- Refresh media-provider eligibility immediately before apply and document unavoidable remote races.
- Explicit multi-user policy for shared libraries.
- Persistent operation journal, process ownership and uncertain-outcome reconciliation.
- Saved-plan review/approval, byte limits, richer operation reasons and reporting.
- Restore configurable file logging as a separate component if needed.
- Packaging, formatting, CI and container smoke tests.

No real library was queried or changed. This is a first implementation, not a complete deletion-safety guarantee.
