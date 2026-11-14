"""Which local day a record belongs to, which file it goes in, and the daily summary."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hkacommon.encoding import is_instant, parse_instant

SLEEP = "HKCategoryTypeIdentifierSleepAnalysis"
WORKOUT = "HKWorkoutTypeIdentifier"
HEART_RATE = "HKQuantityTypeIdentifierHeartRate"
RESTING_HR = "HKQuantityTypeIdentifierRestingHeartRate"
HRV_SDNN = "HKQuantityTypeIdentifierHeartRateVariabilitySDNN"

# Cumulative types summed per day (one source chosen per type to avoid double counting).
CUMULATIVE = (
    "HKQuantityTypeIdentifierStepCount",
    "HKQuantityTypeIdentifierDistanceWalkingRunning",
    "HKQuantityTypeIdentifierDistanceCycling",
    "HKQuantityTypeIdentifierActiveEnergyBurned",
    "HKQuantityTypeIdentifierBasalEnergyBurned",
    "HKQuantityTypeIdentifierFlightsClimbed",
    "HKQuantityTypeIdentifierAppleExerciseTime",
)

_zones: dict[str, ZoneInfo] = {}


def zone(name: str | None) -> ZoneInfo:
    key = name or "UTC"
    if key not in _zones:
        try:
            _zones[key] = ZoneInfo(key)
        except (ZoneInfoNotFoundError, ValueError):
            _zones[key] = ZoneInfo("UTC")
    return _zones[key]


def local_day(instant: object, tz: object) -> str | None:
    """Local calendar date of an instant, or None if the instant is not in the protocol's format."""
    if not is_instant(instant):
        return None
    try:
        return parse_instant(instant).astimezone(zone(tz if isinstance(tz, str) else None)).date().isoformat()
    except (OverflowError, ValueError):  # e.g. year 1 shifted west of UTC
        return None


def record_tz(rec: dict, batch_tz: str | None) -> str | None:
    meta = rec.get("metadata")
    tz = meta.get("HKMetadataKeyTimeZone") if isinstance(meta, dict) else None
    return tz if isinstance(tz, str) else batch_tz


def file_name(rec: dict) -> str:
    """One NDJSON file per HealthKit type (or per record kind when it has no type)."""
    name = rec.get("type") if isinstance(rec.get("type"), str) else rec.get("k", "unknown")
    if rec.get("k") == "route":
        name = "HKWorkoutRoute"
    elif rec.get("k") == "object" and isinstance(rec.get("class"), str) and not isinstance(rec.get("type"), str):
        name = rec["class"]
    safe = "".join(c if c.isalnum() or c in "_-." else "_" for c in str(name))
    return f"{safe}.ndjson"


def assign_day(rec: dict, batch_tz: str | None, workout_day) -> str | None:
    """Sleep belongs to the day it ends on; routes to their workout's day; the rest to the day they start."""
    tz = record_tz(rec, batch_tz)
    if rec.get("k") == "route":
        day = workout_day(rec.get("workout_uuid"))
        if day:
            return day
    if rec.get("type") == SLEEP and is_instant(rec.get("end")):
        return local_day(rec["end"], tz)
    return local_day(rec.get("start"), tz)


def sort_key(rec: dict) -> tuple:
    start = rec.get("start") if isinstance(rec.get("start"), str) else ""
    uuid = rec.get("uuid") if isinstance(rec.get("uuid"), str) else ""
    part = rec.get("part") if type(rec.get("part")) is int else 0
    return (start, uuid, part)


# ---------------------------------------------------------------------------- summary

# HKCategoryValueSleepAnalysis raw values, for records without value_name.
SLEEP_VALUES = {0: "inBed", 1: "asleepUnspecified", 2: "awake", 3: "asleepCore", 4: "asleepDeep", 5: "asleepREM"}


def _num(rec: dict) -> float | None:
    v = rec.get("value")
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _source(rec: dict) -> str:
    src = rec.get("source")
    bundle = src.get("bundle_id") if isinstance(src, dict) else None
    return bundle if isinstance(bundle, str) else "?"


def _minutes(rec: dict) -> float | None:
    if not (is_instant(rec.get("start")) and is_instant(rec.get("end"))):
        return None
    return (parse_instant(rec["end"]) - parse_instant(rec["start"])).total_seconds() / 60


def _choose(candidates: dict, priority: list[str], score) -> tuple:
    """Pick one (source, ...) key: the first source matching the priority list, else the best score."""
    for prefix in priority:
        matching = [k for k in candidates if k[0].startswith(prefix)]
        if matching:
            return max(matching, key=lambda k: (score(candidates[k]), k))
    return max(candidates, key=lambda k: (score(candidates[k]), k))


def summarize(day: str, files: dict[str, list[dict]], source_priority: list[str] | None = None) -> dict:
    """Daily aggregates for agents and FHIR. Sources and units are never mixed within one total."""
    priority = [p for p in source_priority if isinstance(p, str)] if isinstance(source_priority, list) else []
    out: dict = {"day": day, "totals": {}, "heart_rate": None, "sleep": None, "workouts": [], "latest": {}}

    for t in CUMULATIVE:
        groups: dict[tuple[str, str], float] = defaultdict(float)
        for r in files.get(f"{t}.ndjson", []):
            v = _num(r)
            if v is not None:
                unit = r.get("unit") if isinstance(r.get("unit"), str) else "?"
                groups[(_source(r), unit)] += v
        if groups:
            source, unit = _choose(groups, priority, lambda total: total)
            out["totals"][t] = {"value": round(groups[(source, unit)], 3), "unit": unit, "source": source,
                                "sources": {f"{s} [{u}]": round(v, 3) for (s, u), v in sorted(groups.items())}}

    hr = [v for v in (_num(r) for r in files.get(f"{HEART_RATE}.ndjson", [])
                      if r.get("unit") in (None, "count/min")) if v is not None]
    if hr:
        out["heart_rate"] = {"unit": "count/min", "min": min(hr), "max": max(hr),
                             "avg": round(sum(hr) / len(hr), 1), "samples": len(hr)}

    for t in (RESTING_HR, HRV_SDNN):
        recs = [r for r in files.get(f"{t}.ndjson", []) if _num(r) is not None]
        if recs:
            last = max(recs, key=sort_key)
            out["latest"][t] = {"value": last["value"], "unit": last.get("unit"), "at": last.get("start")}

    by_source: dict[tuple[str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in files.get(f"{SLEEP}.ndjson", []):
        minutes = _minutes(r)
        if minutes is None:
            continue
        raw = r.get("value")
        name = (r.get("value_name") if isinstance(r.get("value_name"), str)
                else SLEEP_VALUES.get(raw) if type(raw) is int else None)
        by_source[(_source(r),)][name or f"value_{r.get('value')}"] += minutes
    if by_source:
        def asleep(stages: dict[str, float]) -> float:
            return sum(v for k, v in stages.items() if k.startswith("asleep"))

        (source,) = _choose(by_source, priority, asleep)
        stages = by_source[(source,)]
        out["sleep"] = {"source": source, "asleep_min": round(asleep(stages), 1),
                        "stages_min": {k: round(v, 1) for k, v in sorted(stages.items())}}

    for w in sorted(files.get(f"{WORKOUT}.ndjson", []), key=sort_key):
        out["workouts"].append({k: w.get(k) for k in ("uuid", "activity_name", "activity_type", "start", "end",
                                                       "duration_s", "stats")})
    return out
