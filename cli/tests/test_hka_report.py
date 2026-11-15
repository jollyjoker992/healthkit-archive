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
            d.update({"date": (dt.date(2026, 11, 9) + dt.timedelta(days=i)).isoformat(), "steps": 3000 + 500 * i,
                      "distance_m": 2000 + 300 * i, "active_kcal": 50 + i, "resting_hr": 57 + (i % 3),
                      "sleep_min": 360 + i, "deep_min": 70, "core_min": 220, "rem_min": 60 + i, "awake_min": 15,
                      "weight_kg": None})
            days.append(d)
        agg = R.aggregate(days, [])
        facts = {"kind": "period", "period": {"kind": "week", "value": "2026-W46"}, "from": "2026-11-09",
                 "to": "2026-11-15", "in_progress": False, "person": {"name": "Alex"},
                 "coverage": {"today_partial": None}, "summary": agg,
                 "previous_period": {"from": "2026-11-02", "to": "2026-11-08"},
                 "change_vs_previous_avg": {k: None for k in ("sleep_min", "resting_hr", "hrv_ms", "steps",
                                                              "distance_m", "active_kcal", "exercise_min")},
                 "days": days, "workouts": [], "data_notes": ["made-up data"]}
        series = {"days": days, "workouts": [], "from": dt.date(2026, 11, 9), "to": dt.date(2026, 11, 15)}
        narrative = {"language": "vi", "headline": "Tuần thử nghiệm.", "summary": "Dữ liệu giả.",
                     "evaluation": ["Một nhận xét."], "recommendations": ["Một khuyến nghị."]}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.pdf"
            R.render_period(facts, series, narrative, out)
            self.assertTrue(out.read_bytes().startswith(b"%PDF"))
            self.assertEqual(out.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
