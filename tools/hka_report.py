#!/usr/bin/env python3
"""PDF reports from a family member's HealthKit data in the local Medplum: one day, one workout,
or a period (week, month, or any range).

Reports are made in two steps so that the evaluation is written by a person or an agent from
computed facts, never invented by this script:

  1. facts   python3 tools/hka_report.py day --date 2026-11-13 [--person P] --facts facts.json
             python3 tools/hka_report.py workout <workout uuid> --facts facts.json
             python3 tools/hka_report.py period --week 2026-W45 [--person P] --facts facts.json
  2. render  the same command with --narrative narrative.json [--out report.pdf]

Period options: --week YYYY-Www | --month YYYY-MM | --from YYYY-MM-DD --to YYYY-MM-DD.
PERSON is a Patient id, a user id (u_...) or a name (see `tools/medplum_agent.py people`); it can
be left out when there is only one person. Workout option: --route adds the GPS route shape (off by
default: it shows where the person lives and runs).

narrative.json:
  {"language": "en",                       optional; built in: en, vi
   "headline": "one sentence",
   "summary": "a short paragraph",
   "evaluation": ["finding grounded in the facts", ...],
   "recommendations": ["concrete, doable step", ...],
   "data_notes": ["..."],                  optional: the facts' data_notes in the report language
   "labels": {"summary": "...", ...}}      optional: titles, axes, table headers for other languages
                                           (python3 tools/hka_report.py labels --lang en lists them)

Fonts: the PDF and charts embed DejaVu Sans (shipped with matplotlib; Latin including Vietnamese,
Greek, Cyrillic). For other scripts pass a TrueType font with --font FILE.ttf [--font-bold FILE.ttf]
or HKA_REPORT_FONT(_BOLD); the script refuses to render text the font cannot show.

Data comes from Medplum through tools/medplum_agent.py (read-only client). Reports are written to
reports/ in the repository (ignored by git, mode 600), or $HKA_REPORT_DIR, unless --out says otherwise.

Optional dependencies (the rest of the project needs only the standard library):
  python3 -m pip install -r tools/requirements-reports.txt
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import statistics as st
import sys
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import medplum_agent as ma  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
REPORT_DIR = Path(os.environ.get("HKA_REPORT_DIR") or REPO / "reports").expanduser()
LOINC = "http://loinc.org"
HKA = ma.HKA_SYSTEM
DEPS_HINT = "reports need matplotlib and reportlab: python3 -m pip install -r tools/requirements-reports.txt"
BASELINE_DAYS = 28
RECENT_WEEKS = 8

# Colors: a validated categorical palette (light mode, print).
INK, INK2, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1", "#ffffff"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]          # categorical slots 1-4, fixed order

# Daily resources: identifier <user>/<day>/<name> (docs/fhir-mapping.md).
DAILY_NAMES = {
    "steps": "HKQuantityTypeIdentifierStepCount", "distance_m": "HKQuantityTypeIdentifierDistanceWalkingRunning",
    "flights": "HKQuantityTypeIdentifierFlightsClimbed", "active_kcal": "HKQuantityTypeIdentifierActiveEnergyBurned",
    "basal_kcal": "HKQuantityTypeIdentifierBasalEnergyBurned", "exercise_min": "HKQuantityTypeIdentifierAppleExerciseTime",
    "cycling_m": "HKQuantityTypeIdentifierDistanceCycling", "hr_min": "hr-min", "hr_max": "hr-max",
}
# Single readings, averaged per local day.
READINGS = {"resting_hr": f"{LOINC}|40443-4", "hrv_ms": f"{LOINC}|80404-7", "spo2_pct": f"{LOINC}|2708-6",
            "resp_rate": f"{LOINC}|9279-1", "weight_kg": f"{LOINC}|29463-7"}
STAGES = (("deep", "93831-6"), ("core", "93830-8"), ("rem", "93829-0"), ("awake", "93828-2"))
# Values that keep growing during the day: today's are partial and stay out of averages.
ACCUMULATING = ("steps", "distance_m", "flights", "active_kcal", "basal_kcal", "exercise_min", "cycling_m", "hr_min",
                "hr_max")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def fail(message: str) -> None:
    print(f"hka_report: {message}", file=sys.stderr)
    sys.exit(2)


def mean(values):
    values = [v for v in values if v is not None]
    return st.fmean(values) if values else None


def rnd(value, digits=1):
    return None if value is None else round(value, digits)


def code_of(cc: dict) -> str:
    """A component's own code: its LOINC or hka-metrics coding, not the HealthKit type it also carries."""
    return next((c["code"] for c in cc.get("coding") or [] if not c["system"].endswith("/healthkit-type")),
                (cc.get("coding") or [{}])[0].get("code"))


def comps(obs: dict | None) -> dict:
    return {code_of(c["code"]): (c.get("valueQuantity") or {}).get("value") if c.get("valueQuantity")
            else (c.get("valueCodeableConcept") or {}).get("text") for c in (obs or {}).get("component") or []}


def value(obs: dict | None):
    return ((obs or {}).get("valueQuantity") or {}).get("value")


def when(obs: dict) -> str:
    return obs.get("effectiveDateTime") or (obs.get("effectivePeriod") or {}).get("start") or ""


def parse(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def parse_offset(offset: str) -> dt.timezone:
    sign = -1 if offset.startswith("-") else 1
    hours, minutes = (int(x) for x in offset[1:].split(":"))
    return dt.timezone(sign * dt.timedelta(hours=hours, minutes=minutes))


def pace_text(min_per_km):
    if not min_per_km:
        return "-"
    m = int(min_per_km)
    s = round((min_per_km - m) * 60)
    if s == 60:
        m, s = m + 1, 0
    return f"{m}:{s:02d}/km"


def dur_text(seconds):
    if seconds is None:
        return "-"
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def days_between(start: dt.date, end: dt.date) -> list[dt.date]:
    return [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]


# --------------------------------------------------------------------------
# Reading Medplum
# --------------------------------------------------------------------------

OBS_FIELDS = ("identifier { value } status effectivePeriod { start end } effectiveDateTime valueQuantity { value unit } "
              "dataAbsentReason { text } device { display } "
              "component { code { coding { system code } } valueQuantity { value unit } valueCodeableConcept { text } } "
              "valueSampledData { period data }")


class Person:
    def __init__(self, choice: str | None):
        everyone = ma.people()
        patient = ma.resolve_person(choice)
        p = next(x for x in everyone if x["patient"] == patient)
        self.patient, self.user, self.name = p["patient"], p["user"], p["name"]
        self.birth_date, self.gender = p.get("birthDate"), p.get("gender")
        self.offset = p.get("utc_offset") or "+00:00"
        self.tz = parse_offset(self.offset)
        self.count = len(everyone)

    def iso(self, day: dt.date) -> str:
        return dt.datetime.combine(day, dt.time(), self.tz).isoformat()

    def ref(self) -> str:
        return f"Patient/{self.patient}"


def gql(query: str, variables: dict | None = None) -> dict:
    return ma.data_of(ma.graphql(query, variables))


def daily_resources(person: Person, days: list[dt.date], names: list[str]) -> dict[tuple[str, str], dict]:
    """Daily Observations by (day, name), fetched by identifier in batches."""
    keys = [f"{person.user}/{d.isoformat()}/{n}" for d in days for n in names]
    out = {}
    for i in range(0, len(keys), 80):
        res = gql('query($i: String) { ObservationList(identifier: $i, _count: 1000) { %s } }' % OBS_FIELDS,
                  {"i": ",".join(f"{ma.OBS_SYSTEM}|{k}" for k in keys[i:i + 80])})
        for o in res["ObservationList"]:
            _, day, name = o["identifier"][0]["value"].split("/", 2)
            out[(day, name)] = o
    return out


def readings(person: Person, code: str, start: dt.date, end: dt.date, fields: str = OBS_FIELDS) -> list[dict]:
    """Every Observation of one code for the person from start to end (inclusive), paged."""
    out, offset = [], 0
    flt = f"date ge {person.iso(start)} and date lt {person.iso(end + dt.timedelta(days=1))}"
    while True:
        page = gql('query($s: String, $c: String, $f: String, $o: Int) { ObservationList(subject: $s, code: $c, '
                   '_filter: $f, _sort: "date", _count: 1000, _offset: $o) { %s } }' % fields,
                   {"s": person.ref(), "c": code, "f": flt, "o": offset})["ObservationList"]
        out += page
        if len(page) < 1000:
            return out
        offset += 1000


def sampled_points(obs: dict | None) -> list[tuple[dt.datetime, float]]:
    """(time, value) of a SampledData Observation; 'E' (no reading) is skipped."""
    if not obs or not obs.get("valueSampledData"):
        return []
    start = parse(obs["effectivePeriod"]["start"])
    period = dt.timedelta(milliseconds=float(obs["valueSampledData"]["period"]))
    return [(start + i * period, float(v)) for i, v in enumerate((obs["valueSampledData"].get("data") or "").split())
            if v not in ("E", "L", "U")]


def day_records(person: Person, days: list[dt.date]) -> list[dict]:
    """One record per day: daily totals, last night's sleep and the day's average of each reading."""
    if not days:
        return []
    res = daily_resources(person, days, list(DAILY_NAMES.values()) + ["sleep"])
    by_day: dict[str, dict] = {}
    for key, code in READINGS.items():
        for o in readings(person, code, days[0], days[-1], "effectiveDateTime effectivePeriod { start } valueQuantity { value }"):
            if value(o) is not None:
                by_day.setdefault(when(o)[:10], {}).setdefault(key, []).append(value(o))
    out = []
    for d in days:
        iso = d.isoformat()
        rec = {"date": iso}
        for key, name in DAILY_NAMES.items():
            rec[key] = value(res.get((iso, name)))
        sleep = res.get((iso, "sleep"))
        sc = comps(sleep)
        rec.update({"sleep_min": value(sleep), "in_bed_min": sc.get("sleep-in-bed"),
                    "unspecified_min": sc.get("sleep-unspecified"),
                    **{f"{name}_min": sc.get(code) for name, code in STAGES},
                    "sleep_start": ((sleep or {}).get("effectivePeriod") or {}).get("start"),
                    "sleep_end": ((sleep or {}).get("effectivePeriod") or {}).get("end"),
                    "sleep_source": ((sleep or {}).get("device") or {}).get("display")})
        for key in READINGS:
            vals = by_day.get(iso, {}).get(key)
            rec[key] = rnd(mean(vals)) if vals else None
            if key != "weight_kg":
                rec[f"{key}_readings"] = len(vals) if vals else 0
        rec["total_kcal"] = (rnd(rec["active_kcal"] + rec["basal_kcal"], 0)
                             if rec["active_kcal"] is not None and rec["basal_kcal"] is not None else None)
        rec["status"] = next((o.get("status") for (day, _), o in res.items() if day == iso), None)
        out.append(rec)
    return out


def without_partial_today(records: list[dict], today: dt.date) -> list[dict]:
    return [{**r, **{k: None for k in ACCUMULATING + ("total_kcal",)}} if r["date"] == today.isoformat() else r
            for r in records]


def workout_list(person: Person, start: dt.date, end: dt.date) -> list[dict]:
    rows = ma.list_workouts(person.patient, person.iso(start), person.iso(end + dt.timedelta(days=1)))
    return sorted(rows, key=lambda w: w["start"])


def hr_minutes(person: Person, days: list[dt.date]) -> list[tuple[dt.datetime, float]]:
    res = daily_resources(person, days, ["hr-minutes"])
    return sorted(p for d in days for p in sampled_points(res.get((d.isoformat(), "hr-minutes"))))


def hr_during(points: list[tuple[dt.datetime, float]], start: dt.datetime, end: dt.datetime):
    minute = dt.timedelta(minutes=1)
    return [(t, v) for t, v in points if start - minute < t < end]


def hr_summary(points: list[tuple[dt.datetime, float]]) -> dict:
    vals = [v for _, v in points]
    half = len(vals) // 2
    first, second = mean(vals[:half]), mean(vals[half:])
    return {"readings": len(vals), "avg": rnd(mean(vals)), "max": max(vals) if vals else None,
            "min": min(vals) if vals else None, "first_half_avg": rnd(first), "second_half_avg": rnd(second),
            "drift": rnd(second - first) if first is not None and second is not None and half >= 3 else None}


def compare(this, values: list) -> dict:
    values = [v for v in values if v is not None]
    avg = mean(values)
    return {"this": this, "avg": rnd(avg), "min": min(values) if values else None, "max": max(values) if values else None,
            "diff": rnd(this - avg) if this is not None and avg is not None else None,
            "days_lower": sum(1 for v in values if v < this) if this is not None and values else None,
            "days_with_data": len(values)}


def person_facts(person: Person) -> dict:
    age = None
    if person.birth_date:
        b = dt.date.fromisoformat(person.birth_date)
        today = dt.date.today()
        age = today.year - b.year - ((today.month, today.day) < (b.month, b.day))
    return {"name": person.name, "user": person.user, "gender": person.gender, "age_years": age,
            "utc_offset": person.offset}


SOURCE_NOTE = ("Each daily total comes from one source (the app or device named in the facts); iPhone, watch and "
               "other apps are never added together.")


def workout_notes(workouts: list[dict]) -> list[str]:
    merged = [w for w in workouts if w.get("also_recorded_by")]
    if not merged:
        return []
    apps = sorted({x["source"] or "?" for w in merged for x in w["also_recorded_by"]})
    return [f"{len(merged)} of {len(workouts)} workouts were recorded by more than one app (also by: {', '.join(apps)}); "
            "each is counted once."]


def basal_note(records: list[dict]) -> list[str]:
    vals = [r["basal_kcal"] for r in records if r.get("basal_kcal") is not None]
    if len(vals) >= 5 and len(set(vals)) == 1:
        return [f"Basal energy is the same on every day ({vals[0]:g} kcal): the source writes a fixed estimate, not a "
                "measurement. Total energy therefore only varies with active energy."]
    return []


# --------------------------------------------------------------------------
# Day facts
# --------------------------------------------------------------------------

COMPARED = ("sleep_min", "resting_hr", "hrv_ms", "steps", "distance_m", "active_kcal", "exercise_min", "flights")


def day_facts(person: Person, day: dt.date) -> tuple[dict, dict]:
    today = dt.date.today()
    partial = day == today
    base_days = days_between(day - dt.timedelta(days=BASELINE_DAYS), day - dt.timedelta(days=1))
    records = day_records(person, base_days + [day])
    rec, base = records[-1], records[:-1]
    if partial:
        rec = without_partial_today([rec], today)[0]
    hr = hr_minutes(person, [day])
    workouts = workout_list(person, day, day)
    sleep_window = (rec["sleep_start"], rec["sleep_end"]) if rec["sleep_start"] else None
    in_sleep = lambda t: bool(sleep_window) and parse(sleep_window[0]) <= t <= parse(sleep_window[1])
    in_workout = lambda t: any(parse(w["start"]) <= t <= parse(w["end"]) for w in workouts)
    hourly = {}
    for t, v in hr:
        hourly.setdefault(t.astimezone(person.tz).hour, []).append(v)
    for w in workouts:
        w["heart_rate"] = hr_summary(hr_during(hr, parse(w["start"]), parse(w["end"])))
    stages = {name: rec[f"{name}_min"] for name, _ in STAGES}
    staged = sum(v for v in stages.values() if v) or None
    facts = {
        "kind": "day", "date": day.isoformat(), "person": person_facts(person), "partial_day": partial,
        "status": rec["status"],
        "sleep": {"start": rec["sleep_start"], "end": rec["sleep_end"], "asleep_min": rec["sleep_min"],
                  "in_bed_min": rec["in_bed_min"], "stages_min": stages, "unspecified_min": rec["unspecified_min"],
                  "stages_pct": {k: rnd(100 * v / staged) if v and staged else None for k, v in stages.items()},
                  "source": rec["sleep_source"]},
        "heart_rate": {"resting": rec["resting_hr"], "day_min": rec["hr_min"], "day_max": rec["hr_max"],
                       "minutes_with_reading": len(hr),
                       "asleep_avg": rnd(mean(v for t, v in hr if in_sleep(t))),
                       "awake_avg": rnd(mean(v for t, v in hr if not in_sleep(t) and not in_workout(t))),
                       "hourly_avg": {f"{h:02d}": round(mean(v)) for h, v in sorted(hourly.items())}},
        "hrv_sdnn_ms": {"avg": rec["hrv_ms"], "readings": rec["hrv_ms_readings"]},
        "spo2_pct": {"avg": rec["spo2_pct"], "readings": rec["spo2_pct_readings"]},
        "respiratory_rate": {"avg": rec["resp_rate"], "readings": rec["resp_rate_readings"]},
        "activity": {k: rec[k] for k in ("steps", "distance_m", "flights", "active_kcal", "basal_kcal", "total_kcal",
                                         "exercise_min", "cycling_m")},
        "weight_kg": rec["weight_kg"],
        "workouts": [{k: w[k] for k in ("workout", "start", "end", "type", "minutes", "distance_m", "active_kcal",
                                        "heart_rate", "source", "also_recorded_by")} for w in workouts],
        f"compared_with_previous_{BASELINE_DAYS}_days": {k: compare(rec[k], [b[k] for b in base]) for k in COMPARED},
        "data_notes": [
            "Source: local Medplum. Sleep is the night that ended on this morning (sleep day 18:00 to 18:00).",
            f"Comparison: the {BASELINE_DAYS} days before ({base_days[0]} to {base_days[-1]}); days_lower = how many "
            "of those days had a lower value; days_with_data = how many had a value at all.",
            SOURCE_NOTE,
            "HRV is SDNN (Apple's measure), spot readings during the day; compare it only with this person's own values.",
        ] + (["This is today: last night's sleep and the readings so far count; steps, distance, energy and other "
              "daily totals are partial and left out of the comparison."] if partial else [])
          + (["This day is still preliminary: late syncs can change it."] if rec["status"] == "preliminary"
             and not partial and today - day <= dt.timedelta(days=7) else [])
          + workout_notes(workouts) + basal_note(records),
    }
    series = {"day": day, "tz": person.tz, "hr": hr, "sleep": sleep_window, "workouts": workouts, "stages": stages,
              "unspecified": rec["unspecified_min"], "in_bed": rec["in_bed_min"], "resting": rec["resting_hr"],
              "history": base + [rec]}
    return facts, series


# --------------------------------------------------------------------------
# Period facts
# --------------------------------------------------------------------------


def period_from_args(args) -> tuple[dt.date, dt.date, dict]:
    if args.week:
        year, week = args.week.upper().split("-W")
        start = dt.date.fromisocalendar(int(year), int(week), 1)
        return start, start + dt.timedelta(days=6), {"kind": "week", "value": args.week.upper()}
    if args.month:
        year, month = (int(x) for x in args.month.split("-"))
        start = dt.date(year, month, 1)
        nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
        return start, nxt - dt.timedelta(days=1), {"kind": "month", "value": args.month}
    if args.start and args.end:
        start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
        if end < start:
            fail("--to is before --from")
        return start, end, {"kind": "range", "value": f"{start}/{end}"}
    fail("period needs --week YYYY-Www, --month YYYY-MM, or --from and --to")


AGGREGATED = ("sleep_min", "deep_min", "core_min", "rem_min", "awake_min", "in_bed_min", "resting_hr", "hrv_ms",
              "spo2_pct", "resp_rate", "steps", "distance_m", "flights", "active_kcal", "basal_kcal", "total_kcal",
              "exercise_min")


def aggregate(records: list[dict], workouts: list[dict]) -> dict:
    col = lambda k: [r[k] for r in records if r.get(k) is not None]
    out = {"days": len(records)}
    for k in AGGREGATED:
        vals = col(k)
        out[k] = {"avg": rnd(mean(vals)), "min": min(vals) if vals else None, "max": max(vals) if vals else None,
                  "days_with_data": len(vals)}
    for k in ("steps", "distance_m", "active_kcal", "exercise_min", "flights"):
        out[k]["total"] = rnd(sum(col(k)), 0)
    out["workouts"] = {"count": len(workouts), "minutes": rnd(sum(w.get("minutes") or 0 for w in workouts)),
                       "distance_km": rnd(sum(w.get("distance_m") or 0 for w in workouts) / 1000, 2),
                       "by_type": {t: sum(1 for w in workouts if w.get("type") == t)
                                   for t in sorted({w.get("type") or "?" for w in workouts})}}
    weights = [(r["date"], r["weight_kg"]) for r in records if r.get("weight_kg") is not None]
    out["weight_kg"] = {"first": weights[0] if weights else None, "last": weights[-1] if weights else None,
                        "readings_days": len(weights)}
    return out


def period_facts(person: Person, start: dt.date, end: dt.date, period: dict) -> tuple[dict, dict]:
    today = dt.date.today()
    days = days_between(start, min(end, today))
    length = (end - start).days + 1
    prev_start, prev_end = start - dt.timedelta(days=length), start - dt.timedelta(days=1)
    records = day_records(person, days)
    prev_records = day_records(person, days_between(prev_start, prev_end))
    workouts = workout_list(person, start, end)
    prev_workouts = workout_list(person, prev_start, prev_end)
    hr = hr_minutes(person, sorted({parse(w["start"]).astimezone(person.tz).date() for w in workouts}
                                   | {parse(w["end"]).astimezone(person.tz).date() for w in workouts}))
    for w in workouts:
        w["heart_rate"] = hr_summary(hr_during(hr, parse(w["start"]), parse(w["end"])))
    this = aggregate(without_partial_today(records, today), workouts)
    prev = aggregate(prev_records, prev_workouts)
    delta = {k: rnd(this[k]["avg"] - prev[k]["avg"], 0 if k in ("steps", "distance_m", "active_kcal") else 1)
             if this[k]["avg"] is not None and prev[k]["avg"] is not None else None
             for k in ("sleep_min", "resting_hr", "hrv_ms", "steps", "distance_m", "active_kcal", "exercise_min")}
    has = lambda r: any(r.get(k) is not None for k in ("sleep_min", "steps", "resting_hr"))
    with_data = [r["date"] for r in records if has(r)]
    sources = sorted({r["sleep_source"] for r in records if r.get("sleep_source")})
    coverage = {"days_in_period": length, "days_so_far": len(days), "days_with_data": len(with_data),
                "first_day_with_data": with_data[0] if with_data else None,
                "today_partial": today.isoformat() if start <= today <= end else None,
                "previous_days_with_data": sum(1 for r in prev_records if has(r))}
    coverage["by_metric"] = {k: {"this": this[k]["days_with_data"], "previous": prev[k]["days_with_data"]}
                             for k in ("sleep_min", "resting_hr", "hrv_ms", "steps", "active_kcal", "exercise_min")}
    thin = [k for k, c in coverage["by_metric"].items()
            if 0 < max(c["this"], c["previous"]) and min(c["this"], c["previous"]) < length / 2]
    pick = lambda key, best: (best((r for r in records if r.get(key) is not None), key=lambda r: r[key], default={})
                              .get("date"))
    facts = {
        "kind": "period", "period": period, "from": start.isoformat(), "to": end.isoformat(),
        "person": person_facts(person), "in_progress": end >= today, "coverage": coverage,
        "summary": this, "previous_period": {"from": prev_start.isoformat(), "to": prev_end.isoformat(), **prev},
        "change_vs_previous_avg": delta,
        "best_and_worst": {"longest_sleep": pick("sleep_min", max), "shortest_sleep": pick("sleep_min", min),
                           "most_steps": pick("steps", max), "fewest_steps": pick("steps", min),
                           "highest_resting_hr": pick("resting_hr", max), "lowest_resting_hr": pick("resting_hr", min)},
        "days": records,
        "workouts": [{k: w[k] for k in ("workout", "start", "end", "type", "minutes", "distance_m", "active_kcal",
                                        "heart_rate", "source", "also_recorded_by")} for w in workouts],
        "sleep_sources": sources,
        "data_notes": [
            "Source: local Medplum. Sleep is dated by the morning it ends (sleep day 18:00 to 18:00).",
            SOURCE_NOTE,
            "HRV is SDNN (Apple's measure); compare it only with this person's own values.",
            "Active and basal energy are separate; total = active + basal.",
        ] + ([f"The period is still in progress: {len(days)} of {length} days so far. Today ({today}) counts for last "
              "night's sleep and today's readings; its steps, distance and energy are partial and left out of the "
              "averages."] if coverage["today_partial"] else [])
          + ([f"Data starts on {coverage['first_day_with_data']}; earlier days of the period have no data."]
             if coverage["first_day_with_data"] and coverage["first_day_with_data"] > start.isoformat() else [])
          + ([f"The previous period ({prev_start} to {prev_end}) has data on only {coverage['previous_days_with_data']} "
              "days, so the comparison is weak."] if coverage["previous_days_with_data"] < length / 2 else [])
          + (["Thin coverage (days with a value, this period / previous of " + str(length) + "): "
              + "; ".join(f"{k} {coverage['by_metric'][k]['this']} / {coverage['by_metric'][k]['previous']}" for k in thin)
              + ". Averages and changes of these metrics rest on few days."] if thin else [])
          + (["Workout heart rate is the one-minute series cut to the workout (sources pooled); some sources write "
              "a reading only every few minutes."] if workouts else [])
          + workout_notes(workouts) + basal_note(records),
    }
    return facts, {"days": records, "workouts": workouts, "from": start, "to": end}


# --------------------------------------------------------------------------
# Workout facts
# --------------------------------------------------------------------------


def workout_facts(uuid: str, route: bool) -> tuple[dict, dict]:
    obs = gql('query($i: String) { ObservationList(identifier: $i) { subject { reference } %s } }' % OBS_FIELDS,
              {"i": f"{ma.OBS_SYSTEM}|{uuid}"})["ObservationList"]
    if not obs:
        fail(f"no workout {uuid} (see: python3 tools/medplum_agent.py workouts)")
    o = obs[0]
    person = Person(o["subject"]["reference"].split("/", 1)[1])
    same = [w for w in ma.list_workouts(person.patient, o["effectivePeriod"]["start"], o["effectivePeriod"]["end"])
            if uuid in [w["workout"]] + [x["workout"] for x in w["also_recorded_by"]]]
    also = [x for w in same for x in [{"source": w["source"], "workout": w["workout"]}] + w["also_recorded_by"]
            if x["workout"] != uuid]
    c = comps(o)
    start, end = parse(o["effectivePeriod"]["start"]), parse(o["effectivePeriod"]["end"])
    day = start.astimezone(person.tz).date()
    kind = c.get("73985-4")
    minutes = value(o)
    distance = c.get("workout-distance")
    hr = hr_minutes(person, sorted({day, end.astimezone(person.tz).date()}))
    during = hr_during(hr, start, end)
    hrs = hr_summary(during)
    pace = (minutes / (distance / 1000)) if minutes and distance else None
    route_info, rows = {"present": False}, []
    has_route = bool(gql('query($i: String) { DocumentReferenceList(identifier: $i) { id } }',
                         {"i": f"{ma.DOC_SYSTEM}|route/{uuid}"})["DocumentReferenceList"])
    if has_route:
        err = io.StringIO()
        with redirect_stderr(err):
            rows = ma.route_rows(uuid, gps=True)
        timed = "not usable" not in err.getvalue()
        alts = [r["altitude_m"] for r in rows if r.get("altitude_m") is not None]
        gain = sum(max(0.0, b - a) for a, b in zip(alts, alts[1:])) if len(alts) > 1 else None
        route_info = {"present": True, "points": len(rows), "distance_m": rows[-1]["distance_m"] if rows else None,
                      "point_times_usable": timed, "elevation_gain_m": rnd(gain, 0),
                      "altitude_range_m": [min(alts), max(alts)] if alts else None}
    splits = km_splits(rows, during) if has_route and route_info["point_times_usable"] else []
    morning = day_records(person, [day])[0]
    recent = [w for w in workout_list(person, day - dt.timedelta(weeks=RECENT_WEEKS), day - dt.timedelta(days=1))
              if w.get("type") == kind]
    recent_hr = hr_minutes(person, sorted({parse(w["start"]).astimezone(person.tz).date() for w in recent}))
    history = []
    for w in recent:
        h = hr_summary(hr_during(recent_hr, parse(w["start"]), parse(w["end"])))
        history.append({"start": w["start"], "minutes": w["minutes"], "distance_m": w["distance_m"],
                        "pace_min_per_km": rnd(w["minutes"] / (w["distance_m"] / 1000), 2)
                        if w.get("minutes") and w.get("distance_m") else None,
                        "avg_hr": h["avg"], "hr_readings": h["readings"]})

    def efficiency(minutes_, distance_, avg_hr):
        """metres per heartbeat: distance / (minutes x average heart rate)."""
        return rnd(distance_ / (minutes_ * avg_hr), 3) if minutes_ and distance_ and avg_hr else None
    eff = efficiency(minutes, distance, hrs["avg"])
    past = [efficiency(h["minutes"], h["distance_m"], h["avg_hr"]) for h in history]
    past = [p for p in past if p is not None]
    facts = {
        "kind": "workout", "workout": uuid, "person": person_facts(person), "type": kind,
        "start": o["effectivePeriod"]["start"], "end": o["effectivePeriod"]["end"], "source": (o.get("device") or {}).get("display"),
        "summary": {"duration_min": minutes, "elapsed_min": rnd((end - start).total_seconds() / 60),
                    "distance_m": distance, "pace_min_per_km": rnd(pace, 2), "pace": pace_text(pace) if pace else None,
                    "active_kcal": c.get("55424-6"), "avg_hr_recorded": c.get("55425-3"), "max_hr_recorded": c.get("55426-1")},
        "heart_rate_from_minute_series": hrs,
        "heart_rate_by_10_min": by_ten_minutes(during, start),
        "route": route_info, "splits_km": splits,
        "that_day": {"sleep_min": morning["sleep_min"], "deep_min": morning["deep_min"], "rem_min": morning["rem_min"],
                     "resting_hr": morning["resting_hr"], "hrv_ms": morning["hrv_ms"]},
        f"same_type_previous_{RECENT_WEEKS}_weeks": history,
        "efficiency_m_per_beat": {"this": eff, "previous_median": rnd(st.median(past), 3) if past else None,
                                  "previous_count": len(past)},
        "also_recorded_by": also,
        "data_notes": [
            "Source: local Medplum. HealthKit keeps no second-by-second workout file; heart rate here is the "
            "one-minute series of the day cut to the workout (all sources pooled), so its density depends on the "
            "source (an Apple Watch reads every few seconds in a workout; some apps write every few minutes).",
            "duration_min is the duration the recording app set (usually without pauses); elapsed_min is start to end. "
            "Pace uses duration_min.",
            "Efficiency = metres per heartbeat (distance / (duration x average heart rate)); higher at the same effort "
            "means fitter. It is only comparable between workouts of the same type and similar conditions.",
        ] + (["The route's point times are not usable (the source wrote one time for every point), so there are no "
              "splits; the route gives distance and shape only."] if has_route and not route_info["point_times_usable"] else [])
          + (["No heart rate was recorded during this workout."] if not during else [])
          + ([f"This session was also recorded by {', '.join(x['source'] or '?' for x in also)}; it is counted once "
              "here and in the comparison."] if also else []),
    }
    series = {"tz": person.tz, "start": start, "end": end, "hr": during, "rows": rows if route else
              [{k: v for k, v in r.items() if k not in ("lat", "lon")} for r in rows],
              "route": route and has_route, "history": history, "eff": eff, "splits": splits, "resting": morning["resting_hr"]}
    return facts, series


def by_ten_minutes(points: list[tuple[dt.datetime, float]], start: dt.datetime) -> list[dict]:
    """Average heart rate per 10-minute block from the start of the workout (blocks without a reading left out)."""
    blocks: dict[int, list[float]] = {}
    for t, v in points:
        blocks.setdefault(max(0, int((t - start).total_seconds() // 600)), []).append(v)
    return [{"from_min": i * 10, "avg": round(mean(v)), "readings": len(v)} for i, v in sorted(blocks.items())]


def km_splits(rows: list[dict], hr: list[tuple[dt.datetime, float]]) -> list[dict]:
    out, mark, t_prev = [], 1000.0, None
    first = parse(rows[0]["time"]) if rows and rows[0].get("time") else None
    for r in rows:
        if r.get("time") is None:
            continue
        t = parse(r["time"])
        t_prev = t_prev or first
        if r["distance_m"] >= mark:
            seg = [v for x, v in hr if t_prev <= x < t]
            secs = (t - t_prev).total_seconds()
            out.append({"km": int(mark // 1000), "time_s": round(secs), "pace_min_per_km": rnd(secs / 60, 2),
                        "avg_hr": rnd(mean(seg))})
            mark += 1000
            t_prev = t
    return out


# --------------------------------------------------------------------------
# Language: every visible string of the PDF and the charts
# --------------------------------------------------------------------------

LABELS_EN = {
    "summary": "Summary", "details": "Details", "evaluation": "Evaluation", "recommendations": "Recommendations",
    "data_notes": "Data notes",
    "disclaimer": "This report describes patterns in Apple Health data. It is not medical advice; see a clinician for "
                  "symptoms or concerns.",
    "footer": "healthkit-archive report - generated {when}", "page": "page {n}",
    "no_data": "no data", "min": "min", "bpm": "bpm",
    "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "weekdays_long": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "months": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    "months_long": ["January", "February", "March", "April", "May", "June", "July", "August", "September",
                    "October", "November", "December"],
    "long_day": "{wdl} {d:02d} {mon} {y}", "long_date": "{wdl} {d:02d} {mon} {y}, {hh:02d}:{mi:02d}",
    "chart_weekday": "{wd} {d:02d}", "chart_date": "{d:02d} {mon}", "hm": "{h}h {m:02d}m",
    "decimal_sep": ".", "thousands_sep": ",", "for_person": "{name}", "in_progress": "(in progress)",
    # day report
    "day_title": "Daily report: {date}",
    "day_subtitle": "{name} - one day of Apple Health data, compared with the previous 28 days ({start} to {end})",
    "d_sleep": "sleep (in bed)", "d_rhr": "resting heart rate", "d_hr_range": "heart rate, day min - max",
    "d_hrv": "HRV (SDNN, readings)", "d_steps": "steps", "d_distance": "walking + running",
    "d_energy": "active / total energy", "d_exercise": "exercise minutes / flights",
    "c_day_hr": "Heart rate through the day", "resting_hr_line": "resting",
    "c_day_hr_caption": "One-minute heart rate; grey: asleep, blue: workouts; dashed: resting heart rate",
    "c_stages": "Last night's sleep by stage (minutes)", "c_stages_caption": "Sleep stages, and time in bed",
    "c_context": "This day against the previous 28 days",
    "c_context_caption": "Steps and sleep of the previous 28 days (grey) and this day (blue)",
    "c_steps": "Steps", "c_sleep_h": "Sleep (hours)",
    "s_deep": "Deep", "s_core": "Core (light)", "s_rem": "REM", "s_awake": "Awake", "s_unspecified": "Asleep, no stage",
    "s_in_bed": "In bed",
    "tb_baseline": "Compared with the previous 28 days", "h_measure": "measure", "h_this_day": "this day",
    "h_avg": "28-day average", "h_range": "28-day range", "h_diff": "difference", "h_days_lower": "days lower",
    "b_sleep_min": "sleep", "b_resting_hr": "resting heart rate", "b_hrv_ms": "HRV (SDNN)", "b_steps": "steps",
    "b_distance_m": "walking + running distance", "b_active_kcal": "active energy", "b_exercise_min": "exercise minutes",
    "b_flights": "flights climbed",
    "tb_night": "Sleep and heart rate", "h_value": "value", "n_window": "sleep window", "n_source": "sleep source",
    "n_pct": "deep / core / REM / awake", "n_hr_asleep": "heart rate asleep / awake (average)",
    "n_hr_minutes": "minutes with a heart-rate reading", "n_spo2_resp": "SpO2 / respiratory rate", "n_weight": "weight",
    "tb_workouts": "Workouts", "h_time": "time", "h_type": "type", "h_minutes": "minutes", "h_distance": "distance",
    "h_avg_hr": "avg HR", "h_max_hr": "max HR", "h_date": "date",
    # period report
    "period_title": "Health report: {label}", "p_week": "week of {start} to {end}", "p_month": "{month} {year}",
    "long_day_short": "{d:02d} {mon} {y}",
    "p_range": "{start} to {end}",
    "period_subtitle": "{name} - {start} to {end}, compared with {pstart} to {pend}",
    "w_sleep": "average sleep", "w_rhr": "average resting HR", "w_hrv": "average HRV (SDNN)", "w_steps": "steps per day",
    "w_distance": "walking + running per day", "w_energy": "active energy per day", "w_exercise": "exercise minutes per day",
    "w_workouts": "workouts / minutes", "vs_prev": "({delta} vs previous)",
    "c_sleep": "Sleep per night (hours, by stage)", "c_sleep_caption": "Sleep and its stages",
    "c_rhr": "Resting heart rate", "c_hrv": "HRV (SDNN)", "c_hearts_caption": "Resting heart rate and HRV",
    "c_steps_day": "Steps per day", "c_energy": "Active energy per day", "c_move_caption": "Steps and active energy",
    "c_workout_min": "Workout minutes per day", "no_workouts": "no workouts recorded in this period",
    "c_training_caption": "Workouts",
    "tb_daily": "Day by day", "h_sleep": "sleep", "h_rhr": "RHR", "h_hrv": "HRV", "h_steps": "steps",
    "h_active": "active kcal", "today_partial": "today, partial",
    # workout report
    "workout_title": "Workout report: {type}, {when}",
    "workout_subtitle": "{name} - {start} to {end} ({offset}), source: {source}",
    "t_duration": "duration (elapsed)", "t_distance": "distance", "t_pace": "average pace", "t_hr": "average / max HR",
    "t_drift": "HR drift, 1st to 2nd half", "t_kcal": "active energy", "t_eff": "metres per heartbeat (8-week median)",
    "t_route": "route",
    "c_w_hr": "Heart rate during the workout", "c_w_hr_caption": "One-minute heart rate; dashed: resting heart rate that day",
    "c_split_pace": "Pace per km (min/km)", "c_split_hr": "Average heart rate per km (bpm)",
    "c_splits_caption": "Splits per kilometre",
    "c_history": "Pace and heart rate of the same workout type, previous 8 weeks",
    "c_history_caption": "Previous workouts of this type (grey) and this one (blue)",
    "u_pace": "pace (min/km)", "u_avg_hr": "average heart rate (bpm)",
    "c_route": "Route shape", "c_route_caption": "Route shape (GPS)",
    "tb_splits": "Splits per kilometre", "h_km": "km", "h_pace": "pace",
    "tb_more": "More measures", "m_hr_readings": "heart-rate readings in the workout",
    "m_hr_10": "heart rate by 10 minutes", "m_recorded_hr": "average / max HR recorded by the app",
    "m_route": "route: points / distance / climb", "m_that_day": "that day: sleep / resting HR / HRV",
    "route_none": "no route", "route_untimed": "shape only (no usable times)", "route_timed": "with times",
    "type.running": "Running", "type.walking": "Walking", "type.cycling": "Cycling", "type.hiking": "Hiking",
    "type.swimming": "Swimming", "type.traditionalStrengthTraining": "Strength training",
    "type.functionalStrengthTraining": "Functional strength", "type.yoga": "Yoga", "type.pickleball": "Pickleball",
    "type.tennis": "Tennis", "type.highIntensityIntervalTraining": "HIIT", "type.elliptical": "Elliptical",
    "type.rowing": "Rowing", "type.mixedCardio": "Mixed cardio", "type.badminton": "Badminton",
}

LABELS_VI = {
    "summary": "Tóm tắt", "details": "Chi tiết", "evaluation": "Đánh giá", "recommendations": "Khuyến nghị",
    "data_notes": "Ghi chú dữ liệu",
    "disclaimer": "Báo cáo này mô tả các xu hướng trong dữ liệu Apple Health. Đây không phải lời khuyên y tế; hãy gặp "
                  "bác sĩ nếu bạn có triệu chứng hoặc lo ngại.",
    "footer": "Báo cáo healthkit-archive - tạo lúc {when}", "page": "trang {n}",
    "no_data": "không có dữ liệu", "min": "phút", "bpm": "nhịp/phút",
    "weekdays": ["T2", "T3", "T4", "T5", "T6", "T7", "CN"],
    "weekdays_long": ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"],
    "months": ["Th1", "Th2", "Th3", "Th4", "Th5", "Th6", "Th7", "Th8", "Th9", "Th10", "Th11", "Th12"],
    "months_long": ["Tháng 1", "Tháng 2", "Tháng 3", "Tháng 4", "Tháng 5", "Tháng 6", "Tháng 7", "Tháng 8",
                    "Tháng 9", "Tháng 10", "Tháng 11", "Tháng 12"],
    "long_day": "{wdl}, {d:02d}/{m:02d}/{y}", "long_date": "{wdl}, {d:02d}/{m:02d}/{y}, {hh:02d}:{mi:02d}",
    "chart_weekday": "{wd} {d:02d}", "chart_date": "{d:02d}/{m:02d}", "hm": "{h}h{m:02d}",
    "decimal_sep": ",", "thousands_sep": ".", "for_person": "{name}", "in_progress": "(đang diễn ra)",
    "day_title": "Báo cáo ngày: {date}",
    "day_subtitle": "{name} - dữ liệu Apple Health của một ngày, so với 28 ngày trước đó ({start} đến {end})",
    "d_sleep": "giấc ngủ (thời gian trên giường)", "d_rhr": "nhịp tim nghỉ", "d_hr_range": "nhịp tim thấp - cao trong ngày",
    "d_hrv": "HRV (SDNN, số lần đo)", "d_steps": "số bước", "d_distance": "đi bộ + chạy",
    "d_energy": "năng lượng vận động / tổng", "d_exercise": "phút tập / số tầng",
    "c_day_hr": "Nhịp tim trong ngày", "resting_hr_line": "nghỉ",
    "c_day_hr_caption": "Nhịp tim theo từng phút; xám: lúc ngủ, xanh: buổi tập; nét đứt: nhịp tim nghỉ",
    "c_stages": "Giấc ngủ đêm qua theo giai đoạn (phút)", "c_stages_caption": "Các giai đoạn ngủ và thời gian trên giường",
    "c_context": "Ngày này so với 28 ngày trước đó",
    "c_context_caption": "Số bước và giấc ngủ của 28 ngày trước (xám) và ngày này (xanh)",
    "c_steps": "Số bước", "c_sleep_h": "Giấc ngủ (giờ)",
    "s_deep": "Ngủ sâu", "s_core": "Ngủ nông", "s_rem": "REM", "s_awake": "Thức giấc", "s_unspecified": "Ngủ, không rõ giai đoạn",
    "s_in_bed": "Trên giường",
    "tb_baseline": "So với 28 ngày trước đó", "h_measure": "chỉ số", "h_this_day": "ngày này",
    "h_avg": "trung bình 28 ngày", "h_range": "khoảng 28 ngày", "h_diff": "chênh lệch", "h_days_lower": "số ngày thấp hơn",
    "b_sleep_min": "giấc ngủ", "b_resting_hr": "nhịp tim nghỉ", "b_hrv_ms": "HRV (SDNN)", "b_steps": "số bước",
    "b_distance_m": "quãng đường đi bộ + chạy", "b_active_kcal": "năng lượng vận động", "b_exercise_min": "phút tập",
    "b_flights": "số tầng đã leo",
    "tb_night": "Giấc ngủ và nhịp tim", "h_value": "giá trị", "n_window": "khung giờ ngủ", "n_source": "nguồn dữ liệu ngủ",
    "n_pct": "sâu / nông / REM / thức", "n_hr_asleep": "nhịp tim khi ngủ / khi thức (trung bình)",
    "n_hr_minutes": "số phút có số đo nhịp tim", "n_spo2_resp": "SpO2 / nhịp thở", "n_weight": "cân nặng",
    "tb_workouts": "Các buổi tập", "h_time": "giờ", "h_type": "loại", "h_minutes": "phút", "h_distance": "quãng đường",
    "h_avg_hr": "NT TB", "h_max_hr": "NT cao nhất", "h_date": "ngày",
    "period_title": "Báo cáo sức khỏe: {label}", "p_week": "tuần {start} - {end}", "p_month": "{month} năm {year}",
    "long_day_short": "{d:02d}/{m:02d}/{y}",
    "p_range": "{start} đến {end}",
    "period_subtitle": "{name} - {start} đến {end}, so với {pstart} đến {pend}",
    "w_sleep": "giấc ngủ trung bình", "w_rhr": "nhịp tim nghỉ trung bình", "w_hrv": "HRV trung bình (SDNN)",
    "w_steps": "số bước mỗi ngày", "w_distance": "đi bộ + chạy mỗi ngày", "w_energy": "năng lượng vận động mỗi ngày",
    "w_exercise": "phút tập mỗi ngày", "w_workouts": "buổi tập / số phút", "vs_prev": "({delta} so với kỳ trước)",
    "c_sleep": "Giấc ngủ mỗi đêm (giờ, theo giai đoạn)", "c_sleep_caption": "Giấc ngủ và các giai đoạn",
    "c_rhr": "Nhịp tim nghỉ", "c_hrv": "HRV (SDNN)", "c_hearts_caption": "Nhịp tim nghỉ và HRV",
    "c_steps_day": "Số bước mỗi ngày", "c_energy": "Năng lượng vận động mỗi ngày", "c_move_caption": "Số bước và năng lượng vận động",
    "c_workout_min": "Số phút tập mỗi ngày", "no_workouts": "không có buổi tập nào trong giai đoạn này",
    "c_training_caption": "Tập luyện",
    "tb_daily": "Từng ngày", "h_sleep": "giấc ngủ", "h_rhr": "NT nghỉ", "h_hrv": "HRV", "h_steps": "số bước",
    "h_active": "kcal vận động", "today_partial": "hôm nay, chưa đủ ngày",
    "workout_title": "Báo cáo buổi tập: {type}, {when}",
    "workout_subtitle": "{name} - {start} đến {end} ({offset}), nguồn: {source}",
    "t_duration": "thời lượng (tổng)", "t_distance": "quãng đường", "t_pace": "tốc độ trung bình", "t_hr": "nhịp tim TB / cao nhất",
    "t_drift": "nhịp tim tăng, nửa đầu sang nửa sau", "t_kcal": "năng lượng vận động",
    "t_eff": "mét mỗi nhịp tim (trung vị 8 tuần)", "t_route": "tuyến đường",
    "c_w_hr": "Nhịp tim trong buổi tập", "c_w_hr_caption": "Nhịp tim theo từng phút; nét đứt: nhịp tim nghỉ ngày hôm đó",
    "c_split_pace": "Tốc độ mỗi km (phút/km)", "c_split_hr": "Nhịp tim trung bình mỗi km (nhịp/phút)",
    "c_splits_caption": "Chia theo từng kilômét",
    "c_history": "Tốc độ và nhịp tim của cùng loại buổi tập, 8 tuần trước",
    "c_history_caption": "Các buổi tập cùng loại trước đó (xám) và buổi này (xanh)",
    "u_pace": "tốc độ (phút/km)", "u_avg_hr": "nhịp tim trung bình (nhịp/phút)",
    "c_route": "Hình dạng tuyến đường", "c_route_caption": "Hình dạng tuyến đường (GPS)",
    "tb_splits": "Chia theo từng kilômét", "h_km": "km", "h_pace": "tốc độ",
    "tb_more": "Các chỉ số khác", "m_hr_readings": "số lần đo nhịp tim trong buổi tập",
    "m_hr_10": "nhịp tim theo từng 10 phút", "m_recorded_hr": "nhịp tim TB / cao nhất do ứng dụng ghi",
    "m_route": "tuyến đường: số điểm / quãng đường / độ cao leo", "m_that_day": "ngày hôm đó: giấc ngủ / nhịp tim nghỉ / HRV",
    "route_none": "không có", "route_untimed": "chỉ có hình dạng (không có thời gian dùng được)", "route_timed": "có thời gian",
    "type.running": "Chạy bộ", "type.walking": "Đi bộ", "type.cycling": "Đạp xe", "type.hiking": "Leo núi",
    "type.swimming": "Bơi", "type.traditionalStrengthTraining": "Tập tạ", "type.functionalStrengthTraining": "Tập sức mạnh",
    "type.yoga": "Yoga", "type.pickleball": "Pickleball", "type.tennis": "Quần vợt",
    "type.highIntensityIntervalTraining": "HIIT", "type.elliptical": "Máy chạy elip", "type.rowing": "Chèo thuyền",
    "type.mixedCardio": "Cardio tổng hợp", "type.badminton": "Cầu lông",
}

BUILT_IN_LABELS = {"en": LABELS_EN, "vi": LABELS_VI}


class Labels:
    """Label lookup for one report: English, then the built-in language, then the narrative's own labels."""

    def __init__(self, language: str = "en", extra: dict | None = None):
        self.language = (language or "en").lower()
        built_in = BUILT_IN_LABELS.get(self.language.split("-")[0], {})
        self.table = {**LABELS_EN, **built_in, **(extra or {})}
        self.fallback = [] if self.language.startswith("en") else sorted(
            k for k in LABELS_EN if k not in built_in and k not in (extra or {}) and not k.startswith("type."))

    def __call__(self, key: str, **kw) -> str:
        text = self.table[key]
        return text.format(**kw) if kw else text

    def value(self, prefix: str, raw) -> str:
        if raw is None:
            return "-"
        return self.table.get(f"{prefix}.{raw}") or str(raw).replace("_", " ").capitalize()

    def date(self, d, template: str = "chart_date") -> str:
        return self.table[template].format(wd=self.table["weekdays"][d.weekday()], wdl=self.table["weekdays_long"][d.weekday()],
                                           d=d.day, m=d.month, mon=self.table["months"][d.month - 1], y=d.year,
                                           hh=getattr(d, "hour", 0), mi=getattr(d, "minute", 0))

    def n(self, value, unit: str = "") -> str:
        """A number with the language's separators ('-' when missing)."""
        if value is None:
            return "-"
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{value}{unit}"
        text = f"{value:,}" if isinstance(value, int) or not float(value).is_integer() else f"{int(value):,}"
        dec, thou = self.table.get("decimal_sep", "."), self.table.get("thousands_sep", ",")
        return text.replace(",", "\0").replace(".", dec).replace("\0", thou) + unit

    def whole(self, value, unit: str = "") -> str:
        return self.n(None if value is None else int(round(value)), unit)

    def km(self, metres) -> str:
        return "-" if metres is None else self.n(round(metres / 1000, 2), " km")

    def hm(self, minutes) -> str:
        if minutes is None:
            return "-"
        h, m = divmod(int(round(minutes)), 60)
        return self.table["hm"].format(h=h, m=m)

    def all_text(self) -> str:
        return " ".join(" ".join(v) if isinstance(v, list) else str(v) for v in self.table.values())


# --------------------------------------------------------------------------
# Fonts
# --------------------------------------------------------------------------


def font_files(regular: str | None, bold: str | None) -> tuple[Path, Path]:
    regular = regular or os.environ.get("HKA_REPORT_FONT")
    bold = bold or os.environ.get("HKA_REPORT_FONT_BOLD")
    if not regular:
        try:
            import matplotlib
        except ImportError:
            fail(DEPS_HINT)
        ttf = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
        regular, bold = str(ttf / "DejaVuSans.ttf"), bold or str(ttf / "DejaVuSans-Bold.ttf")
    regular_path, bold_path = Path(regular).expanduser(), Path(bold or regular).expanduser()
    for p in (regular_path, bold_path):
        if not p.is_file():
            fail(f"font file not found: {p}")
        if p.suffix.lower() not in (".ttf", ".ttc"):
            fail(f"{p} is not a TrueType font (.ttf); reportlab cannot embed OpenType/CFF fonts")
    return regular_path, bold_path


def check_font_covers(font: Path, text: str) -> None:
    try:
        from reportlab.pdfbase.ttfonts import TTFont
    except ImportError:
        fail(DEPS_HINT)
    covered = set(TTFont("probe", str(font)).face.charToGlyph)
    missing = sorted({c for c in text if ord(c) > 32 and ord(c) not in covered})
    if missing:
        fail(f"the font {font.name} has no glyphs for {''.join(missing[:40])!r}{' ...' if len(missing) > 40 else ''}; "
             "pass a TrueType font that covers the language with --font FILE.ttf (and --font-bold), or set HKA_REPORT_FONT")


# --------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------


def _plt(fonts: tuple[Path, Path]):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
    except ImportError:
        fail(DEPS_HINT)
    for f in fonts:
        font_manager.fontManager.addfont(str(f))
    plt.rcParams.update({
        "font.family": font_manager.FontProperties(fname=str(fonts[0])).get_name(), "font.size": 8,
        "axes.edgecolor": GRID, "axes.labelcolor": INK2, "axes.titlesize": 9, "axes.titleweight": "bold",
        "axes.titlecolor": INK, "axes.titlelocation": "left", "xtick.color": INK2, "ytick.color": INK2,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "axes.spines.top": False,
        "axes.spines.right": False, "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "lines.linewidth": 1.4,
        "savefig.dpi": 200,
    })
    return plt


def _save(fig, path: Path) -> Path:
    import matplotlib.pyplot as plt
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    return path


def _title(ax, text):
    import textwrap
    width = ax.figure.get_size_inches()[0] * ax.get_position().width
    ax.set_title(textwrap.fill(text, width=max(24, int(width * 11))))


def _empty(ax, L):
    ax.text(0.5, 0.5, L("no_data"), ha="center", va="center", color=MUTED, transform=ax.transAxes)


def _date_axis(ax, L, template, maxticks=None):
    import matplotlib.dates as mdates
    from matplotlib.ticker import FuncFormatter
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=maxticks) if maxticks else mdates.DayLocator())
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: L.date(mdates.num2date(v), template)))
    ax.tick_params(axis="x", labelsize=7)


def gapped(points, max_gap, local):
    """x/y with a break wherever readings are missing, so a line never bridges a gap."""
    xs, ys = [], []
    for i, (t, v) in enumerate(points):
        if i and t - points[i - 1][0] > max_gap:
            xs.append(local(t))
            ys.append(float("nan"))
        xs.append(local(t))
        ys.append(v)
    return xs, ys


def stage_bars(ax, xs, records, L, width, per_hour=True):
    bottom = [0.0] * len(records)
    scale = 60 if per_hour else 1
    for (key, label), color in zip((("deep_min", L("s_deep")), ("core_min", L("s_core")), ("rem_min", L("s_rem")),
                                    ("awake_min", L("s_awake")), ("unspecified_min", L("s_unspecified"))),
                                   SERIES + [MUTED]):
        vals = [(r.get(key) or 0) / scale for r in records]
        if any(vals):
            ax.bar(xs, vals, bottom=bottom, color=color, width=width, label=label, edgecolor=SURFACE, linewidth=1.0)
            bottom = [b + v for b, v in zip(bottom, vals)]
    return bottom


def day_charts(series: dict, workdir: Path, L: Labels, fonts) -> list[tuple[str, Path]]:
    plt = _plt(fonts)
    import matplotlib.dates as mdates
    tz, day = series["tz"], series["day"]
    local = lambda t: t.astimezone(tz).replace(tzinfo=None)
    d0 = dt.datetime.combine(day, dt.time())
    d1 = d0 + dt.timedelta(days=1)
    charts = []

    fig, ax = plt.subplots(figsize=(7.2, 2.4))
    if series["sleep"]:
        ax.axvspan(max(local(parse(series["sleep"][0])), d0), min(local(parse(series["sleep"][1])), d1),
                   color=MUTED, alpha=0.13, lw=0)
    for w in series["workouts"]:
        ax.axvspan(local(parse(w["start"])), local(parse(w["end"])), color=SERIES[0], alpha=0.15, lw=0)
    if series["hr"]:
        ax.plot(*gapped(series["hr"], dt.timedelta(minutes=5), local), color=SERIES[0], lw=1.0)
        ax.plot([local(t) for t, _ in series["hr"]], [v for _, v in series["hr"]], ".", color=SERIES[0], markersize=1.2)
    else:
        _empty(ax, L)
    if series["resting"]:
        ax.axhline(series["resting"], color=INK2, lw=0.9, ls="--")
        ax.text(1.005, series["resting"], L("resting_hr_line"), transform=ax.get_yaxis_transform(), va="center",
                fontsize=7, color=INK2)
    ax.set_xlim(d0, d1)
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=3))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    _title(ax, f"{L('c_day_hr')} ({L('bpm')})")
    charts.append((L("c_day_hr_caption"), _save(fig, workdir / "day_hr.png")))

    stages = [(L("s_deep"), series["stages"]["deep"]), (L("s_core"), series["stages"]["core"]),
              (L("s_rem"), series["stages"]["rem"]), (L("s_awake"), series["stages"]["awake"]),
              (L("s_unspecified"), series["unspecified"])]
    if any(v for _, v in stages):
        fig, ax = plt.subplots(figsize=(7.2, 1.7))
        left = 0
        for (name, v), color in zip(stages, SERIES + [MUTED]):
            if not v:
                continue
            ax.barh([0], [v], left=left, color=color, edgecolor=SURFACE, linewidth=2, height=0.5, label=name)
            if v >= 25:
                ax.text(left + v / 2, 0, L.whole(v), ha="center", va="center", fontsize=7.5, color=SURFACE)
            left += v
        if series["in_bed"]:
            ax.plot([series["in_bed"]] * 2, [-0.38, 0.38], color=INK, lw=1.5)
            ax.text(series["in_bed"], 0.42, f"{L('s_in_bed')} {L.hm(series['in_bed'])}", fontsize=7, color=INK,
                    ha="center", va="bottom")
        ax.set_ylim(-0.6, 0.75)
        ax.set_yticks([])
        ax.set_xlim(0, max(left, series["in_bed"] or 0) * 1.08)
        ax.legend(frameon=False, fontsize=7, ncol=5, loc="upper left", bbox_to_anchor=(0, -0.42))
        ax.grid(axis="y", visible=False)
        _title(ax, L("c_stages"))
        charts.append((L("c_stages_caption"), _save(fig, workdir / "day_sleep.png")))

    hist = series["history"]
    xs = [dt.datetime.combine(dt.date.fromisoformat(r["date"]), dt.time()) for r in hist]
    colors = [MUTED] * (len(hist) - 1) + [SERIES[0]]
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.0))
    for ax, key, title, scale in ((axes[0], "steps", L("c_steps"), 1), (axes[1], "sleep_min", L("c_sleep_h"), 60)):
        vals = [(r.get(key) or 0) / scale for r in hist]
        if any(vals):
            ax.bar(xs, vals, color=colors, width=0.75)
        else:
            _empty(ax, L)
        _date_axis(ax, L, "chart_date", maxticks=4)
        _title(ax, title)
    charts.append((L("c_context_caption"), _save(fig, workdir / "day_context.png")))
    return charts


def period_charts(series: dict, workdir: Path, L: Labels, fonts) -> list[tuple[str, Path]]:
    plt = _plt(fonts)
    days = series["days"]
    xs = [dt.datetime.combine(dt.date.fromisoformat(r["date"]), dt.time()) for r in days]
    many = len(days) > 10
    period = (dt.datetime.combine(series["from"], dt.time()) - dt.timedelta(days=0.6),
              dt.datetime.combine(series["to"], dt.time()) + dt.timedelta(days=0.6))

    def fmt(ax):
        ax.set_xlim(*period)
        if many:
            _date_axis(ax, L, "chart_date", maxticks=8 if ax.get_position().width > 0.6 else 4)
        else:
            _date_axis(ax, L, "chart_weekday")

    def points(ax, key, title):
        pts = [(x, r[key]) for x, r in zip(xs, days) if r.get(key) is not None]
        if pts:
            ax.plot([p[0] for p in pts], [p[1] for p in pts], "-o", color=SERIES[0], markersize=3)
        else:
            _empty(ax, L)
        _title(ax, title)

    charts = []
    width = 0.7
    fig, ax = plt.subplots(figsize=(7.2, 2.4))
    stage_bars(ax, xs, days, L, width)
    if not any(r.get("sleep_min") for r in days):
        _empty(ax, L)
    _title(ax, L("c_sleep"))
    ax.legend(frameon=False, fontsize=7, ncol=5, loc="upper left", bbox_to_anchor=(0, -0.18))
    fmt(ax)
    charts.append((L("c_sleep_caption"), _save(fig, workdir / "sleep.png")))

    panels = [(k, title) for k, title in (("resting_hr", f"{L('c_rhr')} ({L('bpm')})"), ("hrv_ms", f"{L('c_hrv')} (ms)"))
              if any(r.get(k) is not None for r in days)]
    if panels:
        fig, axes = plt.subplots(1, len(panels), figsize=(7.2, 2.1), squeeze=False)
        for ax, (key, title) in zip(axes[0], panels):
            points(ax, key, title)
            fmt(ax)
        charts.append((L("c_rhr") if len(panels) == 1 and panels[0][0] == "resting_hr" else L("c_hearts_caption"),
                       _save(fig, workdir / "hearts.png")))

    fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.1))
    for ax, key, title in ((axes[0], "steps", L("c_steps_day")), (axes[1], "active_kcal", f"{L('c_energy')} (kcal)")):
        vals = [r.get(key) or 0 for r in days]
        if any(vals):
            ax.bar(xs, vals, color=SERIES[0], width=width, edgecolor=SURFACE, linewidth=1.0)
        else:
            _empty(ax, L)
        _title(ax, title)
        fmt(ax)
    charts.append((L("c_move_caption"), _save(fig, workdir / "move.png")))

    per_day = {r["date"]: 0.0 for r in days}
    for w in series["workouts"]:
        key = w["start"][:10]
        if key in per_day:
            per_day[key] += w.get("minutes") or 0
    if any(per_day.values()):                      # no workouts: the tiles and a data note say so instead
        fig, ax = plt.subplots(figsize=(7.2, 1.8))
        ax.bar(xs, list(per_day.values()), color=SERIES[0], width=width, edgecolor=SURFACE, linewidth=1.0)
        _title(ax, L("c_workout_min"))
        fmt(ax)
        charts.append((L("c_training_caption"), _save(fig, workdir / "training.png")))
    return charts


def workout_charts(series: dict, workdir: Path, L: Labels, fonts) -> list[tuple[str, Path]]:
    plt = _plt(fonts)
    start = series["start"]
    charts = []
    fig, ax = plt.subplots(figsize=(7.2, 2.3))
    if series["hr"]:
        mins = [((t - start).total_seconds() / 60, v) for t, v in series["hr"]]
        xs, ys = [], []
        for i, (m, v) in enumerate(mins):
            if i and m - mins[i - 1][0] > 3.5:
                xs.append(m)
                ys.append(float("nan"))
            xs.append(m)
            ys.append(v)
        ax.plot(xs, ys, color=SERIES[0], lw=1.2)
        ax.plot([m for m, _ in mins], [v for _, v in mins], "o", color=SERIES[0], markersize=2.5)
    else:
        _empty(ax, L)
    if series["resting"]:
        ax.axhline(series["resting"], color=INK2, lw=0.9, ls="--")
    ax.set_xlim(0, (series["end"] - start).total_seconds() / 60)
    ax.set_xlabel(L("min"))
    _title(ax, f"{L('c_w_hr')} ({L('bpm')})")
    charts.append((L("c_w_hr_caption"), _save(fig, workdir / "w_hr.png")))

    if series["splits"]:
        fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.1))
        km = [s["km"] for s in series["splits"]]
        axes[0].bar(km, [s["pace_min_per_km"] for s in series["splits"]], color=SERIES[0], width=0.7)
        axes[0].invert_yaxis()
        _title(axes[0], L("c_split_pace"))
        hrs = [s["avg_hr"] or 0 for s in series["splits"]]
        axes[1].bar(km, hrs, color=SERIES[1], width=0.7)
        if any(hrs):
            axes[1].set_ylim(min(h for h in hrs if h) - 10, max(hrs) + 5)
        _title(axes[1], L("c_split_hr"))
        charts.append((L("c_splits_caption"), _save(fig, workdir / "w_splits.png")))

    past = [h for h in series["history"] if h.get("pace_min_per_km") and h.get("avg_hr")]
    if past:
        fig, ax = plt.subplots(figsize=(7.2, 2.2))
        ax.scatter([h["pace_min_per_km"] for h in past], [h["avg_hr"] for h in past], color=MUTED, s=18)
        this = series.get("this_point")
        if this and all(this):
            ax.scatter([this[0]], [this[1]], color=SERIES[0], s=40, zorder=3)
        ax.invert_xaxis()
        ax.set_xlabel(L("u_pace"))
        ax.set_ylabel(L("u_avg_hr"))
        _title(ax, L("c_history"))
        charts.append((L("c_history_caption"), _save(fig, workdir / "w_history.png")))

    if series["route"] and series["rows"] and "lat" in series["rows"][0]:
        fig, ax = plt.subplots(figsize=(3.4, 3.4))
        ax.plot([r["lon"] for r in series["rows"]], [r["lat"] for r in series["rows"]], color=SERIES[0], lw=1.6)
        ax.set_aspect("equal", adjustable="datalim")
        ax.set_xticks([])
        ax.set_yticks([])
        ax.grid(False)
        _title(ax, L("c_route"))
        charts.append((L("c_route_caption"), _save(fig, workdir / "w_route.png")))
    return charts


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------


def load_narrative(path: str) -> dict:
    try:
        n = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        fail(f"cannot read the narrative {path}: {exc}")
    missing = [k for k in ("headline", "summary", "evaluation", "recommendations") if not n.get(k)]
    if missing:
        fail(f"the narrative lacks {missing}; write it from the facts first (see the module docstring)")
    for k in ("evaluation", "recommendations") + (("data_notes",) if "data_notes" in n else ()):
        if not isinstance(n[k], list):
            fail(f"narrative.{k} must be a list of strings")
    if "labels" in n and not isinstance(n["labels"], dict):
        fail("narrative.labels must be an object of label key -> text (see: hka_report.py labels)")
    return n


def build_pdf(out: Path, title: str, subtitle: str, tiles: list[tuple[str, str]], narrative: dict,
              charts: list[tuple[str, Path]], tables: list[tuple[str, list[list]]], notes: list[str],
              L: Labels, fonts: tuple[Path, Path]) -> None:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import (CondPageBreak, Image, KeepTogether, ListFlowable, ListItem, Paragraph, SimpleDocTemplate,
                                        Spacer, Table, TableStyle)
    except ImportError:
        fail(DEPS_HINT)
    from xml.sax.saxutils import escape

    pdfmetrics.registerFont(TTFont("Body", str(fonts[0])))
    pdfmetrics.registerFont(TTFont("Body-Bold", str(fonts[1])))
    pdfmetrics.registerFontFamily("Body", normal="Body", bold="Body-Bold", italic="Body", boldItalic="Body-Bold")
    ss = getSampleStyleSheet()
    base = ParagraphStyle("base", parent=ss["Normal"], fontName="Body", fontSize=9.5, leading=13.5, textColor=colors.HexColor(INK))
    h1 = ParagraphStyle("h1", parent=base, fontName="Body-Bold", fontSize=18, leading=23, spaceAfter=2)
    sub = ParagraphStyle("sub", parent=base, fontSize=9, textColor=colors.HexColor(INK2), spaceAfter=10)
    h2 = ParagraphStyle("h2", parent=base, fontName="Body-Bold", fontSize=12, leading=16, spaceBefore=12, spaceAfter=5)
    lead = ParagraphStyle("lead", parent=base, fontName="Body-Bold", fontSize=10.5, leading=14.5, spaceAfter=4)
    cap = ParagraphStyle("cap", parent=base, fontSize=8, textColor=colors.HexColor(INK2), spaceAfter=8)
    small = ParagraphStyle("small", parent=base, fontSize=7.5, leading=10, textColor=colors.HexColor(INK2))
    small_b = ParagraphStyle("small_b", parent=small, fontName="Body-Bold", textColor=colors.HexColor(INK))
    tile_v = ParagraphStyle("tv", parent=base, fontName="Body-Bold", fontSize=13, leading=16)
    tile_l = ParagraphStyle("tl", parent=base, fontSize=7.5, leading=9.5, textColor=colors.HexColor(INK2))
    p = lambda text, style=base: Paragraph(escape(str(text)), style)

    story = [p(title, h1), p(subtitle, sub)]
    cells = [[[p(v, tile_v), p(label, tile_l)] for label, v in tiles[i:i + 4]] for i in range(0, len(tiles), 4)]
    for row in cells:
        row += [""] * (4 - len(row))
    tt = Table(cells, colWidths=[43 * mm] * 4)
    tt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f4f3f0")),
                            ("LINEBELOW", (0, 0), (-1, -1), 3, colors.white), ("LINEAFTER", (0, 0), (-1, -1), 3, colors.white),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 6),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("LEFTPADDING", (0, 0), (-1, -1), 7)]))
    story += [tt, Spacer(1, 6), p(L("summary"), h2), p(narrative["headline"], lead), p(narrative["summary"]),
              p(L("details"), h2)]
    for caption, path in charts:
        w, h = ImageReader(str(path)).getSize()
        width = 174 * mm if w / h > 1.5 else 90 * mm
        story.append(KeepTogether([Image(str(path), width=width, height=width * h / w), p(caption, cap)]))
    for caption, rows in tables:
        if len(rows) < 2:
            continue
        data = [[p(c, small_b) for c in rows[0]]] + [[p(c, small) for c in r] for r in rows[1:]]
        t = Table(data, repeatRows=1, hAlign="LEFT")
        t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor(INK2)),
                               ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.HexColor(GRID)), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        # Long tables split across pages; the heading moves to the next page only when little room is left.
        story += [CondPageBreak(40 * mm), p(caption, h2), t]
    bullet = lambda items: ListFlowable([ListItem(p(i), leftIndent=10, value="•") for i in items], bulletType="bullet",
                                        start="•", leftIndent=12, bulletFontSize=8, bulletFontName="Body")
    story += [p(L("evaluation"), h2), bullet(narrative["evaluation"]), p(L("recommendations"), h2),
              bullet(narrative["recommendations"]), p(L("data_notes"), h2)] + [p(n, small) for n in notes + [L("disclaimer")]]

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Body", 7)
        canvas.setFillColor(colors.HexColor(MUTED))
        canvas.drawString(18 * mm, 10 * mm, L("footer", when=dt.datetime.now().strftime("%Y-%m-%d %H:%M")))
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, L("page", n=doc.page))
        canvas.restoreState()

    out.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(out), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=16 * mm, title=title, author="healthkit-archive", subject=subtitle, lang=L.language)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    os.chmod(out, 0o600)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def prepare(narrative: dict, font: str | None, font_bold: str | None) -> tuple[Labels, tuple[Path, Path], list[str]]:
    L = Labels(narrative.get("language", "en"), narrative.get("labels"))
    if L.fallback:
        print(f"hka_report: no built-in labels for language {L.language!r}; {len(L.fallback)} label(s) stay in English "
              f"(e.g. {', '.join(L.fallback[:5])}). Translate them into narrative.labels (see: hka_report.py labels).",
              file=sys.stderr)
    fonts = font_files(font, font_bold)
    text = " ".join([narrative["headline"], narrative["summary"], *narrative["evaluation"], *narrative["recommendations"],
                     *narrative.get("data_notes", []), L.all_text()])
    for f in set(fonts):
        check_font_covers(f, text)
    return L, fonts, narrative.get("data_notes") or []


def render_day(facts: dict, series: dict, narrative: dict, out: Path, font=None, font_bold=None) -> None:
    import tempfile
    L, fonts, notes = prepare(narrative, font, font_bold)
    s, hr, act = facts["sleep"], facts["heart_rate"], facts["activity"]
    cmp = facts[f"compared_with_previous_{BASELINE_DAYS}_days"]
    day = dt.date.fromisoformat(facts["date"])
    with tempfile.TemporaryDirectory() as tmp:
        charts = day_charts(series, Path(tmp), L, fonts)
        tiles = [(L("d_sleep"), f"{L.hm(s['asleep_min'])} ({L.hm(s['in_bed_min'])})"),
                 (L("d_rhr"), L.n(hr["resting"], " " + L("bpm"))),
                 (L("d_hr_range"), f"{L.n(hr['day_min'])} - {L.n(hr['day_max'])}"),
                 (L("d_hrv"), f"{L.n(facts['hrv_sdnn_ms']['avg'], ' ms')} ({facts['hrv_sdnn_ms']['readings']})"
                  if facts["hrv_sdnn_ms"]["readings"] else "-"),
                 (L("d_steps"), L.whole(act["steps"])), (L("d_distance"), L.km(act["distance_m"])),
                 (L("d_energy"), f"{L.whole(act['active_kcal'])} / {L.whole(act['total_kcal'])} kcal"),
                 (L("d_exercise"), f"{L.whole(act['exercise_min'])} / {L.whole(act['flights'])}")]
        fmt = {"sleep_min": L.hm, "resting_hr": lambda v: L.n(rnd(v), " " + L("bpm")), "hrv_ms": lambda v: L.n(rnd(v), " ms"),
               "steps": L.whole, "distance_m": L.km, "active_kcal": lambda v: L.whole(v, " kcal"),
               "exercise_min": lambda v: L.whole(v, " " + L("min")), "flights": L.whole}
        signed = lambda v, f: "-" if v is None else ("+" if v > 0 else "-" if v < 0 else "") + f(abs(v))
        baseline = [[L("h_measure"), L("h_this_day"), L("h_avg"), L("h_range"), L("h_diff"), L("h_days_lower")]] + [
            [L(f"b_{k}"), f(c["this"]), f(c["avg"]), f"{f(c['min'])} - {f(c['max'])}" if c["min"] is not None else "-",
             signed(c["diff"], f), f"{c['days_lower']} / {c['days_with_data']}" if c["days_lower"] is not None else "-"]
            for k, f in fmt.items() for c in [cmp[k]]]
        pct = s["stages_pct"]
        night = [[L("h_measure"), L("h_value")],
                 [L("n_window"), f"{s['start'][11:16]} - {s['end'][11:16]}" if s.get("start") else "-"],
                 [L("n_source"), s.get("source") or "-"],
                 [L("n_pct"), " / ".join(f"{L.n(pct[k])}%" for k in ("deep", "core", "rem", "awake"))],
                 [L("n_hr_asleep"), f"{L.n(hr['asleep_avg'])} / {L.n(hr['awake_avg'])} {L('bpm')}"],
                 [L("n_hr_minutes"), L.n(hr["minutes_with_reading"])],
                 [L("n_spo2_resp"), f"{L.n(facts['spo2_pct']['avg'], ' %')} / {L.n(facts['respiratory_rate']['avg'])}"],
                 [L("n_weight"), L.n(facts["weight_kg"], " kg")]]
        workouts = [[L("h_time"), L("h_type"), L("h_minutes"), L("h_distance"), L("h_avg_hr"), L("h_max_hr")]] + [
            [w["start"][11:16], L.value("type", w["type"]), L.n(rnd(w["minutes"])), L.km(w["distance_m"]),
             L.n(w["heart_rate"]["avg"]), L.n(w["heart_rate"]["max"])] for w in facts["workouts"]]
        build_pdf(out, L("day_title", date=L.date(day, "long_day")) + (f" {L('in_progress')}" if facts["partial_day"] else ""),
                  L("day_subtitle", name=facts["person"]["name"], start=(day - dt.timedelta(days=BASELINE_DAYS)).isoformat(),
                    end=(day - dt.timedelta(days=1)).isoformat()),
                  tiles, narrative, charts, [(L("tb_baseline"), baseline), (L("tb_night"), night), (L("tb_workouts"), workouts)],
                  notes or facts["data_notes"], L, fonts)


def period_label(facts: dict, L: Labels) -> str:
    kind, val = facts["period"]["kind"], facts["period"]["value"]
    if kind == "week":
        start, end = dt.date.fromisoformat(facts["from"]), dt.date.fromisoformat(facts["to"])
        return L("p_week", start=L.date(start, "chart_date"), end=L.date(end, "long_day_short"))
    if kind == "month":
        year, month = (int(x) for x in val.split("-"))
        return L("p_month", month=L.table["months_long"][month - 1], year=year)
    return L("p_range", start=facts["from"], end=facts["to"])


def render_period(facts: dict, series: dict, narrative: dict, out: Path, font=None, font_bold=None) -> None:
    import tempfile
    L, fonts, notes = prepare(narrative, font, font_bold)
    s, d = facts["summary"], facts["change_vs_previous_avg"]
    vs = lambda v, f: "" if v is None else " " + L("vs_prev", delta=("+" if v >= 0 else "-") + f(abs(v)))
    with tempfile.TemporaryDirectory() as tmp:
        charts = period_charts(series, Path(tmp), L, fonts)
        tiles = [(L("w_sleep") + vs(d["sleep_min"], lambda v: L.whole(v, " " + L("min"))), L.hm(s["sleep_min"]["avg"])),
                 (L("w_rhr") + vs(d["resting_hr"], lambda v: L.n(v)), L.n(s["resting_hr"]["avg"], " " + L("bpm"))),
                 (L("w_hrv") + vs(d["hrv_ms"], lambda v: L.n(v, " ms")), L.n(s["hrv_ms"]["avg"], " ms")),
                 (L("w_steps") + vs(d["steps"], L.whole), L.whole(s["steps"]["avg"])),
                 (L("w_distance") + vs(d["distance_m"], L.km), L.km(s["distance_m"]["avg"])),
                 (L("w_energy") + vs(d["active_kcal"], L.whole), L.whole(s["active_kcal"]["avg"], " kcal")),
                 (L("w_exercise") + vs(d["exercise_min"], L.whole), L.whole(s["exercise_min"]["avg"])),
                 (L("w_workouts"), f"{s['workouts']['count']} / {L.whole(s['workouts']['minutes'])}")]
        today = facts["coverage"].get("today_partial")
        daily = [[L("h_date"), L("h_sleep"), L("h_rhr"), L("h_hrv"), L("h_steps"), L("h_distance"), L("h_active")]] + [
            [L.date(dt.date.fromisoformat(x["date"]), "chart_weekday" if len(facts["days"]) <= 10 else "chart_date")
             + (f" ({L('today_partial')})" if x["date"] == today else ""),
             L.hm(x["sleep_min"]), L.n(x["resting_hr"]), L.n(x["hrv_ms"]), L.whole(x["steps"]), L.km(x["distance_m"]),
             L.whole(x["active_kcal"])] for x in facts["days"]]
        workouts = [[L("h_date"), L("h_type"), L("h_minutes"), L("h_distance"), L("h_avg_hr"), L("h_max_hr")]] + [
            [w["start"][:16].replace("T", " "), L.value("type", w["type"]), L.n(rnd(w["minutes"])), L.km(w["distance_m"]),
             L.n(w["heart_rate"]["avg"]), L.n(w["heart_rate"]["max"])] for w in facts["workouts"]]
        build_pdf(out, L("period_title", label=period_label(facts, L)) + (f" {L('in_progress')}" if facts["in_progress"] else ""),
                  L("period_subtitle", name=facts["person"]["name"], start=facts["from"], end=facts["to"],
                    pstart=facts["previous_period"]["from"], pend=facts["previous_period"]["to"]),
                  tiles, narrative, charts, [(L("tb_daily"), daily), (L("tb_workouts"), workouts)],
                  notes or facts["data_notes"], L, fonts)


def render_workout(facts: dict, series: dict, narrative: dict, out: Path, font=None, font_bold=None) -> None:
    import tempfile
    L, fonts, notes = prepare(narrative, font, font_bold)
    s, h, r, e = facts["summary"], facts["heart_rate_from_minute_series"], facts["route"], facts["efficiency_m_per_beat"]
    series["this_point"] = (s["pace_min_per_km"], h["avg"])
    route = L("route_none") if not r["present"] else L("route_timed") if r["point_times_usable"] else L("route_untimed")
    start = parse(facts["start"])
    with tempfile.TemporaryDirectory() as tmp:
        charts = workout_charts(series, Path(tmp), L, fonts)
        secs = lambda m: None if m is None else m * 60
        tiles = [(L("t_duration"), f"{dur_text(secs(s['duration_min']))} ({dur_text(secs(s['elapsed_min']))})"),
                 (L("t_distance"), L.km(s["distance_m"])), (L("t_pace"), s["pace"] or "-"),
                 (f"{L('t_hr')} ({L('bpm')})", f"{L.n(h['avg'])} / {L.n(h['max'])}"),
                 (f"{L('t_drift')} ({L('bpm')})", "-" if h["drift"] is None else ("+" if h["drift"] >= 0 else "") + L.n(h["drift"])),
                 (L("t_kcal"), L.whole(s["active_kcal"], " kcal")),
                 (L("t_eff"), f"{L.n(e['this'])} ({L.n(e['previous_median'])})"), (L("t_route"), route)]
        splits = [[L("h_km"), L("h_time"), L("h_pace"), L("h_avg_hr")]] + [
            [x["km"], dur_text(x["time_s"]), pace_text(x["pace_min_per_km"]), L.n(x["avg_hr"])] for x in facts["splits_km"]]
        day = facts["that_day"]
        more = [[L("h_measure"), L("h_value")],
                [L("m_hr_readings"), L.n(h["readings"])],
                [L("m_hr_10"), ", ".join(f"{x['from_min']}': {L.n(x['avg'])}" for x in facts["heart_rate_by_10_min"]) or "-"],
                [L("m_recorded_hr"), f"{L.n(s['avg_hr_recorded'])} / {L.n(s['max_hr_recorded'])}"],
                [L("m_route"), f"{L.n(r.get('points'))} / {L.km(r.get('distance_m'))} / {L.whole(r.get('elevation_gain_m'), ' m')}"
                 if r["present"] else "-"],
                [L("m_that_day"), f"{L.hm(day['sleep_min'])} / {L.n(day['resting_hr'])} / {L.n(day['hrv_ms'], ' ms')}"]]
        build_pdf(out, L("workout_title", type=L.value("type", facts["type"]), when=L.date(start, "long_date")),
                  L("workout_subtitle", name=facts["person"]["name"], start=facts["start"][:16].replace("T", " "),
                    end=facts["end"][11:16], offset=facts["start"][-6:], source=facts.get("source") or "-"),
                  tiles, narrative, charts, [(L("tb_splits"), splits), (L("tb_more"), more)],
                  notes or facts["data_notes"], L, fonts)


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="hka_report.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="kind", required=True)
    dy = sub.add_parser("day", help="one day against the previous 28 days")
    dy.add_argument("--date", required=True, metavar="YYYY-MM-DD")
    pr = sub.add_parser("period", help="a week, a month or a range, against the period before it")
    pr.add_argument("--week")
    pr.add_argument("--month")
    pr.add_argument("--from", dest="start")
    pr.add_argument("--to", dest="end")
    wk = sub.add_parser("workout", help="one workout")
    wk.add_argument("workout", help="the workout's HealthKit UUID (python3 tools/medplum_agent.py workouts)")
    wk.add_argument("--route", action="store_true", help="include the GPS route shape")
    for s in (dy, pr, wk):
        g = s.add_mutually_exclusive_group(required=True)
        g.add_argument("--facts", metavar="FILE", help="write the computed facts as JSON (step 1)")
        g.add_argument("--narrative", metavar="FILE", help="render the PDF with this narrative (step 2)")
        s.add_argument("--out", help="PDF path (default: reports/...)")
        s.add_argument("--font", help="TrueType font for the report language (default: DejaVu Sans from matplotlib)")
        s.add_argument("--font-bold", help="bold TrueType font (default: the bold DejaVu Sans, or --font)")
    for s in (dy, pr):
        s.add_argument("--person", help="Patient id, user id or name (see medplum_agent.py people)")
    lb = sub.add_parser("labels", help="print every label as JSON, to translate into narrative.labels")
    lb.add_argument("--lang", default="en", help="start from this built-in language (en, vi)")
    args = parser.parse_args(argv)

    if args.kind == "labels":
        print(json.dumps(Labels(args.lang).table, indent=1, ensure_ascii=False))
        return 0
    if args.kind == "period" and args.start and args.end and args.start == args.end and not (args.week or args.month):
        args.kind, args.date = "day", args.start            # a one-day range is a daily report
    if args.kind == "day":
        try:
            day = dt.date.fromisoformat(args.date)
        except ValueError:
            fail(f"--date {args.date!r} is not YYYY-MM-DD")
        if day > dt.date.today():
            fail("--date is in the future")
        person = Person(args.person)
        facts, series = day_facts(person, day)
        default = REPORT_DIR / f"{person.user}-day-{day.isoformat()}.pdf"
    elif args.kind == "period":
        start, end, period = period_from_args(args)
        if start > dt.date.today():
            fail("the period has not started yet")
        person = Person(args.person)
        facts, series = period_facts(person, start, end, period)
        default = REPORT_DIR / f"{person.user}-period-{args.week or args.month or f'{start}_{end}'}.pdf"
    else:
        facts, series = workout_facts(args.workout, route=args.route)
        default = REPORT_DIR / f"{facts['person']['user']}-workout-{args.workout}.pdf"

    if args.facts:
        Path(args.facts).write_text(json.dumps(facts, indent=1, ensure_ascii=False, default=str) + "\n")
        print(f"facts written to {args.facts}")
        return 0
    narrative = load_narrative(args.narrative)
    out = Path(args.out) if args.out else default
    {"day": render_day, "period": render_period, "workout": render_workout}[args.kind](
        facts, series, narrative, out, args.font, args.font_bold)
    print(f"report written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
