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
from typing import NamedTuple
from xml.sax.saxutils import escape

from hkacommon.encoding import is_instant, parse_instant, sha256_hex
from . import days as D
from .build import read_lines
from .store import Store, atomic_write, instant, read_json, write_json

MAPPING_VERSION = "0.6.0"
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
    # Audiometry: checked against the NLM API and tx.fhir.org on 2026-11-16 (all ACTIVE, example unit dB).
    "89015-2": ("Pure tone air conduction threshold audiometry panel", None),
}

# Hearing threshold per (frequency in Hz, ear) -> LOINC; the panel 89015-2 says the test is air conduction.
HEARING_THRESHOLD = {
    (250, "left"): "91375-6", (250, "right"): "91374-9",
    (500, "left"): "89024-4", (500, "right"): "89025-1",
    (750, "left"): "91379-8", (750, "right"): "91378-0",
    (1000, "left"): "89016-0", (1000, "right"): "89017-8",
    (1500, "left"): "91373-1", (1500, "right"): "91372-3",
    (2000, "left"): "89018-6", (2000, "right"): "89019-4",
    (3000, "left"): "89020-2", (3000, "right"): "89021-0",
    (4000, "left"): "89022-8", (4000, "right"): "89023-6",
    (5000, "left"): "91377-2", (5000, "right"): "91376-4",
    (6000, "left"): "89026-9", (6000, "right"): "89027-7",
    (8000, "left"): "89028-5", (8000, "right"): "89029-3",
}
LOINC.update({code: (f"Hearing threshold Ear - {side} --{hz} Hz", "dB") for (hz, side), code in HEARING_THRESHOLD.items()})
AUDIOGRAM = "HKDataTypeIdentifierAudiogram"

class Metric(NamedTuple):
    group: str
    display: str
    unit: str | None          # UCUM unit; None for a text value
    value_type: str           # FHIR value type: Quantity, SampledData or CodeableConcept (text only)
    source: str               # where the value comes from
    definition: str


# Groups of the hka-metrics CodeSystem (not selectable, they only organise the codes).
METRIC_GROUPS = {
    "daily-activity": ("Daily activity", "Day totals of cumulative HealthKit quantities that have no matching LOINC code."),
    "heart": ("Heart", "Heart-rate values that no LOINC code describes correctly."),
    "sleep": ("Sleep", "Sleep stages that LOINC has no code for."),
    "workout": ("Workout", "Workout figures without a LOINC code."),
    "ecg": ("Electrocardiogram", "Apple Watch single-lead ECG recordings."),
    "fitness": ("Fitness", "Cardio-fitness values without a correct LOINC code."),
}

# Values without a correct LOINC code. Published as fhir/CodeSystem-hka-metrics.json; changing this table
# changes MAPPING_VERSION and the files (python3 tools/fhir_terminology.py).
METRICS = {
    "active-energy-daily": Metric(
        "daily-activity", "Active energy burned, day total", "kcal", "Quantity",
        "summary.json totals.HKQuantityTypeIdentifierActiveEnergyBurned",
        "Energy burned above resting metabolism during the local calendar day: the sum of "
        "HKQuantityTypeIdentifierActiveEnergyBurned samples from one source (the source with the largest total, "
        "or the configured priority). Estimated by the device. LOINC 41979-6 is total calories, not active."),
    "basal-energy-daily": Metric(
        "daily-activity", "Basal energy burned, day total", "kcal", "Quantity",
        "summary.json totals.HKQuantityTypeIdentifierBasalEnergyBurned",
        "Resting energy for the local calendar day: the sum of HKQuantityTypeIdentifierBasalEnergyBurned from one "
        "source. An estimate from body measurements, not a measured metabolic rate (so not LOINC 69429-9)."),
    "exercise-minutes-daily": Metric(
        "daily-activity", "Exercise minutes, day total", "min", "Quantity",
        "summary.json totals.HKQuantityTypeIdentifierAppleExerciseTime",
        "Apple's Exercise ring minutes for the local calendar day: minutes of movement at or above a brisk walk, as "
        "judged by Apple Watch. Not split into moderate and vigorous, so LOINC 77592-4 / 77593-2 do not apply."),
    "distance-cycling-daily": Metric(
        "daily-activity", "Cycling distance, day total", "m", "Quantity",
        "summary.json totals.HKQuantityTypeIdentifierDistanceCycling",
        "Distance cycled during the local calendar day, from one source."),
    "hr-minute-mean": Metric(
        "heart", "Heart rate, 1-minute means", "/min", "SampledData",
        "HKQuantityTypeIdentifierHeartRate.ndjson",
        "Mean of all heart-rate samples (every source) that start in each minute of the local day; one slot per "
        "minute (1440, or 1380/1500 on daylight-saving days), E where there is no sample. Not coded LOINC 8867-4: "
        "the vital-signs heart-rate profile allows only a Quantity value."),
    "sleep-in-bed": Metric(
        "sleep", "Time in bed", "min", "Quantity", "summary.json sleep.stages_min.inBed",
        "Minutes recorded as HKCategoryValueSleepAnalysis.inBed for the night ending on this day, from the chosen "
        "sleep source. In bed is not asleep; it can overlap the stage minutes."),
    "sleep-unspecified": Metric(
        "sleep", "Asleep, stage not specified", "min", "Quantity", "summary.json sleep.stages_min.asleepUnspecified",
        "Minutes recorded as HKCategoryValueSleepAnalysis.asleepUnspecified (asleep, with no stage) for the night "
        "ending on this day. Sources without stage tracking (older watches, phones, many third-party apps) write only this."),
    "workout-distance": Metric(
        "workout", "Workout distance", "m", "Quantity", "HKWorkoutTypeIdentifier stats.HKQuantityTypeIdentifierDistance*",
        "Total distance of the workout: the largest of its distance statistics (walking/running, cycling, swimming, "
        "and so on), converted to metres."),
    "ecg-lead-i": Metric(
        "ecg", "ECG voltage, lead I (Apple Watch)", "uV", "SampledData", "HKDataTypeIdentifierElectrocardiogram voltages_uv",
        "Voltage samples of an Apple Watch ECG, similar to limb lead I, in microvolts at the recorded sampling "
        "frequency (period = 1000 / Hz ms). E marks a missing sample."),
    "ecg-classification": Metric(
        "ecg", "ECG classification", None, "CodeableConcept", "HKDataTypeIdentifierElectrocardiogram classification",
        "Apple's classification of the recording, as the HKElectrocardiogram.Classification case name "
        "(sinusRhythm, atrialFibrillation, inconclusiveLowHeartRate, ...). A screening result, not a diagnosis."),
    "vo2-max": Metric(
        "fitness", "VO2 max", "mL/(kg.min)", "Quantity", "HKQuantityTypeIdentifierVO2Max",
        "Maximal oxygen uptake as estimated by the device (Apple Watch cardio fitness). LOINC 60842-2 is VO2 "
        "(oxygen consumption), not VO2 max."),
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

# Daily totals (summary.json): HealthKit type, LOINC codes, hka-metrics code, UCUM unit, category.
DAILY_PLAN = [
    ("HKQuantityTypeIdentifierStepCount", ["41950-7"], None, "/d", "activity"),
    ("HKQuantityTypeIdentifierDistanceWalkingRunning", ["41953-1"], None, "m", "activity"),
    ("HKQuantityTypeIdentifierFlightsClimbed", ["93833-2"], None, "{#}", "activity"),
    ("HKQuantityTypeIdentifierActiveEnergyBurned", [], "active-energy-daily", "kcal", "activity"),
    ("HKQuantityTypeIdentifierBasalEnergyBurned", [], "basal-energy-daily", "kcal", "activity"),
    ("HKQuantityTypeIdentifierAppleExerciseTime", [], "exercise-minutes-daily", "min", "activity"),
    ("HKQuantityTypeIdentifierDistanceCycling", [], "distance-cycling-daily", "m", "activity"),
]

# Quantity types aggregated per day instead of one Observation per sample.
DAILY_ONLY = set(D.CUMULATIVE) | {D.HEART_RATE}

BP_MEMBERS = {"HKQuantityTypeIdentifierBloodPressureSystolic", "HKQuantityTypeIdentifierBloodPressureDiastolic"}

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
            codings.append({"system": f"{self.base}/CodeSystem/hka-metrics", "code": metric, "display": METRICS[metric].display})
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
        self.audiograms()
        keys =[f"{r['resourceType']}|{r['identifier'][0]['value']}" for r in self.resources]
        if len(keys) != len(set(keys)):
            raise MappingError("two resources with the same identifier")
        return self.resources

    def key(self, name: str) -> str:
        return f"{self.user_id}/{self.day}/{name}"  # daily resources: unique per user

    def daily_totals(self) -> None:
        totals = self.summary.get("totals") or {}
        for hk, loinc, metric, unit, category in DAILY_PLAN:
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

    def audiograms(self) -> None:
        """One Observation per audiogram: the air-conduction panel with a threshold per ear and frequency."""
        for r in self.files.get(f"{AUDIOGRAM}.ndjson", []):
            points = (r.get("data") or {}).get("sensitivity_points") if isinstance(r.get("data"), dict) else None
            if r.get("k") != "object" or not isinstance(r.get("uuid"), str) or not isinstance(points, list):
                continue
            try:
                effective = self.period(r)
            except MappingError:
                self.unmapped += 1
                continue
            comps = []
            for p in sorted((p for p in points if isinstance(p, dict)), key=lambda p: p.get("frequency_hz") or 0):
                for side in ("left", "right"):
                    comp = self.hearing_threshold(p, side)
                    if comp:
                        comps.append(comp)
            if comps:
                self.observation(r["uuid"], self.code(["89015-2"], AUDIOGRAM), effective, category="procedure",
                                 components=comps, device=self.device(r))
            else:
                self.unmapped += 1

    def hearing_threshold(self, point: dict, side: str) -> dict | None:
        """The component for one ear at one frequency, or None when there is nothing correct to say.

        With per-test data (iOS 18.1+) only an air-conduction test counts (the panel is air conduction); when a
        frequency has a masked and an unmasked test, the masked threshold is the valid one. Without tests, the
        point's `<side>_db_hl` is used (HealthKit does not say the conduction; see the mapping document). A value
        at the edge of the test's clamping range is a bound, not a measurement: comparator <= or >=.
        """
        hz = point.get("frequency_hz")
        tests = point.get("tests")
        if isinstance(tests, list) and tests:
            air = [t for t in tests if isinstance(t, dict) and t.get("side") == side and t.get("conduction") == "air"
                   and isinstance(t.get("sensitivity_db_hl"), (int, float))]
            others = [t for t in tests if isinstance(t, dict) and t.get("side") == side and t not in air]
            self.unmapped += len(others)
            if not air:
                return None
            test = max(air, key=lambda t: t.get("masked") is True)
            value, masked, clamp = test["sensitivity_db_hl"], test.get("masked") is True, test.get("clamping")
        else:
            value, masked, clamp = point.get(f"{side}_db_hl"), None, None
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return None
        code = HEARING_THRESHOLD.get((hz, side))
        if code is None:
            self.unmapped += 1  # no LOINC code for this frequency; kept in the day folder
            return None
        q = {"value": round(value, 2), "unit": "dB HL", "system": UCUM, "code": "dB"}
        clamp = clamp if isinstance(clamp, dict) else {}
        lower, upper = clamp.get("lower_db_hl"), clamp.get("upper_db_hl")
        if isinstance(lower, (int, float)) and value <= lower:
            q["comparator"] = "<="  # the test could not go quieter: the real threshold is at or below this
        elif isinstance(upper, (int, float)) and value >= upper:
            q["comparator"] = ">="  # the test could not go louder: the real threshold is at or above this
        comp = {"code": self.code([code]), "valueQuantity": q}
        if masked is not None:
            comp["extension"] = [{"url": f"{self.base}/StructureDefinition/audiogram-masked", "valueBoolean": masked}]
        return comp


def fmt(x: float, digits: int) -> str:
    """Fixed-point decimal without trailing zeros (never exponent notation)."""
    text = f"{x:.{digits}f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text



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

# The published terminology: generated from the tables above by tools/fhir_terminology.py, committed, shipped
# with the package and loaded by `hka fhir load`. CI checks the files match the code (`--check`).
TERMINOLOGY_DIR = Path(__file__).resolve().parent / "terminology"
TERMINOLOGY_FILES = ("CodeSystem-hka-metrics.json", "ValueSet-hka-metrics.json", "CodeSystem-healthkit-type.json")

# Display names where splitting the identifier would read badly.
HK_DISPLAY = {
    "HKQuantityTypeIdentifierVO2Max": "VO2 max",
    "HKQuantityTypeIdentifierDistanceWalkingRunning": "Walking and running distance",
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": "Heart rate variability (SDNN)",
    "HKQuantityTypeIdentifierUVExposure": "UV exposure",
    "HKQuantityTypeIdentifierForcedExpiratoryVolume1": "Forced expiratory volume in 1 second",
    "HKQuantityTypeIdentifierNikeFuel": "NikeFuel",
    "HKQuantityTypeIdentifierHeartRateRecoveryOneMinute": "Heart rate recovery, one minute",
    "HKCorrelationTypeIdentifierBloodPressure": "Blood pressure (correlation)",
    "HKCorrelationTypeIdentifierFood": "Food (correlation)",
    "HKWorkoutTypeIdentifier": "Workout",
    "HKDataTypeIdentifierElectrocardiogram": "Electrocardiogram",
    "HKDataTypeIdentifierHeartbeatSeries": "Heartbeat series",
    "HKDataTypeIdentifierAudiogram": "Audiogram",
    "HKDataTypeStateOfMind": "State of mind",
    "HKScoredAssessmentTypeIdentifierGAD7": "GAD-7 anxiety assessment",
    "HKScoredAssessmentTypeIdentifierPHQ9": "PHQ-9 depression assessment",
    "HKQuantityTypeIdentifierHeartRateVariabilityRMSSD": "Heart rate variability (RMSSD)",
}
# Sent as `object` records (spec/records.md); kept in the day folders, not mapped (audiograms are, see Mapper).
OBJECT_TYPES = ("HKDataTypeStateOfMind", "HKScoredAssessmentTypeIdentifierGAD7", "HKScoredAssessmentTypeIdentifierPHQ9")
HK_PREFIXES = (("HKQuantityTypeIdentifier", "quantity"), ("HKCategoryTypeIdentifier", "category"),
               ("HKCorrelationTypeIdentifier", "correlation"), ("HKDataTypeIdentifier", "data"),
               ("HKDataType", "data"), ("HKScoredAssessmentTypeIdentifier", "data"),
               ("HKWorkoutTypeIdentifier", "workout"))


def hk_kind(t: str) -> str:
    return next(kind for prefix, kind in HK_PREFIXES if t.startswith(prefix))


def hk_display(t: str) -> str:
    if t in HK_DISPLAY:
        return HK_DISPLAY[t]
    name = next(t[len(prefix):] for prefix, _ in HK_PREFIXES if t.startswith(prefix))
    if name.startswith("Apple"):
        name = name[5:]
    words, word = [], ""
    for i, ch in enumerate(name):  # "DistanceWalkingRunning" -> "Distance walking running", "UVExposure" kept as UV
        if ch.isupper() and word and not (word[-1].isupper() and (i + 1 == len(name) or name[i + 1].isupper())):
            words.append(word)
            word = ""
        word += ch
    words.append(word)
    text = " ".join(w if w.isupper() and len(w) > 1 else w.lower() for w in words)
    return text[0].upper() + text[1:]


def hk_mapping(t: str) -> str:
    """How `hka fhir map` represents a HealthKit type (shown by CodeSystem/$lookup)."""
    loinc_txt = lambda codes: " + ".join(f"LOINC {c}" for c in codes)
    for hk, loinc, metric, unit, _ in DAILY_PLAN:
        if hk == t:
            return f"Daily total (one source) as one Observation per day: {loinc_txt(loinc) or 'hka-metrics ' + metric} in {unit}."
    if t == D.HEART_RATE:
        return ("Per day: hka-metrics hr-minute-mean (valueSampledData, 1-minute means), and LOINC 8883-1 / 8873-2 "
                "for the day minimum and maximum. Individual samples stay in the day folder.")
    if t in SAMPLE_LOINC:
        category, loinc, unit = SAMPLE_LOINC[t]
        return f"One Observation per sample: {loinc_txt(loinc)} in {unit}, category {category}."
    if t == "HKQuantityTypeIdentifierVO2Max":
        return "One Observation per sample: hka-metrics vo2-max in mL/(kg.min)."
    if t in BP_MEMBERS:
        return "Not mapped on its own: a component (LOINC 8480-6 / 8462-4) of the blood-pressure Observation."
    if t == D.SLEEP:
        return ("One Observation per night (ending on the day): LOINC 93832-4 asleep minutes, with stage components "
                "93831-6 deep, 93830-8 core, 93829-0 REM, 93828-2 awake, and hka-metrics sleep-in-bed / sleep-unspecified.")
    if t == "HKCorrelationTypeIdentifierBloodPressure":
        return "One Observation per reading: LOINC 85354-9 panel with 8480-6 systolic and 8462-4 diastolic components."
    if t == D.WORKOUT:
        return ("One Observation per workout: LOINC 55411-3 duration in min, components 73985-4 activity (text), "
                "55425-3 mean HR, 55426-1 max HR, 55424-6 energy, hka-metrics workout-distance; the route as a GPX "
                "DocumentReference in derivedFrom.")
    if t == "HKDataTypeIdentifierElectrocardiogram":
        return ("One Observation per recording, category procedure: hka-metrics ecg-classification (text), "
                "ecg-lead-i (valueSampledData, uV) and LOINC 8867-4 average heart rate.")
    if t == AUDIOGRAM:
        return ("One Observation per audiogram, category procedure: LOINC 89015-2 air-conduction panel with one "
                "hearing-threshold component per ear and frequency (250-8000 Hz) in dB HL; comparator <= or >= where "
                "the value is at the edge of the test's range.")
    if t in ("HKCorrelationTypeIdentifierFood", "HKDataTypeIdentifierHeartbeatSeries") + OBJECT_TYPES:
        return "Not mapped to FHIR; kept in the day folder."
    if hk_kind(t) == "quantity":
        return "One Observation per sample, coded with this HealthKit type only, value in the UCUM unit of the sample."
    return "One Observation per sample, coded with this HealthKit type only, valueCodeableConcept.text = the HealthKit value name."


def build_terminology(catalog: list[str], base: str = DEFAULT_BASE) -> list[dict]:
    """CodeSystem and ValueSet for hka-metrics, and the healthkit-type CodeSystem for `catalog` (the app's types)."""
    concepts = []
    for group, (display, definition) in METRIC_GROUPS.items():
        members = [{"code": code, "display": m.display, "definition": m.definition, "property":
                    ([{"code": "unit", "valueString": m.unit}] if m.unit else [])
                    + [{"code": "valueType", "valueString": m.value_type}, {"code": "source", "valueString": m.source}]}
                   for code, m in METRICS.items() if m.group == group]
        if members:
            concepts.append({"code": group, "display": display, "definition": definition,
                             "property": [{"code": "notSelectable", "valueBoolean": True}], "concept": members})
    metrics = {
        "resourceType": "CodeSystem", "id": "hka-metrics", "url": f"{base}/CodeSystem/hka-metrics",
        "version": MAPPING_VERSION, "name": "HkaMetrics", "title": "HealthKit values without a standard code",
        "status": "draft", "experimental": False, "publisher": "healthkit-archive",
        "description": "Codes for Apple HealthKit values that have no correct LOINC code. Values that do have one "
                       "(steps, heart rate, sleep durations, SpO2, weight and others) use LOINC; see docs/fhir-mapping.md. "
                       "Each concept states its unit, value type and where the value comes from. Group concepts are "
                       "not selectable and only organise the codes.",
        "caseSensitive": True, "hierarchyMeaning": "grouped-by", "content": "complete",
        "property": [
            {"code": "notSelectable", "uri": "http://hl7.org/fhir/concept-properties#notSelectable", "type": "boolean",
             "description": "Group concept; never used as an Observation code."},
            {"code": "unit", "type": "string", "description": "UCUM unit the value is recorded in. Absent for text values."},
            {"code": "valueType", "type": "string", "description": "FHIR value type: Quantity, SampledData or CodeableConcept (text only)."},
            {"code": "source", "type": "string", "description": "Where the value comes from: day-folder file or HealthKit field."},
        ],
        "count": len(METRICS), "concept": concepts,
    }
    valueset = {
        "resourceType": "ValueSet", "id": "hka-metrics", "url": f"{base}/ValueSet/hka-metrics",
        "version": MAPPING_VERSION, "name": "HkaMetrics", "title": "All selectable hka-metrics codes", "status": "draft",
        "description": "Every selectable code in the hka-metrics CodeSystem. Group concepts are excluded.",
        "compose": {"include": [{"system": f"{base}/CodeSystem/hka-metrics",
                                 "concept": [{"code": c, "display": m.display} for c, m in METRICS.items()]}]},
    }
    types = {
        "resourceType": "CodeSystem", "id": "healthkit-type", "url": f"{base}/CodeSystem/healthkit-type",
        "version": MAPPING_VERSION, "name": "HealthKitType", "title": "Apple HealthKit type identifiers",
        "status": "draft", "experimental": False, "publisher": "healthkit-archive",
        "description": "The HealthKit type identifiers the healthkit-archive app reads. Every Observation carries its "
                       "HealthKit type as a coding in this system, so it can be traced back to the source type. The "
                       "`kind` property gives the HealthKit class and `mapping` how the type appears in FHIR. The codes "
                       "are Apple's identifier strings; their meaning is defined by Apple's HealthKit documentation.",
        "caseSensitive": True, "content": "complete",
        "property": [
            {"code": "kind", "type": "string", "description": "quantity, category, correlation, workout or data (series and other objects)."},
            {"code": "mapping", "type": "string", "description": "How hka fhir map represents values of this type."},
        ],
        "count": len(catalog),
        "concept": [{"code": t, "display": hk_display(t), "property": [
            {"code": "kind", "valueString": hk_kind(t)}, {"code": "mapping", "valueString": hk_mapping(t)}]}
            for t in catalog],
    }
    return [metrics, valueset, types]


def terminology(base: str) -> list[dict]:
    """The committed terminology files, with their URLs moved to `base` when it is not the default."""
    out = []
    for name in TERMINOLOGY_FILES:
        text = (TERMINOLOGY_DIR / name).read_text()
        if base != DEFAULT_BASE:
            text = text.replace(json.dumps(DEFAULT_BASE)[1:-1], json.dumps(base)[1:-1])
        out.append(json.loads(text))
    return out


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
        for res in terminology(self.base):
            digest = sha256_hex(json.dumps(res, sort_keys=True).encode())  # any edit reaches the server
            if self.manifest["terminology"].get(res["url"]) == digest:
                continue
            rtype = res["resourceType"]
            res.pop("id", None)  # the server assigns ids; the file's id is for readers and the validator
            found = self.client.request("GET", f"/fhir/R4/{rtype}?url={urllib.parse.quote(res['url'], safe='')}")
            entries = (found or {}).get("entry") or [] if isinstance(found, dict) else []
            if entries:
                res["id"] = entries[0]["resource"]["id"]
                self.client.request("PUT", f"/fhir/R4/{rtype}/{res['id']}", res)
            else:
                self.client.request("POST", f"/fhir/R4/{rtype}", res)
            self.manifest["terminology"][res["url"]] = digest
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
