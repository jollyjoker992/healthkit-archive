"""tools/hka_report.py: the computed facts (coverage, comparisons, splits, drift), number and date
formats, and, where matplotlib and reportlab are installed, a full render from made-up data. Offline."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("hka_report", REPO / "tools" / "hka_report.py")
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)

TZ = dt.timezone(dt.timedelta(hours=7))
T0 = dt.datetime(2026, 9, 30, 18, 20, tzinfo=TZ)


def have_pdf_deps() -> bool:
    return all(importlib.util.find_spec(m) for m in ("matplotlib", "reportlab"))


class Facts(unittest.TestCase):
    def test_compare_counts_only_days_with_data(self):
        c = R.compare(58, [55, None, 60, 57, None])
        self.assertEqual((c["avg"], c["min"], c["max"], c["diff"], c["days_lower"], c["days_with_data"]),
                         (57.3, 55, 60, 0.7, 2, 3))
        self.assertIsNone(R.compare(None, [1, 2])["diff"])

    def test_hr_summary_and_drift(self):
        pts = [(T0 + dt.timedelta(minutes=2 * i), v) for i, v in enumerate([110, 140, 150, 150, 160, 162, 164, 166])]
        h = R.hr_summary(pts)
        self.assertEqual((h["readings"], h["max"], h["first_half_avg"], h["second_half_avg"], h["drift"]),
                         (8, 166, 137.5, 163.0, 25.5))
        self.assertIsNone(R.hr_summary(pts[:4])["drift"])          # too few readings for a drift

    def test_hr_during_keeps_the_workout_minutes(self):
        pts = [(T0 + dt.timedelta(minutes=m), 100 + m) for m in range(-3, 50)]
        inside = R.hr_during(pts, T0, T0 + dt.timedelta(minutes=42))
        self.assertEqual((inside[0][0], inside[-1][0]), (T0, T0 + dt.timedelta(minutes=41)))

    def test_by_ten_minutes(self):
        pts = [(T0 + dt.timedelta(minutes=m), 100 + m) for m in (0, 5, 12, 31)]
        self.assertEqual(R.by_ten_minutes(pts, T0), [{"from_min": 0, "avg": 102, "readings": 2},
                                                     {"from_min": 10, "avg": 112, "readings": 1},
                                                     {"from_min": 30, "avg": 131, "readings": 1}])

    def test_km_splits_use_route_times_and_heart_rate(self):
        rows = [{"time": (T0 + dt.timedelta(seconds=s)).isoformat(), "distance_m": d}
                for s, d in ((0, 0), (300, 600), (480, 1000), (900, 1700), (1000, 2050))]
        hr = [(T0 + dt.timedelta(seconds=s), v) for s, v in ((60, 140), (400, 150), (700, 160))]
        splits = R.km_splits(rows, hr)
        self.assertEqual([(s["km"], s["time_s"], s["avg_hr"]) for s in splits], [(1, 480, 145.0), (2, 520, 160.0)])
        self.assertEqual(R.pace_text(splits[0]["pace_min_per_km"]), "8:00/km")

    def test_aggregate_keeps_coverage_per_metric(self):
        days = [{"date": f"2026-10-{d:02d}", **{k: None for k in R.AGGREGATED}, "weight_kg": None} for d in range(1, 11)]
        for i, day in enumerate(days):
            day["steps"] = 1000 * (i + 1)
            if i >= 7:
                day["sleep_min"] = 360 + i
        a = R.aggregate(days, [{"minutes": 30, "distance_m": 5000, "type": "running"}])
        self.assertEqual((a["steps"]["days_with_data"], a["sleep_min"]["days_with_data"]), (10, 3))
        self.assertEqual((a["steps"]["total"], a["sleep_min"]["avg"]), (55000, 368.0))
        self.assertEqual(a["workouts"], {"count": 1, "minutes": 30, "distance_km": 5.0, "by_type": {"running": 1}})

    def test_partial_today_drops_growing_totals_only(self):
        today = dt.date(2026, 11, 15)
        rec = {"date": "2026-11-15", "steps": 1200, "sleep_min": 400, "resting_hr": 58, "active_kcal": 20,
               "basal_kcal": 900, "total_kcal": 920}
        out = R.without_partial_today([rec], today)[0]
        self.assertEqual((out["steps"], out["total_kcal"], out["sleep_min"], out["resting_hr"]), (None, None, 400, 58))

    def test_periods(self):
        class A:
            week = month = start = end = None
        a = A()
        a.week = "2026-w46"
        self.assertEqual(R.period_from_args(a)[:2], (dt.date(2026, 11, 9), dt.date(2026, 11, 15)))
        a.week, a.month = None, "2026-12"
        self.assertEqual(R.period_from_args(a)[:2], (dt.date(2026, 12, 1), dt.date(2026, 12, 31)))

    def test_clock_times_wrap_at_the_sleep_day(self):
        self.assertEqual(R.clock_minutes("2026-11-14T00:15:12.000+07:00", TZ, R.EVENING), 375)   # 6h15 after 18:00
        self.assertEqual(R.clock_minutes("2026-11-13T23:18:00+07:00", TZ, R.EVENING), 318)
        self.assertEqual(R.clock_minutes("2026-11-13T22:18:00Z", TZ), 318)                        # 05:18 local
        self.assertEqual((R.clock_text(375, R.EVENING), R.clock_text(318)), ("00:15", "05:18"))

    def test_headphone_dose_and_energy_mean(self):
        self.assertAlmostEqual(R.headphone_dose_pct(40 * 3600, 80), 100)
        self.assertAlmostEqual(R.headphone_dose_pct(4 * 3600, 90), 100.8, places=1)  # 10 dB louder: about a tenth of the time
        self.assertAlmostEqual(R.energy_mean_db([(60, 70), (60, 70)]), 70)
        self.assertAlmostEqual(R.energy_mean_db([(60, 60), (60, 80)]), 77.0, places=1)  # the loud half dominates
        self.assertIsNone(R.energy_mean_db([]))

    def night(self, date: str, start: str, end: str, asleep: float, **extra) -> dict:
        rec = {k: None for k in R.AGGREGATED}
        rec.update({"date": date, "sleep_start": start, "sleep_end": end, "sleep_min": asleep,
                    "bedtime_min": R.clock_minutes(start, TZ, R.EVENING), "wake_min": R.clock_minutes(end, TZ),
                    "sleep_efficiency_pct": 95.0, "sleep_hr_avg": None, "vo2_max": None, "headphone_min": None,
                    "headphone_laeq_db": None, "headphone_dose_pct": None, **{k: None for k in R.MOBILITY}})
        rec.update(extra)
        return rec

    def test_wellness_sleep_timing_activity_and_listening(self):
        recs = [self.night("2026-11-12", "2026-11-11T23:00:00+07:00", "2026-11-12T06:00:00+07:00", 400, sleep_hr_avg=60,
                           walking_speed_kmh=3.8),
                self.night("2026-11-13", "2026-11-12T23:30:00+07:00", "2026-11-13T06:30:00+07:00", 430, sleep_hr_avg=62,
                           headphone_min=60, headphone_laeq_db=70, headphone_dose_pct=0.1),
                self.night("2026-11-14", "2026-11-14T01:00:00+07:00", "2026-11-14T08:00:00+07:00", 410),   # Saturday
                self.night("2026-11-15", "2026-11-15T00:30:00+07:00", "2026-11-15T07:30:00+07:00", 380,    # Sunday
                           steadiness_pct=90.0)]
        w = R.wellness(recs, [{"minutes": 35}])
        t = w["sleep_timing"]
        self.assertEqual((t["bedtime_avg"], t["wake_avg"], t["latest_bedtime"]), ("00:00", "07:00", "01:00"))
        self.assertEqual((t["nights_starting_after_midnight"], t["nights_under_7h"]), (2, 3))
        self.assertEqual(t["weekend_minus_weekday_midpoint_min"], 90)  # 04:15 vs 02:45
        self.assertEqual(w["heart_rate_asleep"], {"nights": 2, "avg": 61.0, "lowest_night": 60, "highest_night": 62})
        self.assertEqual(w["activity_guideline"], {"minutes": 35, "source": "workouts", "per_week": 61,
                                                   "who_target_per_week": 150})
        self.assertEqual(w["walking"]["walking_speed_kmh"], {"avg": 3.8, "days_with_data": 1})
        self.assertEqual(w["walking"]["steadiness_pct_last"], {"date": "2026-11-15", "value": 90.0})
        self.assertEqual((w["headphones"]["minutes"], w["headphones"]["laeq_db"], w["headphones"]["weekly_dose_pct"]),
                         (60, 70.0, 0.2))
        earlier = R.wellness([self.night("2026-11-05", "2026-11-04T23:00:00+07:00", "2026-11-05T06:00:00+07:00", 400)], [])
        ch = R.wellness_change(w, earlier)
        self.assertEqual((ch["bedtime_min"], ch["wake_min"], ch["heart_rate_asleep"]), (60, 60, None))

    def test_exercise_minutes_win_when_they_cover_the_period(self):
        recs = [self.night(f"2026-11-1{i}", "2026-11-10T23:00:00+07:00", "2026-11-11T06:00:00+07:00", 400, exercise_min=20)
                for i in range(4)]
        act = R.wellness(recs, [{"minutes": 90}])["activity_guideline"]
        self.assertEqual((act["source"], act["minutes"], act["per_week"]), ("exercise_minutes", 80, 140))

    def test_audiogram_text_marks_values_at_the_test_limit(self):
        a = {"date": "2025-09-26 14:53", "left": {"pta_db_hl": 5.0, "values_at_test_limit": 4},
             "right": {"pta_db_hl": None, "values_at_test_limit": 0}}
        self.assertEqual(R.audiogram_text(a, R.Labels("en")), "left <= 5 / right - dB HL (2025-09-26)")

    def test_audiogram_thresholds_come_from_the_loinc_displays(self):
        comp = lambda ear, hz, v, **kw: {"code": {"coding": [
            {"system": "http://loinc.org", "code": "x", "display": f"Hearing threshold Ear - {ear} --{hz} Hz"}]},
            "valueQuantity": {"value": v, **({"comparator": kw["cmp"]} if "cmp" in kw else {})},
            **({"extension": [{"url": "b/StructureDefinition/audiogram-masked", "valueBoolean": kw["masked"]}]}
               if "masked" in kw else {})}
        obs = {"component": [comp("right", 1000, 18.09, masked=False), comp("left", 1000, 13.3, cmp="<=", masked=False),
                             comp("left", 250, 3.7, cmp="<="), comp("right", 250, 7.7, masked=True),
                             {"code": {"coding": [{"system": "http://loinc.org", "code": "y", "display": "Something else"}]},
                              "valueQuantity": {"value": 1}}]}
        pts = R.threshold_points(obs)
        self.assertEqual([(p["ear"], p["hz"], p["comparator"], p["masked"]) for p in pts],
                         [("left", 250, "<=", None), ("left", 1000, "<=", False), ("right", 250, None, True),
                          ("right", 1000, None, False)])
        rows = R.audiogram_rows({"points": pts}, R.Labels("vi"))
        self.assertEqual(rows[1:], [["250 Hz", "≤ 3,7", "7,7 (M)"], ["1000 Hz", "≤ 13,3", "18,1"]])

    def test_components_are_keyed_by_their_own_code(self):
        obs = {"component": [{"code": {"coding": [{"system": "http://loinc.org", "code": "93831-6"},
                                                  {"system": "x/CodeSystem/healthkit-type", "code": "HKSleep"}]},
                              "valueQuantity": {"value": 78}}]}
        self.assertEqual(R.comps(obs), {"93831-6": 78})


class Formats(unittest.TestCase):
    def test_vietnamese_numbers_dates_and_durations(self):
        L = R.Labels("vi")
        self.assertEqual(L.n(3032.3), "3.032,3")
        self.assertEqual(L.whole(3032.3), "3.032")
        self.assertEqual(L.km(2578), "2,58 km")
        self.assertEqual(L.hm(375.4), "6h15")
        self.assertEqual(L.hm(59.6), "1h00")
        self.assertEqual(L.date(dt.date(2026, 11, 13), "long_day"), "Thứ Sáu, 13/11/2026")
        self.assertEqual(L.value("type", "running"), "Chạy bộ")
        self.assertEqual(L.value("type", "dance"), "Dance")
        self.assertEqual(L.fallback, [])

    def test_english_and_missing_values(self):
        L = R.Labels("en")
        self.assertEqual((L.n(None), L.hm(None), L.km(None)), ("-", "-", "-"))
        self.assertEqual(L.hm(360), "6h 00m")
        self.assertEqual(R.dur_text(42.15 * 60), "42:09")

    def test_every_built_in_language_has_every_label(self):
        missing = {lang: sorted(set(R.LABELS_EN) - set(t)) for lang, t in R.BUILT_IN_LABELS.items()}
        self.assertEqual(missing, {"en": [], "vi": []})

    def test_unknown_language_reports_untranslated_labels(self):
        self.assertIn("summary", R.Labels("de").fallback)


@unittest.skipUnless(have_pdf_deps(), "matplotlib and reportlab are not installed (tools/requirements-reports.txt)")
class Render(unittest.TestCase):
    def test_period_report_renders_from_made_up_data(self):
        days = []
        for i in range(7):
            d = {k: None for k in R.AGGREGATED}
            date = dt.date(2026, 11, 9) + dt.timedelta(days=i)
            start, end = f"{date - dt.timedelta(days=1)}T23:{5 * i:02d}:00+07:00", f"{date}T06:{5 * i:02d}:00+07:00"
            d.update({"date": date.isoformat(), "steps": 3000 + 500 * i,
                      "distance_m": 2000 + 300 * i, "active_kcal": 50 + i, "resting_hr": 57 + (i % 3),
                      "sleep_min": 360 + i, "deep_min": 70, "core_min": 220, "rem_min": 60 + i, "awake_min": 15,
                      "weight_kg": None, "vo2_max": None, "sleep_start": start, "sleep_end": end,
                      "bedtime_min": R.clock_minutes(start, TZ, R.EVENING), "wake_min": R.clock_minutes(end, TZ),
                      "sleep_efficiency_pct": 96.0, "sleep_hr_avg": 62 + i % 2, "walking_speed_kmh": 3.8,
                      "step_length_cm": 60.0, "double_support_pct": 28.5, "asymmetry_pct": 4.0, "steadiness_pct": None,
                      "headphone_min": 30 if i % 2 else None, "headphone_laeq_db": 64.0 if i % 2 else None,
                      "headphone_dose_pct": 0.05 if i % 2 else None})
            days.append(d)
        agg = R.aggregate(days, [])
        well = R.wellness(days, [])
        facts = {"kind": "period", "period": {"kind": "week", "value": "2026-W46"}, "from": "2026-11-09",
                 "to": "2026-11-15", "in_progress": False, "person": {"name": "Alex"},
                 "coverage": {"today_partial": None}, "summary": agg,
                 "previous_period": {"from": "2026-11-02", "to": "2026-11-08"},
                 "change_vs_previous_avg": {k: None for k in ("sleep_min", "resting_hr", "hrv_ms", "steps",
                                                              "distance_m", "active_kcal", "exercise_min")},
                 "days": days, "workouts": [], "data_notes": ["made-up data"],
                 "wellness": {"this": well, "previous": well, "change": R.wellness_change(well, well)},
                 "audiograms": [{"date": "2026-11-12 09:00", "source": "Health",
                                 "left": {"pta_db_hl": 5.0, "values_at_test_limit": 4},
                                 "right": {"pta_db_hl": 7.5, "values_at_test_limit": 2},
                                 "points": [{"ear": ear, "hz": hz, "db_hl": db, "comparator": "<=" if db < 5 else None,
                                             "masked": ear == "right" and hz == 4000}
                                            for ear in ("left", "right") for hz, db in ((500, 0), (1000, 5), (4000, 25))]}]}
        series = {"days": days, "workouts": [], "from": dt.date(2026, 11, 9), "to": dt.date(2026, 11, 15),
                  "audiograms": facts["audiograms"]}
        narrative = {"language": "vi", "headline": "Tuần thử nghiệm.", "summary": "Dữ liệu giả.",
                     "evaluation": ["Một nhận xét."], "recommendations": ["Một khuyến nghị."]}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.pdf"
            R.render_period(facts, series, narrative, out)
            self.assertTrue(out.read_bytes().startswith(b"%PDF"))
            charts = R.period_charts(series, Path(tmp), R.Labels("vi"), R.font_files(None, None))
            self.assertIn(R.Labels("vi")("c_audiogram_caption"), [c for c, _ in charts])
            self.assertEqual(out.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
