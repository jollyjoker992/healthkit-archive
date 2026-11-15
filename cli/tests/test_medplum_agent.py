"""tools/medplum_agent.py: route maths, unusable route times, workout heart rate and person selection,
offline (Medplum calls are replaced)."""
from __future__ import annotations

import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("medplum_agent", REPO / "tools" / "medplum_agent.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

UUID = "00000000-0000-4000-8000-000000000701"
WORKOUT = {"subject": {"reference": "Patient/p1"},
           "effectivePeriod": {"start": "2026-11-12T23:58:00.000+07:00", "end": "2026-11-13T00:03:00.000+07:00"},
           "component": []}


def gpx(points: list[tuple[str, float, float, float]]) -> str:
    body = "".join(f'<trkpt lat="{lat}" lon="{lon}"><ele>{ele}</ele><time>{t}</time></trkpt>'
                   for t, lat, lon, ele in points)
    return ('<?xml version="1.0" encoding="UTF-8"?><gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">'
            f"<trk><trkseg>{body}</trkseg></trk></gpx>")


class Agent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (M.CACHE_DIR, M.workout, M.graphql, M.people)
        M.CACHE_DIR = Path(self.tmp.name)
        M.workout = lambda uuid: WORKOUT

    def tearDown(self):
        M.CACHE_DIR, M.workout, M.graphql, M.people = self.saved
        self.tmp.cleanup()

    def write_route(self, points):
        path = M.CACHE_DIR / "routes" / f"{UUID}.gpx"
        path.parent.mkdir(parents=True)
        path.write_text(gpx(points))

    def test_distance_speed_and_pace_from_points(self):
        # 0.001 degrees of latitude is about 111.2 m; 40 s apart gives about 2.78 m/s (6:00 min/km).
        self.write_route([("2026-11-12T16:58:00Z", 10.0, 106.0, 5), ("2026-11-12T16:58:40Z", 10.001, 106.0, 6),
                          ("2026-11-12T16:59:20Z", 10.002, 106.0, 7)])
        rows = M.route_rows(UUID, gps=False)
        self.assertEqual([r["elapsed_s"] for r in rows], [0, 40, 80])
        self.assertAlmostEqual(rows[-1]["distance_m"], 222.4, delta=0.5)
        self.assertAlmostEqual(rows[1]["speed_mps"], 2.78, delta=0.01)
        self.assertAlmostEqual(rows[1]["pace_min_per_km"], 6.0, delta=0.02)
        self.assertEqual(rows[0]["time"], "2026-11-12T23:58:00+07:00")  # local time of the workout
        self.assertNotIn("lat", rows[0])

    def test_one_time_for_every_point_drops_time_speed_and_pace(self):
        self.write_route([("2026-03-16T05:50:21Z", 10.0 + i / 1000, 106.0, 5) for i in range(3)])
        err = io.StringIO()
        with redirect_stderr(err):
            rows = M.route_rows(UUID, gps=True)
        self.assertIn("not usable", err.getvalue())
        self.assertEqual(set(rows[0]), {"distance_m", "altitude_m", "lat", "lon"})
        self.assertAlmostEqual(rows[-1]["distance_m"], 222.4, delta=0.5)

    def test_times_outside_the_workout_are_unusable(self):
        self.write_route([("2026-11-10T01:00:00Z", 10.0, 106.0, 5), ("2026-11-10T01:00:40Z", 10.001, 106.0, 5)])
        with redirect_stderr(io.StringIO()):
            rows = M.route_rows(UUID, gps=False)
        self.assertNotIn("speed_mps", rows[-1])

    def test_workout_hr_crosses_midnight(self):
        def series(day_start: str, values: list[str]) -> dict:
            return {"data": {"ObservationList": [{"effectivePeriod": {"start": day_start},
                                                  "valueSampledData": {"period": 60000, "data": " ".join(values)}}]}}
        day1 = ["E"] * 1440
        day1[1437:1440] = ["120", "E", "130"]           # 23:57 (before the start), 23:58, 23:59
        day2 = ["140", "150", "E", "160"] + ["E"] * 1436  # 00:00 .. 00:03
        answers = {"eq2026-11-12T00:00:00+07:00": series("2026-11-12T00:00:00+07:00", day1),
                   "eq2026-11-13T00:00:00+07:00": series("2026-11-13T00:00:00+07:00", day2)}
        M.graphql = lambda query, variables=None: answers[variables["d"]]
        rows = M.workout_hr(UUID)
        self.assertEqual([r["time"][11:16] for r in rows], ["23:58", "23:59", "00:00", "00:01", "00:02"])
        self.assertEqual([r["heart_rate"] for r in rows], [None, 130.0, 140.0, 150.0, None])

    def test_one_session_recorded_by_two_apps_is_merged(self):
        def w(uuid, start, end, kind="running", route=False, hr=None, distance=5000, source="App"):
            return {"workout": uuid, "start": start, "end": end, "type": kind, "route": route, "avg_hr": hr,
                    "distance_m": distance, "source": source}
        rows = [w("a", "2026-06-13T17:34:14+07:00", "2026-06-13T18:18:04+07:00", source="Watch app"),
                w("b", "2026-06-13T17:34:14+07:00", "2026-06-13T18:18:04+07:00", route=True, source="Run app"),
                w("c", "2026-06-13T17:34:00+07:00", "2026-06-13T18:00:00+07:00", kind="cycling"),     # other type
                w("d", "2026-06-13T18:10:00+07:00", "2026-06-13T18:40:00+07:00")]                    # short overlap
        out = M.merge_duplicates(rows)
        self.assertEqual(sorted(x["workout"] for x in out), ["b", "c", "d"])                # the one with a route is kept
        kept = next(x for x in out if x["workout"] == "b")
        self.assertEqual(kept["also_recorded_by"], [{"source": "Watch app", "workout": "a"}])
        self.assertEqual(next(x for x in out if x["workout"] == "d")["also_recorded_by"], [])

    def test_person_must_be_named_when_there_are_several(self):
        M.people = lambda: [{"patient": "p1", "user": "u_1", "name": "Alex"}, {"patient": "p2", "user": "u_2", "name": "Mẹ"}]
        self.assertEqual(M.resolve_person("mẹ"), "p2")
        self.assertEqual(M.resolve_person("u_1"), "p1")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            M.resolve_person(None)
        M.people = lambda: [{"patient": "p1", "user": "u_1", "name": "Alex"}]
        self.assertEqual(M.resolve_person(None), "p1")


if __name__ == "__main__":
    unittest.main()
