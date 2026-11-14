"""`hka fhir map` and `hka fhir load`: FHIR R4 from the day folders (structure follows garmin-archive).

FHIR is generated from the decrypted day folders, never stored as the only copy. Each day becomes one
NDJSON file of resources with stable identifiers, so loading is a conditional upsert:

  users/<user>/fhir/days/YYYY/YYYY-MM-DD.ndjson   Observations and DocumentReferences of the day
  users/<user>/fhir/shared.ndjson                  Patient and Devices (sources)
  users/<user>/fhir/manifest.json                  per day: source hash, mapping version, output hash
  users/<user>/fhir/load-manifest.json             per day: what the server holds

Codes: LOINC where a correct code exists (each checked against the NLM LOINC API, see LOINC), always
with the HealthKit type identifier as a second coding, and the `hka-metrics` CodeSystem for values that
have no correct LOINC code. See docs/fhir-mapping.md.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

from hkacommon.encoding import is_instant, parse_instant, sha256_hex
from . import days as D
from .build import read_lines
from .store import Store, atomic_write, instant, read_json, write_json

MAPPING_VERSION = "0.3.0"
DEFAULT_BASE = "https://github.com/jollyjoker992/healthkit-archive/fhir"
UCUM = "http://unitsofmeasure.org"
LOINC_SYSTEM = "http://loinc.org"
CATEGORY_SYSTEM = "http://terminology.hl7.org/CodeSystem/observation-category"

# LOINC codes with their display and the only unit used with them. Codes shared with garmin-archive
# were checked there; the others against https://clinicaltables.nlm.nih.gov on 2026-11-14.
LOINC = {
    "41950-7": ("Number of steps in 24 hour Measured", "/d"),
    "41953-1": ("Walking distance 24 hour Calculated", "m"),
    "93833-2": ("Flights climbed 24 hour", "{#}"),
    "40443-4": ("Heart rate --resting", "/min"),
    "8883-1": ("Heart rate 24 hour minimum", "/min"),
    "8873-2": ("Heart rate 24 hour maximum", "/min"),
    "8867-4": ("Heart rate", "/min"),
    "2708-6": ("Oxygen saturation in Arterial blood", "%"),
    "59408-5": ("Oxygen saturation in Arterial blood by Pulse oximetry", "%"),
    "9279-1": ("Respiratory rate", "/min"),
    "8310-5": ("Body temperature", "Cel"),
    "29463-7": ("Body weight", "kg"),
    "8302-2": ("Body height", "cm"),
    "39156-5": ("Body mass index (BMI) [Ratio]", "kg/m2"),
    "41982-0": ("Percentage of body fat Measured", "%"),
    "91557-9": ("Lean body weight", "kg"),
    "8280-0": ("Waist Circumference at umbilicus by Tape measure", "cm"),
    "80404-7": ("R-R interval.standard deviation (Heart rate variability)", "ms"),
    "2339-0": ("Glucose [Mass/volume] in Blood", "mg/dL"),
    "85354-9": ("Blood pressure panel with all children optional", None),
    "8480-6": ("Systolic blood pressure", "mm[Hg]"),
    "8462-4": ("Diastolic blood pressure", "mm[Hg]"),
    "93832-4": ("Sleep duration", "min"),
    "93831-6": ("Deep sleep duration", "min"),
    "93830-8": ("Light sleep duration", "min"),
    "93829-0": ("REM sleep duration", "min"),
    "93828-2": ("Nighttime awakening duration", "min"),
    "55411-3": ("Exercise duration", "min"),
    "73985-4": ("Exercise activity", None),
    "55425-3": ("Heart rate unspecified time mean by Pedometer", "/min"),
    "55426-1": ("Heart rate unspecified time maximum by Pedometer", "/min"),
    "55424-6": ("Calories burned in unspecified time Pedometer", "kcal"),
}

# Values without a correct LOINC code: code -> (display, UCUM unit or None for text, definition).
METRICS = {
    "active-energy-daily": ("Active energy burned, day total", "kcal", "Sum of HKQuantityTypeIdentifierActiveEnergyBurned for the local day, one source."),
    "basal-energy-daily": ("Basal energy burned, day total", "kcal", "Sum of HKQuantityTypeIdentifierBasalEnergyBurned for the local day, one source."),
    "exercise-minutes-daily": ("Exercise minutes, day total", "min", "Sum of HKQuantityTypeIdentifierAppleExerciseTime for the local day."),
    "distance-cycling-daily": ("Cycling distance, day total", "m", "Sum of HKQuantityTypeIdentifierDistanceCycling for the local day."),
    "hr-minute-mean": ("Heart rate, 1-minute means", "/min", "Mean of the heart-rate samples starting in each minute of the local day; E where there is none."),
    "sleep-in-bed": ("Time in bed", "min", "Minutes of HKCategoryValueSleepAnalysis.inBed for the night ending on this day."),
    "sleep-unspecified": ("Asleep, stage not specified", "min", "Minutes of asleepUnspecified for the night ending on this day."),
    "workout-distance": ("Workout distance", "m", "Total distance of the workout."),
    "workout-active-energy": ("Workout active energy", "kcal", "Active energy of the workout from its statistics."),
    "ecg-lead-i": ("ECG voltage, lead I (Apple Watch)", "uV", "Voltage samples similar to lead I at the recorded sampling frequency."),
    "ecg-classification": ("ECG classification", None, "HKElectrocardiogram.Classification case name."),
    "vo2-max": ("VO2 max", "mL/(kg.min)", "HKQuantityTypeIdentifierVO2Max. LOINC 60842-2 is VO2, not VO2 max."),
}

# HealthKit unit string -> (UCUM unit, factor). Percent values are fractions in HealthKit.
UNITS = {
    "count/min": ("/min", 1), "count": ("{#}", 1), "kcal": ("kcal", 1), "Cal": ("kcal", 1), "kJ": ("kJ", 1),
    "m": ("m", 1), "km": ("m", 1000), "cm": ("cm", 1), "mm": ("mm", 1), "mi": ("m", 1609.344), "ft": ("cm", 30.48),
    "in": ("cm", 2.54), "yd": ("m", 0.9144), "%": ("%", 100), "mmHg": ("mm[Hg]", 1), "degC": ("Cel", 1),
    "kg": ("kg", 1), "g": ("g", 1), "lb": ("kg", 0.45359237), "oz": ("g", 28.349523125), "ms": ("ms", 1), "s": ("s", 1),
    "min": ("min", 1), "hr": ("h", 1), "mg/dL": ("mg/dL", 1), "mmol<180.15588000005408>/L": ("mmol/L", 1),
    "mL/kg·min": ("mL/(kg.min)", 1), "ml/(kg*min)": ("mL/(kg.min)", 1), "dBASPL": ("dB", 1), "L/min": ("L/min", 1),
    "mL": ("mL", 1), "L": ("L", 1), "W": ("W", 1), "m/s": ("m/s", 1), "S": ("S", 1), "mcS": ("uS", 1), "IU": ("[iU]", 1),
    "mg": ("mg", 1), "mcg": ("ug", 1), "Hz": ("Hz", 1), "lx": ("lx", 1),
    "mL/min·kg": ("mL/(kg.min)", 1), "st": ("kg", 6.35029318), "fl_oz_us": ("mL", 29.5735295625),
    "fl_oz_imp": ("mL", 28.4130625), "km/hr": ("km/h", 1), "mi/hr": ("km/h", 1.609344), "kcal/hr·kg": ("kcal/(h.kg)", 1),
    "appleEffortScore": ("{score}", 1), "cup_us": ("mL", 236.5882365),
}

KJ_PER_KCAL = 4.184
GLUCOSE_MMOL_TO_MG_DL = 18.015588  # molar mass of glucose / 10

# Per-sample quantity types with a LOINC code (category, LOINC refs, target UCUM unit).
SAMPLE_LOINC = {
    "HKQuantityTypeIdentifierRestingHeartRate": ("vital-signs", ["40443-4"], "/min"),
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": ("vital-signs", ["80404-7"], "ms"),
    "HKQuantityTypeIdentifierOxygenSaturation": ("vital-signs", ["2708-6", "59408-5"], "%"),
    "HKQuantityTypeIdentifierRespiratoryRate": ("vital-signs", ["9279-1"], "/min"),
    "HKQuantityTypeIdentifierBodyTemperature": ("vital-signs", ["8310-5"], "Cel"),
    "HKQuantityTypeIdentifierBodyMass": ("vital-signs", ["29463-7"], "kg"),
    "HKQuantityTypeIdentifierHeight": ("vital-signs", ["8302-2"], "cm"),
    "HKQuantityTypeIdentifierBodyMassIndex": ("vital-signs", ["39156-5"], "kg/m2"),
    "HKQuantityTypeIdentifierBodyFatPercentage": ("vital-signs", ["41982-0"], "%"),
    "HKQuantityTypeIdentifierLeanBodyMass": ("vital-signs", ["91557-9"], "kg"),
    "HKQuantityTypeIdentifierWaistCircumference": ("vital-signs", ["8280-0"], "cm"),
    "HKQuantityTypeIdentifierBloodGlucose": ("laboratory", ["2339-0"], "mg/dL"),
}

# Quantity types aggregated per day instead of one Observation per sample.
DAILY_ONLY = set(D.CUMULATIVE) | {D.HEART_RATE}

# Sleep stage name -> LOINC (nightly totals; the per-interval timeline stays in the day folder).
SLEEP_STAGES = {"asleepDeep": "93831-6", "asleepCore": "93830-8", "asleepREM": "93829-0", "awake": "93828-2"}


class MappingError(Exception):
    pass


def ucum(value, hk_unit: str | None, target: str | None = None) -> tuple[float, str] | None:
    """Convert a HealthKit value to UCUM; None when the unit is unknown or does not match `target`."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not isinstance(hk_unit, str):
        return None
    # Units HealthKit spells with variable parts, or that need more than a factor.
    if hk_unit == "degF":
        return (round((value - 32) * 5 / 9, 6), "Cel") if target in (None, "Cel") else None
    if hk_unit.startswith("mmol<") and hk_unit.endswith(">/L"):  # glucose; the molar mass digits vary by OS
        return (round(value * GLUCOSE_MMOL_TO_MG_DL, 3), "mg/dL") if target in (None, "mg/dL") else None
    if hk_unit == "kJ" and target == "kcal":
        return round(value / KJ_PER_KCAL, 6), "kcal"
    if hk_unit not in UNITS:
        return None
    unit, factor = UNITS[hk_unit]
    if hk_unit == "count" and target == "kg/m2":  # BMI is a plain count in HealthKit
        unit = "kg/m2"
    if target is not None and unit != target:
        if unit == "g" and target == "kg":
            value, unit = value / 1000, "kg"
        else:
            return None
    return round(value * factor, 6), unit


def quantity(value: float, unit: str) -> dict:
    return {"value": value, "unit": unit, "system": UCUM, "code": unit}


class Mapper:
    """Builds the FHIR resources of one user's day."""

    def __init__(self, base: str, user_id: str, day: str, tz: str | None, files: dict[str, list[dict]],
                 summary: dict | None, final: bool = True):
        self.base, self.user_id, self.day, self.files, self.summary = base, user_id, day, files, summary or {}
        self.status = "final" if final else "preliminary"  # a provisional day can still change
        self.unmapped = 0
        self.zone = D.zone(tz)
        start = datetime.fromisoformat(day).replace(tzinfo=self.zone)
        self.day_start, self.day_end = start, start + timedelta(days=1)
        self.resources: list[dict] = []
        self.sources: dict[str, str] = {}
        self.binaries: dict[str, bytes] = {}

    # -- building blocks ------------------------------------------------------------------
    def ident(self, kind: str, value: str) -> list[dict]:
        return [{"system": f"{self.base}/identifier/{kind}", "value": value}]

    def ref(self, rtype: str, kind: str, value: str) -> dict:
        return {"reference": f"{rtype}?identifier={self.base}/identifier/{kind}|{value}"}

    def code(self, loinc: list[str] = (), hk_type: str | None = None, metric: str | None = None, text: str | None = None):
        codings = [{"system": LOINC_SYSTEM, "code": c, "display": LOINC[c][0]} for c in loinc]
        if metric:
            codings.append({"system": f"{self.base}/CodeSystem/hka-metrics", "code": metric, "display": METRICS[metric][0]})
        if hk_type:
            codings.append({"system": f"{self.base}/CodeSystem/healthkit-type", "code": hk_type})
        out = {"coding": codings}
        if text:
            out["text"] = text
        return out

    def local_iso(self, dt: datetime) -> str:
        return dt.astimezone(self.zone).isoformat(timespec="milliseconds")

    def period(self, rec: dict) -> dict:
        start, end = rec.get("start"), rec.get("end")
        if not (is_instant(start) and is_instant(end)):
            raise MappingError("record without valid start/end")
        if start == end:
            return {"effectiveDateTime": self.local_iso(parse_instant(start))}
        return {"effectivePeriod": {"start": self.local_iso(parse_instant(start)), "end": self.local_iso(parse_instant(end))}}

    def day_period(self) -> dict:
        return {"effectivePeriod": {"start": self.day_start.isoformat(), "end": self.day_end.isoformat()}}

    def device(self, rec: dict) -> dict | None:
        src = rec.get("source")
        bundle = src.get("bundle_id") if isinstance(src, dict) else None
        if not isinstance(bundle, str):
            return None
        if isinstance(src.get("name"), str):
            self.sources[bundle] = src["name"]
        else:
            self.sources.setdefault(bundle, bundle)
        return self.source_ref(bundle)

    def source_ref(self, bundle: str) -> dict:
        return self.ref("Device", "hka-source", f"{self.user_id}/{bundle}")  # per user: members share apps

    def observation(self, key: str, code: dict, effective: dict, *, category: str | None = None, value=None,
                    components: list | None = None, device: dict | None = None, **extra) -> dict:
        obs = {"resourceType": "Observation", "identifier": self.ident("observation", key), "status": self.status}
        if category:
            obs["category"] = [{"coding": [{"system": CATEGORY_SYSTEM, "code": category}]}]
        obs["code"] = code
        obs["subject"] = self.ref("Patient", "hka-user", self.user_id)
        obs.update(effective)
        if device:
            obs["device"] = device
        if value is not None:
            obs.update(value)
        comps = [c for c in components or [] if c]
        if comps:
            obs["component"] = comps
        obs.update(extra)
        self.resources.append(obs)
        return obs

    def component(self, code: dict, q: tuple[float, str] | None = None, text: str | None = None) -> dict | None:
        if q is not None:
            return {"code": code, "valueQuantity": quantity(*q)}
        if text is not None:
            return {"code": code, "valueCodeableConcept": {"text": text}}
        return None

    # -- the day ------------------------------------------------------------------------
    def run(self) -> list[dict]:
        self.daily_totals()
        self.heart_rate()
        self.samples()
        self.sleep()
        self.workouts()
        self.ecg()
        keys = [f"{r['resourceType']}|{r['identifier'][0]['value']}" for r in self.resources]
        if len(keys) != len(set(keys)):
            raise MappingError("two resources with the same identifier")
        return self.resources

    def key(self, name: str) -> str:
        return f"{self.user_id}/{self.day}/{name}"  # daily resources: unique per user

    def daily_totals(self) -> None:
        totals = self.summary.get("totals") or {}
        plan = [
            ("HKQuantityTypeIdentifierStepCount", ["41950-7"], None, "/d", "activity"),
            ("HKQuantityTypeIdentifierDistanceWalkingRunning", ["41953-1"], None, "m", "activity"),
            ("HKQuantityTypeIdentifierFlightsClimbed", ["93833-2"], None, "{#}", "activity"),
            ("HKQuantityTypeIdentifierActiveEnergyBurned", [], "active-energy-daily", "kcal", "activity"),
            ("HKQuantityTypeIdentifierBasalEnergyBurned", [], "basal-energy-daily", "kcal", "activity"),
            ("HKQuantityTypeIdentifierAppleExerciseTime", [], "exercise-minutes-daily", "min", "activity"),
            ("HKQuantityTypeIdentifierDistanceCycling", [], "distance-cycling-daily", "m", "activity"),
        ]
        for hk, loinc, metric, unit, category in plan:
            t = totals.get(hk)
            if not t:
                continue
            target = "{#}" if unit in ("/d", "{#}") else unit
            q = ucum(t.get("value"), t.get("unit"), target)
            if q is None:
                continue
            value = quantity(q[0], unit)
            self.observation(self.key(hk), self.code(loinc, hk, metric), self.day_period(), category=category,
                             value={"valueQuantity": value}, device=self.source_ref(t["source"])
                             if isinstance(t.get("source"), str) and t["source"] != "?" else None)
            if isinstance(t.get("source"), str) and t["source"] != "?":
                self.sources.setdefault(t["source"], t["source"])

    def heart_rate(self) -> None:
        recs = [r for r in self.files.get(f"{D.HEART_RATE}.ndjson", []) if r.get("unit") == "count/min"
                and isinstance(r.get("value"), (int, float)) and is_instant(r.get("start"))]
        if not recs:
            return
        # A local day is 1380 or 1500 minutes on daylight-saving changes.
        # Subtract in UTC: Python ignores the offset change when both datetimes share a tzinfo.
        utc = D.zone("UTC")
        slots = int((self.day_end.astimezone(utc) - self.day_start.astimezone(utc)).total_seconds() // 60)
        buckets: dict[int, list[float]] = defaultdict(list)
        for r in recs:
            minute = int((parse_instant(r["start"]) - self.day_start).total_seconds() // 60)
            if 0 <= minute < slots:
                buckets[minute].append(r["value"])
            else:
                self.unmapped += 1
        if buckets:
            data = " ".join(fmt(sum(v) / len(v), 1) if v else "E" for v in (buckets.get(i, []) for i in range(slots)))
            # Not coded 8867-4/vital-signs: the vital-signs heart-rate profile allows only a Quantity value.
            self.observation(self.key("hr-minutes"), self.code([], D.HEART_RATE, "hr-minute-mean"), self.day_period(),
                             value={"valueSampledData": {"origin": quantity(0, "/min"), "period": 60000, "dimensions": 1,
                                                         "data": data}})
        hr = self.summary.get("heart_rate") or {}
        for name, code, field in (("hr-min", "8883-1", "min"), ("hr-max", "8873-2", "max")):
            if isinstance(hr.get(field), (int, float)):
                self.observation(self.key(name), self.code([code], D.HEART_RATE), self.day_period(),
                                 category="vital-signs", value={"valueQuantity": quantity(hr[field], "/min")})

    def samples(self) -> None:
        for fname, recs in sorted(self.files.items()):
            for r in recs:
                if r.get("k") != "sample" or not isinstance(r.get("uuid"), str):
                    continue
                t = r.get("type")
                try:
                    if r.get("kind") == "quantity" and t in BP_MEMBERS:
                        continue  # part of a blood-pressure correlation, mapped there
                    if r.get("kind") == "quantity" and t not in DAILY_ONLY:
                        if not self.quantity_sample(r):
                            self.unmapped += 1
                    elif r.get("kind") == "category" and t != D.SLEEP:
                        self.observation(r["uuid"], self.code(hk_type=t), self.period(r), device=self.device(r),
                                         value={"valueCodeableConcept": {"text": str(r.get("value_name") or r.get("value"))}})
                    elif r.get("kind") == "correlation" and t == "HKCorrelationTypeIdentifierBloodPressure":
                        self.blood_pressure(r)
                except MappingError:
                    continue  # record kept in the day folder; nothing correct to say in FHIR

    def quantity_sample(self, r: dict) -> bool:
        """Map one quantity sample; False when its unit cannot be expressed correctly."""
        t = r.get("type")
        if t in SAMPLE_LOINC:
            category, loinc, target = SAMPLE_LOINC[t]
            q = ucum(r.get("value"), r.get("unit"), target)
            if q is None:
                return False
            self.observation(r["uuid"], self.code(loinc, t), self.period(r), category=category,
                             value={"valueQuantity": quantity(*q)}, device=self.device(r))
        elif t == "HKQuantityTypeIdentifierVO2Max":
            q = ucum(r.get("value"), r.get("unit"), "mL/(kg.min)")
            if q is None:
                return False
            self.observation(r["uuid"], self.code([], t, "vo2-max"), self.period(r),
                             value={"valueQuantity": quantity(*q)}, device=self.device(r))
        else:
            q = ucum(r.get("value"), r.get("unit"))
            if q is None:
                return False
            self.observation(r["uuid"], self.code(hk_type=t), self.period(r), value={"valueQuantity": quantity(*q)},
                             device=self.device(r))
        return True

    def blood_pressure(self, r: dict) -> None:
        comps = []
        for member in r.get("objects") or []:
            if not isinstance(member, dict):
                continue
            code = {"HKQuantityTypeIdentifierBloodPressureSystolic": "8480-6",
                    "HKQuantityTypeIdentifierBloodPressureDiastolic": "8462-4"}.get(member.get("type"))
            q = ucum(member.get("value"), member.get("unit"), "mm[Hg]")
            if code and q:
                comps.append(self.component(self.code([code], member["type"]), q))
        if len(comps) == 2:  # the vital-signs blood-pressure profile needs both
            self.observation(r["uuid"], self.code(["85354-9"], r["type"]), self.period(r), category="vital-signs",
                             components=comps, device=self.device(r))
        elif comps:
            self.observation(r["uuid"], self.code(hk_type=r["type"]), self.period(r), components=comps,
                             device=self.device(r))

    def sleep(self) -> None:
        s = self.summary.get("sleep")
        if not s:
            return
        stages = s.get("stages_min") or {}
        comps = [self.component(self.code([code], D.SLEEP, text=name), (round(stages[name], 1), "min"))
                 for name, code in SLEEP_STAGES.items() if isinstance(stages.get(name), (int, float))]
        for name, metric in (("inBed", "sleep-in-bed"), ("asleepUnspecified", "sleep-unspecified")):
            if isinstance(stages.get(name), (int, float)):
                comps.append(self.component(self.code([], D.SLEEP, metric), (round(stages[name], 1), "min")))
        chosen = s.get("source")
        recs = [r for r in self.files.get(f"{D.SLEEP}.ndjson", []) if is_instant(r.get("start")) and is_instant(r.get("end"))
                and (r.get("source") or {}).get("bundle_id") == chosen]
        if recs:
            effective = {"effectivePeriod": {"start": self.local_iso(min(parse_instant(r["start"]) for r in recs)),
                                             "end": self.local_iso(max(parse_instant(r["end"]) for r in recs))}}
        else:
            effective = self.day_period()
        asleep = s.get("asleep_min")
        has_asleep = any(k.startswith("asleep") for k in stages)
        value = ({"valueQuantity": quantity(round(asleep, 1), "min")} if has_asleep and isinstance(asleep, (int, float))
                 else {"dataAbsentReason": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/data-absent-reason",
                                                         "code": "unknown"}]}})
        self.observation(self.key("sleep"), self.code(["93832-4"], D.SLEEP), effective, value=value, components=comps,
                         device=self.source_ref(s["source"]) if s.get("source") not in (None, "?") else None)
        if s.get("source") not in (None, "?"):
            self.sources.setdefault(s["source"], s["source"])

    def workouts(self) -> None:
        routes: dict[str, list[dict]] = defaultdict(list)
        for r in self.files.get("HKWorkoutRoute.ndjson", []):
            if isinstance(r.get("workout_uuid"), str):
                routes[r["workout_uuid"]].append(r)
        for w in self.files.get(f"{D.WORKOUT}.ndjson", []):
            if not isinstance(w.get("uuid"), str):
                continue
            try:
                effective = self.period(w)
            except MappingError:
                continue
            stats = w.get("stats") if isinstance(w.get("stats"), dict) else {}
            hr = stats.get(D.HEART_RATE) or {}
            energy = stats.get("HKQuantityTypeIdentifierActiveEnergyBurned") or {}
            distances = [stats[k] for k in sorted(stats) if k.startswith("HKQuantityTypeIdentifierDistance")
                         and isinstance(stats[k], dict) and isinstance(stats[k].get("sum"), (int, float))]
            distance = max(distances, key=lambda d: ucum(d["sum"], d.get("unit"), "m") or (0, ""), default={})
            comps = [
                self.component(self.code(["73985-4"]), text=str(w.get("activity_name") or w.get("activity_type"))),
                self.component(self.code(["55425-3"]), ucum(hr.get("avg"), hr.get("unit"), "/min")),
                self.component(self.code(["55426-1"]), ucum(hr.get("max"), hr.get("unit"), "/min")),
                self.component(self.code(["55424-6"]), ucum(energy.get("sum"), energy.get("unit"), "kcal")),
                self.component(self.code([], None, "workout-distance"), ucum(distance.get("sum"), distance.get("unit"), "m")),
            ]
            extra = {}
            parts = sorted(routes.get(w["uuid"], []), key=lambda r: r.get("part", 0))
            if parts:
                doc_key = f"route/{w['uuid']}"
                gpx = route_gpx(parts, w)
                # The GPX is uploaded as a Binary by the loader (large, and kept out of searchable resources).
                self.binaries[doc_key] = gpx
                self.resources.append({
                    "resourceType": "DocumentReference", "identifier": self.ident("document", doc_key), "status": "current",
                    "type": {"text": "Workout route (GPX)"}, "subject": self.ref("Patient", "hka-user", self.user_id),
                    "date": effective.get("effectivePeriod", {}).get("start") or effective.get("effectiveDateTime"),
                    "content": [{"attachment": {
                        "contentType": "application/gpx+xml", "size": len(gpx),
                        "hash": base64.b64encode(hashlib.sha1(gpx).digest()).decode(), "title": f"{w['uuid']}.gpx",
                        "extension": [{"url": f"{self.base}/StructureDefinition/archive-path",
                                       "valueString": f"fhir/binaries/{hashlib.sha256(gpx).hexdigest()}.gpx"}]}}],
                })
                extra["derivedFrom"] = [self.ref("DocumentReference", "document", doc_key)]
            duration = w.get("duration_s")
            self.observation(w["uuid"], self.code(["55411-3"], D.WORKOUT), effective, category="activity",
                             value={"valueQuantity": quantity(round(duration / 60, 2), "min")}
                             if isinstance(duration, (int, float)) else None,
                             components=comps, device=self.device(w), **extra)

    def ecg(self) -> None:
        parts: dict[str, list[dict]] = defaultdict(list)
        for r in self.files.get("HKDataTypeIdentifierElectrocardiogram.ndjson", []):
            if isinstance(r.get("uuid"), str):
                parts[r["uuid"]].append(r)
        for uuid, recs in parts.items():
            recs.sort(key=lambda r: r.get("part", 0))
            first = recs[0]
            hz = first.get("sampling_hz")
            complete = [r.get("part", 0) for r in recs] == list(range(first.get("parts", 1) if isinstance(first.get("parts"), int) else 1))
            voltages = [v for r in recs for v in (r.get("voltages_uv") or [])] if complete else []
            try:
                effective = self.period(first)
            except MappingError:
                continue
            comps = [self.component(self.code([], None, "ecg-classification"), text=first["classification"])
                     if isinstance(first.get("classification"), str) else None]
            if isinstance(hz, (int, float)) and hz > 0 and voltages:
                data = " ".join("E" if not isinstance(v, (int, float)) else f"{v:.3f}".rstrip("0").rstrip(".") for v in voltages)
                comps.append({"code": self.code([], None, "ecg-lead-i"), "valueSampledData": {
                    "origin": quantity(0, "uV"), "period": round(1000 / hz, 6), "dimensions": 1, "data": data}})
            avg = ucum(first.get("avg_hr"), "count/min", "/min")
            comps.append(self.component(self.code(["8867-4"]), avg) if avg else None)
            self.observation(uuid, self.code([], first.get("type")), effective, category="procedure", components=comps,
                             device=self.device(first))


def fmt(x: float, digits: int) -> str:
    """Fixed-point decimal without trailing zeros (never exponent notation)."""
    text = f"{x:.{digits}f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


BP_MEMBERS = {"HKQuantityTypeIdentifierBloodPressureSystolic", "HKQuantityTypeIdentifierBloodPressureDiastolic"}


def route_gpx(parts: list[dict], workout: dict) -> bytes:
    points = [p for r in parts for p in (r.get("points") or []) if isinstance(p, list) and len(p) >= 3]
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<gpx version="1.1" creator="healthkit-archive" xmlns="http://www.topografix.com/GPX/1/1">',
           f'<trk><name>{escape(str(workout.get("activity_name") or "workout"))}</name><trkseg>']
    for p in points:
        t, lat, lon = p[0], p[1], p[2]
        if not all(isinstance(x, (int, float)) for x in (t, lat, lon)):
            continue
        ele = f"<ele>{fmt(p[3], 2)}</ele>" if len(p) > 3 and isinstance(p[3], (int, float)) else ""
        when = instant(datetime.fromtimestamp(t / 1000, tz=D.zone("UTC")))
        out.append(f'<trkpt lat="{fmt(lat, 7)}" lon="{fmt(lon, 7)}">{ele}<time>{when}</time></trkpt>')
    out.append("</trkseg></trk></gpx>")
    return "\n".join(out).encode()


# ------------------------------------------------------------------------------ terminology

def codesystems(base: str) -> list[dict]:
    """The hka-metrics and healthkit-type CodeSystems, versioned with the mapping."""
    metrics = {
        "resourceType": "CodeSystem", "url": f"{base}/CodeSystem/hka-metrics", "version": MAPPING_VERSION,
        "name": "HkaMetrics", "title": "healthkit-archive metrics", "status": "active", "content": "complete",
        "description": "Values derived from Apple HealthKit that have no correct LOINC code.",
        "property": [{"code": "unit", "type": "string", "description": "UCUM unit of the value"}],
        "concept": [{"code": c, "display": d, "definition": definition,
                     **({"property": [{"code": "unit", "valueString": u}]} if u else {})}
                    for c, (d, u, definition) in METRICS.items()],
    }
    types = {
        "resourceType": "CodeSystem", "url": f"{base}/CodeSystem/healthkit-type", "version": MAPPING_VERSION,
        "name": "HealthKitType", "title": "Apple HealthKit type identifiers", "status": "active", "content": "fragment",
        "description": "HealthKit type identifier strings (HKQuantityTypeIdentifier…, HKCategoryTypeIdentifier…, …) "
                       "used as a second coding so every Observation round-trips to its HealthKit type.",
        "caseSensitive": True,
    }
    return [metrics, types]


# ------------------------------------------------------------------------------ map

def patient(base: str, user_id: str, name: str, profile: dict | None) -> dict:
    p = {"resourceType": "Patient", "identifier": [{"system": f"{base}/identifier/hka-user", "value": user_id}],
         "name": [{"text": name}]}
    if profile:
        sex = profile.get("biological_sex")
        if sex in ("female", "male", "other"):
            p["gender"] = sex
        if isinstance(profile.get("date_of_birth"), str):
            p["birthDate"] = profile["date_of_birth"]
    return p


def device(base: str, user_id: str, bundle: str, name: str) -> dict:
    return {"resourceType": "Device", "identifier": [{"system": f"{base}/identifier/hka-source", "value": f"{user_id}/{bundle}"}],
            "deviceName": [{"name": name, "type": "user-friendly-name"}]}


def write_ndjson(path: Path, resources: list[dict]) -> str:
    data = b"".join(json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"
                    for r in resources)
    atomic_write(path, data)
    return sha256_hex(data)


def latest_profile(user_dir: Path) -> dict | None:
    best = None
    for line in read_lines(user_dir / "profile.ndjson"):
        rec = json.loads(line)
        if best is None or str(rec.get("observed_at")) > str(best.get("observed_at")):
            best = rec
    return best


def map_user(store: Store, user_id: str, base: str, force: bool = False) -> dict:
    udir = store.user_dir(user_id)
    manifest = read_json(udir / "manifest.json", {"days": {}})
    fdir = udir / "fhir"
    fman = read_json(fdir / "manifest.json", {}) or {}
    if fman.get("mapping_version") != MAPPING_VERSION or fman.get("base") != base:
        # Everything is mapped again; the loader removes what the old mapping produced and the new one does not.
        fman = {"mapping_version": MAPPING_VERSION, "base": base, "days": {}, "sources": fman.get("sources") or {}}
    report = {"mapped": 0, "skipped": 0, "removed": 0, "unmapped_samples": 0, "errors": []}
    sources: dict[str, str] = dict(fman.get("sources") or {})
    failed: set[str] = set(fman.get("failed") or [])
    days = manifest.get("days", {})
    for day, entry in sorted(days.items()):
        src_hash = entry.get("content_sha256")
        fingerprint = f"{src_hash}|{entry.get('tz')}|{entry.get('status')}"
        done = fman["days"].get(day)
        out = fdir / "days" / day[:4] / f"{day}.ndjson"
        if not force and done and done.get("source") == fingerprint and out.exists():
            report["skipped"] += 1
            continue
        day_dir = udir / "days" / day[:4] / day
        try:
            files = {p.name: [json.loads(line) for line in read_lines(p)] for p in sorted(day_dir.glob("*.ndjson"))}
            mapper = Mapper(base, user_id, day, entry.get("tz"), files, read_json(day_dir / "summary.json"),
                            final=entry.get("status") == "final")
            resources = mapper.run()
        except Exception as exc:
            report["errors"].append(f"{user_id} {day}: {exc}")
            failed.add(day)  # the loader must not take a failed day for one the mapping no longer produces
            continue
        failed.discard(day)
        for bundle, name in mapper.sources.items():
            if name != bundle or bundle not in sources:  # never replace a real name with the bundle id
                sources[bundle] = name
        for gpx in mapper.binaries.values():
            path = fdir / "binaries" / f"{hashlib.sha256(gpx).hexdigest()}.gpx"
            if not path.exists():
                atomic_write(path, gpx)
        report["unmapped_samples"] += mapper.unmapped
        fman["days"][day] = {"source": fingerprint, "status": entry.get("status"), "resources": len(resources),
                             "sha256": write_ndjson(out, resources), "mapped_at": instant(datetime.now(D.zone("UTC")))}
        report["mapped"] += 1
    # Days that no longer exist (every record deleted): an empty file, so the loader deletes their resources.
    for day in sorted(set(fman["days"]) - set(days)):
        if fman["days"][day].get("resources") == 0:
            continue
        out = fdir / "days" / day[:4] / f"{day}.ndjson"
        fman["days"][day] = {"source": None, "status": "removed", "resources": 0, "sha256": write_ndjson(out, []),
                             "mapped_at": instant(datetime.now(D.zone("UTC")))}
        report["removed"] += 1
    pin_name = next((p["user_name"] for p in store.pins().values() if p.get("user_id") == user_id), user_id)
    shared = [patient(base, user_id, pin_name, latest_profile(udir))] + [device(base, user_id, b, n) for b, n in sorted(sources.items())]
    fman["shared_sha256"] = write_ndjson(fdir / "shared.ndjson", shared)
    fman["sources"] = sources
    fman["failed"] = sorted(failed & set(days))
    write_json(fdir / "manifest.json", fman)
    return report


# ------------------------------------------------------------------------------ load

class LoadError(Exception):
    pass


class RateLimited(LoadError):
    pass


QUOTA_WINDOW_S = 60  # Medplum's FHIR interaction quota resets every minute
DELETED = (200, 204, 404, 410)  # already gone counts as deleted
QUOTA_RETRIES = 10


class FhirClient:
    """OAuth2 client-credentials FHIR client (Medplum), with retries. Every failure is a LoadError."""

    def __init__(self, url: str, client_id: str, secret: str, timeout: float = 60.0, sleep=time.sleep, max_attempts: int = 5):
        self.url = url.rstrip("/")
        self.client_id, self.secret, self.timeout, self.sleep, self.max_attempts = client_id, secret, timeout, sleep, max_attempts
        self.token = ""

    def _token(self) -> str:
        form = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self.client_id,
                                       "client_secret": self.secret}).encode()
        status, body = self._send("POST", "/oauth2/token", form, "application/x-www-form-urlencoded", auth=False)
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("access_token"), str):
            raise LoadError(f"token request failed: HTTP {status}")
        return body["access_token"]

    def request(self, method: str, path: str, body=None, ok=(200, 201), raw: bytes | None = None,
                content_type: str = "application/fhir+json"):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        status, payload = self._send(method, path, data, content_type)
        if status not in ok:
            raise LoadError(f"{method} {path.split('?')[0]}: HTTP {status}: {str(payload)[:300]}")
        return payload

    def _send(self, method, path, data, content_type, auth=True):
        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            headers = {"Accept": "application/fhir+json", "User-Agent": f"hka-fhir/{MAPPING_VERSION}"}
            if data is not None:
                headers["Content-Type"] = content_type
            if auth:
                self.token = self.token or self._token()
                headers["Authorization"] = f"Bearer {self.token}"
            req = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
            wait = None
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.status, _decode(resp.read())
            except urllib.error.HTTPError as exc:
                payload = _decode(exc.read())
                if exc.code == 401 and auth and not refreshed:
                    refreshed, self.token = True, ""
                    attempt -= 1
                    continue
                if exc.code != 429 and exc.code < 500:
                    return exc.code, payload
                retry_after = exc.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else (
                    QUOTA_WINDOW_S if exc.code == 429 else None)
            except (urllib.error.URLError, OSError, ValueError):
                pass
            if attempt >= self.max_attempts:
                raise LoadError(f"{method} {path.split('?')[0]}: failed after {attempt} attempts")
            self.sleep(wait if wait is not None else min(60, 2 ** attempt))


def _decode(raw: bytes):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw[:300].decode(errors="replace")


def ident_key(r: dict) -> str:
    i = r["identifier"][0]
    return f"{r['resourceType']}|{i['system']}|{i['value']}"


def conditional_url(key: str) -> str:
    rtype, system, value = key.split("|", 2)
    return f"{rtype}?identifier={urllib.parse.quote(system, safe=':/')}|{urllib.parse.quote(value, safe=':/')}"


def transaction(resources: list[dict]) -> dict:
    return {"resourceType": "Bundle", "type": "transaction",
            "entry": [{"resource": r, "request": {"method": "PUT", "url": conditional_url(ident_key(r))}} for r in resources]}


class Loader:
    def __init__(self, store: Store, user_id: str, client: FhirClient, base: str, sleep=time.sleep):
        self.client, self.base, self.sleep = client, base, sleep
        self.fdir = store.user_dir(user_id) / "fhir"
        self.user_id = user_id
        self.path = self.fdir / "load-manifest.json"
        self.errors: list[str] = []
        self.manifest = read_json(self.path, {}) or {}
        if self.manifest.get("server") != client.url:
            self.manifest = {"server": client.url, "terminology": {}, "days": {}}

    def save(self) -> None:
        write_json(self.path, self.manifest)

    def terminology(self) -> None:
        for cs in codesystems(self.base):
            digest = sha256_hex(json.dumps(cs, sort_keys=True).encode())  # any edit reaches the server
            if self.manifest["terminology"].get(cs["url"]) == digest:
                continue
            found = self.client.request("GET", f"/fhir/R4/CodeSystem?url={urllib.parse.quote(cs['url'], safe='')}")
            entries = (found or {}).get("entry") or [] if isinstance(found, dict) else []
            if entries:
                cs["id"] = entries[0]["resource"]["id"]
                self.client.request("PUT", f"/fhir/R4/CodeSystem/{cs['id']}", cs)
            else:
                self.client.request("POST", "/fhir/R4/CodeSystem", cs)
            self.manifest["terminology"][cs["url"]] = digest
            self.save()

    def attach_binaries(self, resources: list[dict], previous: dict, uploaded: list[str]) -> dict:
        binaries = dict(previous.get("binaries") or {})
        for r in resources:
            if r["resourceType"] != "DocumentReference":
                continue
            att = r["content"][0]["attachment"]
            ext = [e["valueString"] for e in att.get("extension", []) if e["url"].endswith("/archive-path")]
            if not ext:
                continue
            key = ident_key(r)
            known = binaries.get(key)
            if known and known.get("hash") == att.get("hash"):
                att["url"] = f"Binary/{known['id']}"
                continue
            try:
                data = (self.fdir.parent / ext[0]).read_bytes()
            except OSError as exc:
                raise LoadError(f"{key}: route file missing ({exc}); run `hka fhir map --force`") from exc
            binary = self.client.request("POST", "/fhir/R4/Binary", raw=data, content_type=att["contentType"])
            if not isinstance(binary, dict) or "id" not in binary:
                raise LoadError(f"{key}: Binary upload returned no id")
            uploaded.append(binary["id"])
            att["url"] = f"Binary/{binary['id']}"
            binaries[key] = {"id": binary["id"], "hash": att.get("hash")}
        return binaries

    def load_batch(self, name: str, path: Path | None) -> bool:
        """Load one day (or `shared`); a missing path removes what the batch had loaded. True if anything changed.

        The manifest entry (new identifiers and binaries) is saved as soon as the transaction succeeds,
        with the deletions still to do; a later run finishes them if this one stops part-way."""
        data = path.read_bytes() if path is not None and path.exists() else b""
        digest = sha256_hex(data)
        try:
            self.finish_pending(name)
        except LoadError as exc:
            self.errors.append(str(exc))  # kept in the entry and retried; must not block new content
        previous = self.manifest["days"].get(name) or {}
        if path is not None and previous.get("sha256") == digest:
            if not previous.get("identifiers") and not previous.get("pending_deletes") and name != "shared":
                self.manifest["days"].pop(name, None)  # a removed day whose cleanup is complete
                self.save()
            return False
        resources = [json.loads(line) for line in data.splitlines() if line.strip()]
        if not resources and not previous:
            return False  # nothing loaded and nothing to load (a removed day after its cleanup)
        uploaded: list[str] = []
        try:
            binaries = self.attach_binaries(resources, previous, uploaded)
            if resources:
                result = self.client.request("POST", "/fhir/R4", transaction(resources))
                statuses = [e.get("response", {}).get("status", "") for e in (result or {}).get("entry", [])] \
                    if isinstance(result, dict) else []
                if any(s.startswith("429") for s in statuses):
                    raise RateLimited(f"{name}: FHIR quota exhausted")
                if len(statuses) != len(resources) or not all(s.startswith("2") for s in statuses):
                    raise LoadError(f"{self.user_id} {name}: transaction failed ({statuses[:5]})")
        except LoadError:
            for bid in uploaded:  # do not leave unreferenced uploads behind; keep the original error
                try:
                    self.client.request("DELETE", f"/fhir/R4/Binary/{bid}", ok=DELETED)
                except LoadError as exc:
                    self.errors.append(f"orphaned Binary/{bid}: {exc}")
            raise
        keys = sorted(ident_key(r) for r in resources)
        current = set(keys)
        binaries = {k: v for k, v in binaries.items() if k in current}
        pending = [f"/fhir/R4/{conditional_url(k)}" for k in sorted(set(previous.get("identifiers") or []) - current)]
        pending += [f"/fhir/R4/Binary/{old['id']}" for k, old in (previous.get("binaries") or {}).items()
                    if binaries.get(k, {}).get("id") != old["id"]]
        # Carried deletes for identifiers this batch has just written again must go: they would remove them.
        rewritten = {f"/fhir/R4/{conditional_url(k)}" for k in current}
        seen = set(pending)
        for u in previous.get("pending_deletes") or []:
            if u not in rewritten and u not in seen:
                pending.append(u)
                seen.add(u)
        self.manifest["days"][name] = {"sha256": digest, "identifiers": keys, "binaries": binaries,
                                       "pending_deletes": pending, "loaded_at": instant(datetime.now(D.zone("UTC")))}
        self.save()
        try:
            self.finish_pending(name)
        except LoadError as exc:
            self.errors.append(str(exc))  # retried on the next run
            return True
        if path is None or (not resources and name != "shared"):
            self.manifest["days"].pop(name, None)
            self.save()
        return True

    def owned_elsewhere(self, name: str) -> set[str]:
        return {f"/fhir/R4/{conditional_url(k)}" for other, e in self.manifest["days"].items() if other != name
                for k in e.get("identifiers") or []}

    def finish_pending(self, name: str) -> None:
        entry = self.manifest["days"].get(name)
        if not entry or not entry.get("pending_deletes"):
            return
        owned = self.owned_elsewhere(name)
        while entry["pending_deletes"]:
            url = entry["pending_deletes"][0]
            # An identifier that moved to another batch (a route re-filed under its workout's day, a new
            # mapping) belongs to that batch now: deleting it here would remove what it just wrote.
            if url not in owned:
                self.client.request("DELETE", url, ok=DELETED)
            entry["pending_deletes"].pop(0)
            self.save()

    def load_with_quota(self, name: str, path: Path | None) -> bool:
        for attempt in range(1, QUOTA_RETRIES + 1):
            try:
                return self.load_batch(name, path)
            except RateLimited:
                if attempt == QUOTA_RETRIES:
                    raise
                self.sleep(QUOTA_WINDOW_S)
        return False


def load_user(store: Store, user_id: str, client: FhirClient, base: str, sleep=time.sleep) -> dict:
    fdir = store.user_dir(user_id) / "fhir"
    fman = read_json(fdir / "manifest.json")
    if not fman:
        raise LoadError(f"{user_id}: run `hka fhir map` first")
    loader = Loader(store, user_id, client, base, sleep=sleep)
    loader.terminology()
    report = {"loaded": 0, "skipped": 0, "removed": 0, "errors": loader.errors}
    batches = [("shared", fdir / "shared.ndjson")] + [
        (day, fdir / "days" / day[:4] / f"{day}.ndjson") for day in sorted(fman.get("days", {}))]
    for name, path in batches:
        try:
            changed = loader.load_with_quota(name, path)
        except LoadError as exc:  # one bad batch must not hold back the others
            loader.errors.append(str(exc))
            continue
        report["loaded" if changed else "skipped"] += 1
    # Batches loaded earlier that the current mapping no longer produces (mapping version or base changed).
    keep = set(fman.get("failed") or [])  # mapping failed this time: leave what the server has
    for name in sorted(set(loader.manifest["days"]) - {n for n, _ in batches} - keep):
        errors_before = len(loader.errors)
        try:
            loader.load_with_quota(name, None)
            if len(loader.errors) == errors_before:
                report["removed"] += 1
        except LoadError as exc:
            loader.errors.append(str(exc))
    return report


def fhir_base() -> str:
    return os.environ.get("HKA_FHIR_BASE", DEFAULT_BASE).rstrip("/")
