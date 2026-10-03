# Safety-first modernisation

Placeholder for modernising the watched-episode cleanup tool with stronger safeguards around destructive operations.

## Scope
- Separate discovery/planning from mutation.
- Add a read-only preview/dry-run mode that explains every proposed deletion and exclusion.
- Require an explicit apply action for destructive changes.
- Revalidate relevant state immediately before deletion.
- Handle multi-episode files conservatively: delete a file only when every contained episode is eligible.
- Rework season-unmonitor logic so watching the final numbered episode does not by itself imply the whole season is complete.
- Require explicit Jellyfin user selection and define multi-user policy rather than selecting the first returned user.
- Reject unsafe or invalid configuration instead of silently selecting destructive defaults.
- Add deletion limits, structured operation results and partial-failure reporting.
- Replace the exploratory test script with automated fixtures for Plex, Jellyfin and Sonarr edge cases.
- Add CI and document the safety model.

## Portfolio outcome
Present this as an example of careful automation engineering where missing evidence fails closed and destructive work is previewable and testable.

No implementation is included in this placeholder PR.