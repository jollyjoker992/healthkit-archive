#!/usr/bin/env python3
"""Write fhir/examples/: one resource per mapping pattern, produced by the real mapper from made-up records.

    python3 tools/fhir_examples.py           write the examples
    python3 tools/fhir_examples.py --check   exit 1 if the committed examples differ (CI)

The records below go through the same day-file grouping, summary and Mapper as `hka fhir map`, so the
examples change when the mapping does. Conditional references (`Patient?identifier=…`) are replaced by
literal ones (`Patient/example`) to keep the files readable; the loader resolves the real ones.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "cli"))

from hka import days as D  # noqa: E402
from hka import fhir as F  # noqa: E402

OUT = REPO / "fhir" / "examples"
USER, DAY, TZ = "u_example", "2026-11-12", "Asia/Ho_Chi_Minh"
WATCH = {"bundle_id": "com.apple.health.WATCH", "name": "Apple Watch", "product_type": "Watch7,1"}
PHONE = {"bundle_id": "com.apple.health.PHONE", "name": "iPhone"}
HR = D.HEART_RATE


def q(uuid: str, type_: str, start: str, value, unit: str, source=WATCH, end: str | None = None) -> dict:
    return {"k": "sample", "kind": "quantity", "type": type_, "uuid": uuid, "start": start, "end": end or start,
            "value": value, "unit": unit, "source": source, "device": None, "metadata": {}}


def cat(uuid: str, type_: str, start: str, end: str, value: int, name: str, source=WATCH) -> dict:
    return {"k": "sample", "kind": "category", "type": type_, "uuid": uuid, "start": start, "end": end,
            "value": value, "value_name": name, "source": source, "device": None, "metadata": {}}


def records() -> list[dict]:
    """A made-up day (local date 2026-11-12, UTC+7)."""
    recs = [q(f"00000000-0000-4000-8000-0000000001{i:02d}", HR, f"2026-11-12T0{i // 2}:{(i % 2) * 30:02d}:00.000Z",
              62 + i, "count/min") for i in range(6)]
    recs += [
        q("00000000-0000-4000-8000-000000000201", "HKQuantityTypeIdentifierStepCount", "2026-11-12T02:00:00.000Z",
          6120, "count", end="2026-11-12T02:59:00.000Z"),
        q("00000000-0000-4000-8000-000000000202", "HKQuantityTypeIdentifierStepCount", "2026-11-12T02:00:00.000Z",
          5980, "count", PHONE, end="2026-11-12T02:59:00.000Z"),
        q("00000000-0000-4000-8000-000000000203", "HKQuantityTypeIdentifierActiveEnergyBurned", "2026-11-12T02:00:00.000Z",
          412.5, "kcal", end="2026-11-12T10:00:00.000Z"),
        q("00000000-0000-4000-8000-000000000301", "HKQuantityTypeIdentifierRestingHeartRate", "2026-11-12T03:00:00.000Z",
          54, "count/min"),
        q("00000000-0000-4000-8000-000000000302", "HKQuantityTypeIdentifierHeartRateVariabilitySDNN",
          "2026-11-11T20:00:00.000Z", 48.2, "ms"),
        q("00000000-0000-4000-8000-000000000303", "HKQuantityTypeIdentifierOxygenSaturation", "2026-11-11T21:00:00.000Z",
          0.97, "%"),
        q("00000000-0000-4000-8000-000000000304", "HKQuantityTypeIdentifierBodyMass", "2026-11-12T00:30:00.000Z",
          150.4, "lb", PHONE),
        q("00000000-0000-4000-8000-000000000305", "HKQuantityTypeIdentifierBloodGlucose", "2026-11-12T01:00:00.000Z",
          5.4, "mmol<180.15588000005408>/L", PHONE),
        q("00000000-0000-4000-8000-000000000306", "HKQuantityTypeIdentifierVO2Max", "2026-11-12T11:00:00.000Z",
          41.3, "mL/kg·min"),
        q("00000000-0000-4000-8000-000000000307", "HKQuantityTypeIdentifierDietaryWater", "2026-11-12T04:00:00.000Z",
          250, "mL", PHONE),
        cat("00000000-0000-4000-8000-000000000401", "HKCategoryTypeIdentifierMindfulSession", "2026-11-12T05:00:00.000Z",
            "2026-11-12T05:10:00.000Z", 0, "notApplicable", PHONE),
        {**q("00000000-0000-4000-8000-000000000501", "HKCorrelationTypeIdentifierBloodPressure",
             "2026-11-12T00:45:00.000Z", None, "", PHONE), "kind": "correlation", "objects": [
            q("00000000-0000-4000-8000-000000000502", "HKQuantityTypeIdentifierBloodPressureSystolic",
              "2026-11-12T00:45:00.000Z", 118, "mmHg", PHONE),
            q("00000000-0000-4000-8000-000000000503", "HKQuantityTypeIdentifierBloodPressureDiastolic",
              "2026-11-12T00:45:00.000Z", 76, "mmHg", PHONE)]},
    ]
    stages = [("2026-11-11T15:30:00.000Z", "2026-11-11T22:40:00.000Z", 0, "inBed"),
              ("2026-11-11T15:40:00.000Z", "2026-11-11T17:00:00.000Z", 3, "asleepCore"),
              ("2026-11-11T17:00:00.000Z", "2026-11-11T18:10:00.000Z", 4, "asleepDeep"),
              ("2026-11-11T18:10:00.000Z", "2026-11-11T18:20:00.000Z", 2, "awake"),
              ("2026-11-11T18:20:00.000Z", "2026-11-11T20:30:00.000Z", 3, "asleepCore"),
              ("2026-11-11T20:30:00.000Z", "2026-11-11T22:30:00.000Z", 5, "asleepREM")]
    recs += [cat(f"00000000-0000-4000-8000-0000000006{i:02d}", D.SLEEP, a, b, v, n) for i, (a, b, v, n) in enumerate(stages)]
    recs.append({"k": "workout", "type": D.WORKOUT, "uuid": "00000000-0000-4000-8000-000000000701",
                 "start": "2026-11-12T10:00:00.000Z", "end": "2026-11-12T11:05:00.000Z", "activity_type": 79,
                 "activity_name": "pickleball", "duration_s": 3720,
                 "stats": {HR: {"unit": "count/min", "avg": 131.0, "min": 92.0, "max": 166.0},
                           "HKQuantityTypeIdentifierActiveEnergyBurned": {"unit": "kcal", "sum": 486.2},
                           "HKQuantityTypeIdentifierDistanceWalkingRunning": {"unit": "km", "sum": 3.1}},
                 "events": [], "activities": [], "source": WATCH, "device": None, "metadata": {}})
    recs.append({"k": "route", "uuid": "00000000-0000-4000-8000-000000000702",
                 "workout_uuid": "00000000-0000-4000-8000-000000000701", "part": 0, "parts": 1,
                 "start": "2026-11-12T10:00:00.000Z", "end": "2026-11-12T11:05:00.000Z",
                 "points": [[1794564000000 + i * 5000, 10.7700 + i * 0.0001, 106.7000 + i * 0.0001, 6.0, 4, 3, 1.5, 45,
                             None, None] for i in range(3)]})
    recs.append({"k": "ecg", "uuid": "00000000-0000-4000-8000-000000000801", "type": "HKDataTypeIdentifierElectrocardiogram",
                 "start": "2026-11-12T12:00:00.000Z", "end": "2026-11-12T12:00:30.000Z", "classification": "sinusRhythm",
                 "avg_hr": 66, "sampling_hz": 512, "symptoms": "none", "part": 0, "parts": 1,
                 "voltages_uv": [12.5, 40.25, 310.0, -55.5, 8.0, None, 4.75], "source": WATCH, "device": None,
                 "metadata": {}})

    def test(side: str, db: float, masked: bool = False, lower: float | None = None, conduction: str = "air") -> dict:
        t = {"side": side, "conduction": conduction, "masked": masked, "sensitivity_db_hl": db}
        return {**t, "clamping": {"lower_db_hl": lower, "upper_db_hl": None}} if lower is not None else t

    points = [(500, [test("left", 10), test("right", 15)]),
              (1000, [test("left", 5, lower=5), test("right", 20), test("right", 25, masked=True)]),
              (4000, [test("left", 30), test("right", 35), test("left", 20, conduction="bone")]),
              (10000, [test("left", 40), test("right", 45)])]  # no LOINC code: stays in the day folder
    recs.append({"k": "object", "class": "HKAudiogramSample", "type": F.AUDIOGRAM,
                 "uuid": "00000000-0000-4000-8000-000000000901", "start": "2026-11-12T13:00:00.000Z",
                 "end": "2026-11-12T13:06:00.000Z", "source": PHONE, "device": {"model": "AirPods Pro"},
                 "metadata": {"HKAlgorithmVersion": 1},
                 "data": {"sensitivity_points": [
                     {"frequency_hz": hz, "left_db_hl": next(t["sensitivity_db_hl"] for t in ts if t["side"] == "left"),
                      "right_db_hl": next(t["sensitivity_db_hl"] for t in ts if t["side"] == "right"), "tests": ts}
                     for hz, ts in points]}})
    return recs


# Example file name -> identifier value of the resource in the mapped day.
PICK = {
    "Observation-steps": f"{USER}/{DAY}/HKQuantityTypeIdentifierStepCount",
    "Observation-active-energy": f"{USER}/{DAY}/HKQuantityTypeIdentifierActiveEnergyBurned",
    "Observation-heart-rate-minutes": f"{USER}/{DAY}/hr-minutes",
    "Observation-heart-rate-max": f"{USER}/{DAY}/hr-max",
    "Observation-resting-heart-rate": "00000000-0000-4000-8000-000000000301",
    "Observation-hrv-sdnn": "00000000-0000-4000-8000-000000000302",
    "Observation-spo2": "00000000-0000-4000-8000-000000000303",
    "Observation-weight": "00000000-0000-4000-8000-000000000304",
    "Observation-blood-glucose": "00000000-0000-4000-8000-000000000305",
    "Observation-vo2-max": "00000000-0000-4000-8000-000000000306",
    "Observation-dietary-water": "00000000-0000-4000-8000-000000000307",
    "Observation-mindful-session": "00000000-0000-4000-8000-000000000401",
    "Observation-blood-pressure": "00000000-0000-4000-8000-000000000501",
    "Observation-sleep": f"{USER}/{DAY}/sleep",
    "Observation-workout": "00000000-0000-4000-8000-000000000701",
    "DocumentReference-workout-route": "route/00000000-0000-4000-8000-000000000701",
    "Observation-ecg": "00000000-0000-4000-8000-000000000801",
    "Observation-audiogram": "00000000-0000-4000-8000-000000000901",
}
LITERAL = {"Patient": "Patient/example", "Device": "Device/example",
           "DocumentReference": "DocumentReference/workout-route"}


def literal_refs(node):
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "reference" and isinstance(v, str) and "?identifier=" in v:
                v = LITERAL[v.split("?", 1)[0]]
            out[k] = literal_refs(v)
        return out
    if isinstance(node, list):
        return [literal_refs(v) for v in node]
    return node


def examples() -> dict[str, dict]:
    files: dict[str, list[dict]] = defaultdict(list)
    for rec in records():
        files[D.file_name(rec)].append(rec)
    summary = D.summarize(DAY, files)
    resources = F.Mapper(F.DEFAULT_BASE, USER, DAY, TZ, files, summary).run()
    by_ident = {r["identifier"][0]["value"]: r for r in resources}
    out = {}
    for name, ident in PICK.items():
        res = literal_refs(by_ident[ident])
        out[name] = {"resourceType": res["resourceType"], "id": name.split("-", 1)[1], **res}
    out["Patient-example"] = {"resourceType": "Patient", "id": "example", **F.patient(
        F.DEFAULT_BASE, USER, "Example Person", {"biological_sex": "female", "date_of_birth": "1960-02-03"})}
    out["Device-example"] = {"resourceType": "Device", "id": "example",
                             **F.device(F.DEFAULT_BASE, USER, WATCH["bundle_id"], WATCH["name"])}
    return out


def main() -> int:
    check = sys.argv[1:] == ["--check"]
    if sys.argv[1:] not in ([], ["--check"]):
        print(__doc__)
        return 2
    wanted = {f"{name}.json": json.dumps(res, indent=1, ensure_ascii=False) + "\n" for name, res in examples().items()}
    present = {p.name for p in OUT.glob("*.json")} if OUT.is_dir() else set()
    if check:
        stale = sorted(n for n in wanted if not (OUT / n).is_file() or (OUT / n).read_text() != wanted[n])
        extra = sorted(present - set(wanted))
        if stale or extra:
            print("examples out of date (run python3 tools/fhir_examples.py): " + ", ".join(stale + extra))
            return 1
        print(f"{len(wanted)} examples match the mapper")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for name in present - set(wanted):
        (OUT / name).unlink()
    for name, text in wanted.items():
        (OUT / name).write_text(text)
    print(f"wrote {len(wanted)} examples to fhir/examples/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
