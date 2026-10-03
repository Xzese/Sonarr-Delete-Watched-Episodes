"""Pure cleanup planning and an explicitly enabled mutation boundary."""
from dataclasses import dataclass
from collections import defaultdict


@dataclass(frozen=True)
class FilePlan:
    series_id: int
    file_id: int
    episodes: tuple[tuple[int, int], ...]  # (Sonarr episode ID, TVDB ID)


def positive_id(value) -> int:
    if isinstance(value, bool):
        raise ValueError("A positive numeric ID is required.")
    text = str(value)
    if not text.isascii() or not text.isdigit() or int(text) <= 0:
        raise ValueError("A positive numeric ID is required.")
    return int(text)


def plan_series(series_id, episodes, eligible_tvdb_ids):
    """A physical file is eligible only when all its episodes have watch evidence."""
    series_id = positive_id(series_id)
    eligible = {positive_id(item) for item in eligible_tvdb_ids}
    groups = defaultdict(list)
    seen = set()
    for episode in episodes:
        if episode.get("hasFile") is not True:
            continue
        try:
            episode_id = positive_id(episode.get("id"))
            file_id = positive_id(episode.get("episodeFileId"))
            tvdb_id = positive_id(episode.get("tvdbId"))
        except ValueError:
            return [], ["Series skipped: an existing file has incomplete identifiers."]
        if episode_id in seen:
            return [], ["Series skipped: duplicate episode identifiers."]
        seen.add(episode_id)
        groups[file_id].append((episode_id, tvdb_id))
    plans, reasons = [], []
    for file_id, members in sorted(groups.items()):
        if all(tvdb in eligible for _, tvdb in members):
            plans.append(FilePlan(series_id, file_id, tuple(sorted(members))))
        else:
            reasons.append(f"File {file_id} retained: not every episode has eligible watch evidence.")
    return plans, reasons


def execute_plans(client, plans, *, apply=False, max_files=10):
    """Recheck Sonarr membership before each file. Stop on the first failed change.

    This is not a transaction across providers. Media-server watch state can
    change after discovery. No season-wide or library-trash mutation is made.
    """
    if isinstance(max_files, bool) or not isinstance(max_files, int) or max_files < 1:
        raise ValueError("max_files must be a positive integer.")
    plans = tuple(plans)
    if len({p.file_id for p in plans}) != len(plans):
        raise ValueError("Duplicate file operations are not permitted.")
    if not apply:
        return [{"file_id": p.file_id, "status": "preview"} for p in plans]
    if len(plans) > max_files:
        raise ValueError("The plan exceeds the deletion limit. No changes were made.")
    results = []
    for plan in plans:
        changed = 0
        try:
            fresh = client.get_episode(id_=plan.series_id, series=True)
            rechecked, _ = plan_series(plan.series_id, fresh, [tvdb for _, tvdb in plan.episodes])
            if plan not in rechecked:
                results.append({"file_id": plan.file_id, "status": "blocked", "reason": "Sonarr file membership or identifiers changed."})
                break
            for episode_id, _ in plan.episodes:
                client.upd_episode(episode_id, {"monitored": False})
                changed += 1
            client.del_episode_file(plan.file_id)
            results.append({"file_id": plan.file_id, "status": "deleted"})
        except Exception:
            results.append({"file_id": plan.file_id, "status": "unconfirmed", "unmonitor_calls_confirmed": changed, "reason": "An API call failed. Inspect remote state before another apply run."})
            break
    for plan in plans[len(results):]:
        results.append({"file_id": plan.file_id, "status": "not_attempted"})
    return results
