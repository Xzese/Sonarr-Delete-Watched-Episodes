"""Pure file-level planning and the explicitly enabled mutation boundary."""

from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath


class CleanupError(ValueError):
    """A locally generated, credential-free reason safe for user reports."""


@dataclass(frozen=True)
class FileIdentity:
    path: str
    size: int
    date_added: str


@dataclass(frozen=True)
class FilePlan:
    series_id: int
    file_id: int
    episodes: tuple[tuple[int, int], ...]  # (Sonarr episode ID, TVDB ID)
    identity: FileIdentity | None = None


def positive_id(value) -> int:
    if isinstance(value, bool):
        raise CleanupError("A positive numeric ID is required.")
    text = str(value)
    if not text.isascii() or not text.isdigit() or int(text) <= 0:
        raise CleanupError("A positive numeric ID is required.")
    return int(text)


def plan_series(series_id, episodes, eligible_tvdb_ids):
    """Require complete, unambiguous membership for every physical file."""
    series_id = positive_id(series_id)
    eligible = {positive_id(item) for item in eligible_tvdb_ids}
    if not isinstance(episodes, list):
        raise CleanupError("Sonarr episodes must be a complete list.")
    groups = defaultdict(list)
    seen, seen_tvdb = set(), set()
    for episode in episodes:
        try:
            if not isinstance(episode, dict):
                raise CleanupError
            episode_id = positive_id(episode.get("id"))
            if episode_id in seen:
                raise CleanupError
            seen.add(episode_id)
            if "seriesId" in episode and positive_id(episode["seriesId"]) != series_id:
                raise CleanupError
            if type(episode.get("hasFile")) is not bool:
                raise CleanupError
            raw_tvdb = episode.get("tvdbId")
            tvdb_id = positive_id(raw_tvdb) if raw_tvdb not in (None, 0, "0") else None
            if tvdb_id is not None:
                if tvdb_id in seen_tvdb:
                    raise CleanupError
                seen_tvdb.add(tvdb_id)
            if episode["hasFile"] is False:
                if type(episode.get("episodeFileId")) is not int or episode["episodeFileId"] != 0:
                    raise CleanupError
                continue
            file_id = positive_id(episode.get("episodeFileId"))
            if tvdb_id is None:
                raise CleanupError
        except ValueError:
            return [], [f"Series {series_id} skipped: incomplete or ambiguous episode metadata."]
        groups[file_id].append((episode_id, tvdb_id))
    plans, reasons = [], []
    for file_id, members in sorted(groups.items()):
        if all(tvdb in eligible for _, tvdb in members):
            plans.append(FilePlan(series_id, file_id, tuple(sorted(members))))
        else:
            reasons.append(
                f"Series {series_id}, file {file_id} retained: not every episode has eligible watch evidence."
            )
    return plans, reasons


def file_identity(data, *, file_id, series_id):
    if not isinstance(data, dict):
        raise CleanupError("Missing Sonarr file metadata.")
    if positive_id(data.get("id")) != file_id or positive_id(data.get("seriesId")) != series_id:
        raise CleanupError("Sonarr file identity does not match its episode membership.")
    path, date_added = data.get("path"), data.get("dateAdded")
    size = data.get("size")
    if (
        not isinstance(path, str)
        or not path.strip()
        or not isinstance(date_added, str)
        or not date_added.strip()
    ):
        raise CleanupError("Incomplete Sonarr file fingerprint.")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise CleanupError("Sonarr file size must be a positive integer.")
    if not PurePosixPath(path).is_absolute() and not PureWindowsPath(path).is_absolute():
        raise CleanupError("Sonarr file path must be absolute.")
    try:
        stamp = datetime.fromisoformat(date_added.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError
    except ValueError:
        raise CleanupError("Sonarr file import date must have a valid timezone.") from None
    return FileIdentity(path, size, date_added)


def attach_file_metadata(client, plans):
    selected, reasons = [], []
    for plan in plans:
        try:
            identity = file_identity(
                client.get_episode_file(plan.file_id),
                file_id=plan.file_id,
                series_id=plan.series_id,
            )
        except ValueError:
            reasons.append(
                f"Series {plan.series_id}, file {plan.file_id} retained: invalid file metadata."
            )
            continue
        selected.append(replace(plan, identity=identity))
    return selected, reasons


def operation_report(plan, status, **extra):
    return {**asdict(plan), "status": status, **extra}


def execute_plans(
    client, plans, *, apply=False, max_files=10, max_bytes=10_000_000_000, revalidate=None
):
    """Check limits globally; stop on changed eligibility, metadata or any failed call.

    Revalidation is mandatory for apply. It is read-only and must refresh media
    eligibility and Sonarr series mapping. There is no cross-provider transaction.
    A zero limit disables only that cap.
    """
    for name, limit in (("max_files", max_files), ("max_bytes", max_bytes)):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise CleanupError(f"{name} must be a non-negative integer; 0 disables that limit.")
    plans = tuple(plans)
    if len({p.file_id for p in plans}) != len(plans):
        raise CleanupError("Duplicate file operations are not permitted.")
    if not apply:
        return [
            operation_report(
                p, "preview", reason="Every contained episode has eligible watch evidence."
            )
            for p in plans
        ]
    if revalidate is None or any(p.identity is None for p in plans):
        raise CleanupError("Apply requires file fingerprints and fresh provider revalidation.")
    if (max_files > 0 and len(plans) > max_files) or (
        max_bytes > 0 and sum(p.identity.size for p in plans) > max_bytes
    ):
        raise CleanupError("The plan exceeds a deletion limit. No changes were made.")

    def check(plan):
        if revalidate(plan) is not True:
            raise CleanupError("Media eligibility or Sonarr series mapping changed.")
        fresh = client.get_episode(id_=plan.series_id, series=True)
        rechecked, _ = plan_series(plan.series_id, fresh, [tvdb for _, tvdb in plan.episodes])
        if replace(plan, identity=None) not in rechecked:
            raise CleanupError("Sonarr file membership or identifiers changed.")
        identity = file_identity(
            client.get_episode_file(plan.file_id), file_id=plan.file_id, series_id=plan.series_id
        )
        if identity != plan.identity:
            raise CleanupError("Sonarr file fingerprint changed.")

    results = []
    for plan in plans:
        changed, attempted, phase = 0, False, "revalidation"
        try:
            check(plan)
            for episode_id, _ in plan.episodes:
                phase, attempted = "unmonitor", True
                updated = client.upd_episode(episode_id, {"monitored": False})
                if (
                    not isinstance(updated, dict)
                    or positive_id(updated.get("id")) != episode_id
                    or updated.get("monitored") is not False
                ):
                    raise CleanupError("Sonarr did not confirm unmonitoring.")
                changed += 1
            # Unmonitor calls may take time; check again immediately before deletion.
            phase = "revalidation_before_delete"
            check(plan)
            phase = "delete"
            client.del_episode_file(plan.file_id)
            results.append(operation_report(plan, "deleted", unmonitor_calls_confirmed=changed))
        except Exception as error:
            status = "unconfirmed" if attempted else "blocked"
            # Never expose raw SDK exceptions, URLs or authentication material.
            reason = (
                str(error)
                if isinstance(error, CleanupError)
                else "An API call failed or returned invalid data."
            )
            results.append(
                operation_report(
                    plan,
                    status,
                    phase=phase,
                    unmonitor_calls_confirmed=changed,
                    reason=f"{reason} Inspect remote state before another apply run.",
                )
            )
            break
    results.extend(operation_report(p, "not_attempted") for p in plans[len(results) :])
    return results
