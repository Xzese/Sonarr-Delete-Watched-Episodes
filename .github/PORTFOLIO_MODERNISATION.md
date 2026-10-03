# Safety-first cleanup modernisation

Status: guarded implementation with automated provider fixtures; keep this PR in draft for supervised validation.

## Implemented

- Import-safe command entry point, JSON preview by default and explicit `--apply`.
- Separate pure planner and mutation boundary with operation details and exclusion reasons.
- File and byte limits checked across the complete plan before mutation.
- Physical-file grouping; every contained episode must qualify.
- Conservative missing, duplicate and contradictory metadata handling.
- Sonarr series, episode membership and file fingerprint rechecks.
- Fresh media eligibility before unmonitoring and again before deletion.
- Explicit watch policy: Plex token user; Jellyfin selected user or intersection of all explicitly selected users.
- Strict retention, URL, user-selection and boolean configuration validation before network clients.
- Jellyfin favourite protection, in-progress protection and pagination consistency checks.
- Plex episode-level playback timestamps, genre protection and ambiguous-mapping exclusions.
- No season-wide unmonitoring, library trash emptying or automatic mutation retry.
- Structured partial outcomes and confirmation of episode unmonitoring responses.
- Bounded Sonarr requests; all unexpected HTTP statuses and redirects rejected.
- Synthetic Plex/Jellyfin/Sonarr fixtures and pinned-SDK contract tests.
- CI for Python 3.11–3.14, lint/format checks and container startup verification.
- Container arguments, build-context exclusions and documented setup/safety model.
- Application modules in `src/`, automated fixtures/tests in `test/`, and a root CLI compatibility launcher.
- Rotating file logs with the original settings/defaults, initialized in `main()` alongside JSON stdout reports; rotation/retention and error-reporting tests.

## Follow-up before unattended use

- Supervised read-only compatibility checks against supported live server versions.
- Persistent operation journal, process ownership and uncertain-outcome reconciliation.
- Saved-plan approval bound to configuration and refreshed state.
- Assessment of unavoidable remote races and provider identity/path matching beyond TVDB metadata.
- Optimise repeated provider discovery without weakening eligibility refresh.

No real library was queried or changed during development. Revalidation does not lock remote servers or make unmonitoring and deletion transactional. The README describes the supervised apply and partial-failure boundaries.
