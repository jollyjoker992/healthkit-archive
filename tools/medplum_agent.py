#!/usr/bin/env python3
"""Read-only access to the local Medplum for agents and scripts. Standard library only.

Always uses the *agent* client (HKA_FHIR_AGENT_CLIENT_ID / _SECRET from the repository's .env),
whose access policy allows reading and searching only.

    python3 tools/medplum_agent.py token              print an access token
    python3 tools/medplum_agent.py headers            print {"Authorization": "Bearer ..."} (MCP headersHelper)
    python3 tools/medplum_agent.py graphql QUERY      run a GraphQL query, print the JSON result
    python3 tools/medplum_agent.py graphql @file.graphql [--vars '{"from": "..."}']
    python3 tools/medplum_agent.py codes [TEXT]       hka-metrics codes (unit, value type, definition), optionally filtered
    python3 tools/medplum_agent.py people             family members: Patient id, user id, name, sex, birth date, UTC offset
    python3 tools/medplum_agent.py workouts [--person P] [--from ISO] [--to ISO]   workouts, with or without a route
    python3 tools/medplum_agent.py route WORKOUT [--format csv|json] [--every N] [--no-gps] [--out FILE]
    python3 tools/medplum_agent.py workout-hr WORKOUT [--format csv|json]          heart rate per minute during it

PERSON is a Patient id, a user id (u_...) or a name from `people`; it can be left out when there is
only one person. WORKOUT is the workout's HealthKit UUID as `workouts` prints it.

`route` downloads the workout's GPX (a Binary in Medplum, read-only client), caches it in
.cache/routes/ (mode 600) and prints one row per GPS point with distance and speed computed from
the points. `workout-hr` cuts the day's one-minute heart-rate series to the workout's time.

Tokens last one hour. They are cached in .cache/medplum-agent-token.json (mode 600) and renewed
five minutes before they expire, so frequent calls do not hit Medplum's auth rate limit.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASE_URL = "https://github.com/jollyjoker992/healthkit-archive/fhir"
HKA_SYSTEM = f"{BASE_URL}/CodeSystem/hka-metrics"
OBS_SYSTEM = f"{BASE_URL}/identifier/observation"
DOC_SYSTEM = f"{BASE_URL}/identifier/document"
USER_SYSTEM = f"{BASE_URL}/identifier/hka-user"
CACHE_DIR = REPO / ".cache"


def read_env() -> dict[str, str]:
    env = {}
    path = REPO / ".env"
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.removeprefix("export ").partition("=")
                env[key.strip()] = value.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k.startswith("HKA_")})
    return env


ENV = read_env()
SERVER = ENV.get("HKA_FHIR_URL", "http://localhost:8203").rstrip("/")


def fail(message: str) -> None:
    print(f"medplum_agent: {message}", file=sys.stderr)
    sys.exit(2)


def write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def token() -> str:
    client_id = ENV.get("HKA_FHIR_AGENT_CLIENT_ID")
    secret = ENV.get("HKA_FHIR_AGENT_CLIENT_SECRET")
    if not client_id or not secret:
        fail("HKA_FHIR_AGENT_CLIENT_ID / _SECRET are not set; run 'python3 medplum/setup.py project'")
    cache = CACHE_DIR / "medplum-agent-token.json"
    try:
        cached = json.loads(cache.read_text())
        if cached.get("client_id") == client_id and cached.get("server") == SERVER \
                and cached.get("expires_at", 0) - time.time() > 300:
            return cached["access_token"]
    except (OSError, ValueError):
        pass
    form = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": client_id,
                                   "client_secret": secret}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(SERVER + "/oauth2/token", data=form), timeout=30) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        fail(f"token request failed: HTTP {exc.code} {exc.read()[:200]!r}")
    except urllib.error.URLError as exc:
        fail(f"cannot reach Medplum at {SERVER} ({exc.reason}); is 'docker compose up -d' running in medplum/?")
    write_private(cache, json.dumps({"client_id": client_id, "server": SERVER, "access_token": body["access_token"],
                                     "expires_at": time.time() + int(body.get("expires_in", 3600))}).encode())
    return body["access_token"]


def graphql(query: str, variables: dict | None = None) -> dict:
    data = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(SERVER + "/fhir/R4/$graphql", data=data, method="POST",
                                 headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        fail(f"GraphQL request failed: HTTP {exc.code} {exc.read()[:500]!r}")
    except urllib.error.URLError as exc:
        fail(f"cannot reach Medplum at {SERVER} ({exc.reason}); is 'docker compose up -d' running in medplum/?")


def data_of(result: dict) -> dict:
    if result.get("errors"):
        fail(f"GraphQL errors: {json.dumps(result['errors'])[:500]}")
    return result["data"]


def codes(text: str | None) -> list[dict]:
    result = data_of(graphql('query($url: String) { CodeSystemList(url: $url) { version concept { code display '
                             'concept { code display definition property { code valueString } } } } }',
                             {"url": HKA_SYSTEM}))
    systems = result.get("CodeSystemList") or []
    if not systems:
        fail("hka-metrics CodeSystem not found on the server; has 'hka fhir load' run?")
    rows = []
    for group in systems[0]["concept"]:
        for c in group.get("concept") or []:
            props = {p["code"]: p.get("valueString") for p in c.get("property") or []}
            row = {"group": group["display"], "code": c["code"], "display": c["display"], "unit": props.get("unit"),
                   "valueType": props.get("valueType"), "definition": c.get("definition")}
            if not text or text.lower() in json.dumps(row).lower():
                rows.append(row)
    return rows


# -------------------------------------------------------------------------- people

def people() -> list[dict]:
    pats = data_of(graphql("{ PatientList(_count: 100) { id identifier { system value } name { text } "
                           "gender birthDate } }"))["PatientList"]
    out = []
    for p in pats:
        user = next((i["value"] for i in p.get("identifier") or [] if i["system"] == USER_SYSTEM), None)
        latest = data_of(graphql('query($s: String) { ObservationList(subject: $s, _sort: "-date", _count: 1) { '
                                 'effectivePeriod { start } effectiveDateTime } }', {"s": f"Patient/{p['id']}"}))
        obs = latest["ObservationList"]
        when = (obs[0].get("effectiveDateTime") or (obs[0].get("effectivePeriod") or {}).get("start")) if obs else None
        out.append({"patient": p["id"], "user": user, "name": ((p.get("name") or [{}])[0]).get("text"),
                    "gender": p.get("gender"), "birthDate": p.get("birthDate"),
                    "utc_offset": when[-6:] if when and re.search(r"[+-]\d\d:\d\d$", when) else None,
                    "latest_observation": when})
    return out


def resolve_person(person: str | None) -> str:
    """Patient id for a Patient id, user id or (part of a) name; the only person when None."""
    everyone = people()
    if not everyone:
        fail("no Patient on the server; has 'hka fhir load' run?")
    if person is None:
        if len(everyone) > 1:
            fail("several people on the server; pass --person (see 'people'): "
                 + ", ".join(f"{p['name']} ({p['user']})" for p in everyone))
        return everyone[0]["patient"]
    for p in everyone:
        if person in (p["patient"], p["user"]) or (p["name"] and person.lower() in p["name"].lower()):
            return p["patient"]
    fail(f"no person matches {person!r} (see 'people')")


# -------------------------------------------------------------------------- workouts

def component_dict(obs: dict) -> dict:
    return {x["code"]["coding"][0]["code"]: (x.get("valueQuantity") or {}).get("value")
            if x.get("valueQuantity") else (x.get("valueCodeableConcept") or {}).get("text")
            for x in obs.get("component") or []}


def list_workouts(person: str | None, start: str | None, end: str | None) -> list[dict]:
    flt = " and ".join(f"date {op} {v}" for op, v in (("ge", start), ("lt", end)) if v)
    # Medplum cannot parse an empty _filter, so the argument is left out when there is no range.
    query = ('query($s: String' + (', $f: String' if flt else '') + ') { ObservationList(subject: $s, '
             'code: "http://loinc.org|55411-3", ' + ('_filter: $f, ' if flt else '') + '_sort: "-date", _count: 1000) '
             '{ identifier { value } effectivePeriod { start end } '
             'valueQuantity { value } device { display } '
             'component { code { coding { code } } valueQuantity { value } valueCodeableConcept { text } } '
             'derivedFrom { reference } } }')
    rows = data_of(graphql(query, {"s": f"Patient/{resolve_person(person)}", "f": flt or None}))["ObservationList"]
    out = []
    for o in rows:
        c = component_dict(o)
        out.append({"workout": o["identifier"][0]["value"], "start": o["effectivePeriod"]["start"],
                    "end": o["effectivePeriod"]["end"], "type": c.get("73985-4"),
                    "minutes": (o.get("valueQuantity") or {}).get("value"), "distance_m": c.get("workout-distance"),
                    "avg_hr": c.get("55425-3"), "max_hr": c.get("55426-1"), "active_kcal": c.get("55424-6"),
                    "route": bool(o.get("derivedFrom"))})
    return out


def workout(uuid: str) -> dict:
    obs = data_of(graphql('query($i: String) { ObservationList(identifier: $i) { subject { reference } '
                          'effectivePeriod { start end } component { code { coding { code } } '
                          'valueCodeableConcept { text } } } }',
                          {"i": f"{OBS_SYSTEM}|{uuid}"}))["ObservationList"]
    if not obs:
        fail(f"no workout {uuid} (see 'workouts')")
    return obs[0]


def parse_time(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def route_points(uuid: str) -> tuple[list[dict], dt.tzinfo]:
    w = workout(uuid)
    zone = parse_time(w["effectivePeriod"]["start"]).tzinfo
    cached = CACHE_DIR / "routes" / f"{uuid}.gpx"
    if not cached.is_file():
        docs = data_of(graphql('query($i: String) { DocumentReferenceList(identifier: $i) { content { attachment '
                               '{ url } } } }', {"i": f"{DOC_SYSTEM}|route/{uuid}"}))["DocumentReferenceList"]
        if not docs:
            fail(f"workout {uuid} has no route in Medplum (see 'workouts', column route)")
        match = re.search(r"(?:Binary/|/storage/)([0-9a-f-]{36})", docs[0]["content"][0]["attachment"]["url"])
        if not match:
            fail(f"cannot find the Binary id in the route attachment of {uuid}")
        req = urllib.request.Request(f"{SERVER}/fhir/R4/Binary/{match.group(1)}",
                                     headers={"Authorization": f"Bearer {token()}", "Accept": "application/gpx+xml"})
        with urllib.request.urlopen(req, timeout=120) as r:
            write_private(cached, r.read())
    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    points = []
    for p in ET.parse(cached).getroot().iterfind(".//g:trkpt", ns):
        ele = p.find("g:ele", ns)
        when = p.find("g:time", ns)
        points.append({"t": parse_time(when.text).astimezone(zone) if when is not None else None,
                       "lat": float(p.get("lat")), "lon": float(p.get("lon")),
                       "ele": float(ele.text) if ele is not None else None})
    return points, zone


def haversine(a: dict, b: dict) -> float:
    r = 6371008.8
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dp, dl = p2 - p1, math.radians(b["lon"] - a["lon"])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def times_usable(points: list[dict], uuid: str) -> bool:
    """Point times are usable when they advance and fall within the workout. Some apps that write routes
    into Health (seen with an import from a third-party app) stamp every point with one time."""
    times = [p["t"] for p in points if p["t"]]
    if len(times) < 2 or len(set(times)) < len(times) / 2:
        return False
    w = workout(uuid)["effectivePeriod"]
    start, end = parse_time(w["start"]), parse_time(w["end"])
    slack = dt.timedelta(minutes=30)
    return start - slack <= times[0] and times[-1] <= end + slack


def route_rows(uuid: str, gps: bool) -> list[dict]:
    points, _ = route_points(uuid)
    if points and not times_usable(points, uuid):
        print("medplum_agent: this route's point times are not usable (the source wrote one time for every point, "
              "or times outside the workout); time, speed and pace are left out, distance and altitude are kept",
              file=sys.stderr)
        for p in points:
            p["t"] = None
    rows, total = [], 0.0
    t0 = points[0]["t"] if points else None
    for i, p in enumerate(points):
        step = haversine(points[i - 1], p) if i else 0.0
        total += step
        secs = (p["t"] - points[i - 1]["t"]).total_seconds() if i and p["t"] and points[i - 1]["t"] else None
        speed = step / secs if secs else None
        row = {"time": p["t"].isoformat(timespec="seconds") if p["t"] else None,
               "elapsed_s": int((p["t"] - t0).total_seconds()) if p["t"] and t0 else None,
               "distance_m": round(total, 1), "speed_mps": round(speed, 2) if speed is not None else None,
               "pace_min_per_km": round(1000 / speed / 60, 2) if speed else None,
               "altitude_m": p["ele"]}
        if gps:
            row["lat"], row["lon"] = p["lat"], p["lon"]
        rows.append(row)
    used = {k for row in rows for k, v in row.items() if v is not None}
    return [{k: v for k, v in row.items() if k in used} for row in rows]


def workout_hr(uuid: str) -> list[dict]:
    """The one-minute heart-rate means of the minutes the workout covers (a workout can cross midnight)."""
    w = workout(uuid)
    start, end = parse_time(w["effectivePeriod"]["start"]), parse_time(w["effectivePeriod"]["end"])
    out = []
    day = start.date()
    while day <= end.date():
        begin = dt.datetime.combine(day, dt.time(), tzinfo=start.tzinfo)
        series = data_of(graphql(
            'query($s: String, $d: String) { ObservationList(subject: $s, code: '
            f'"{HKA_SYSTEM}|hr-minute-mean", date: $d, _count: 1) {{ effectivePeriod {{ start }} '
            'valueSampledData { period data } } }',
            {"s": w["subject"]["reference"], "d": "eq" + begin.isoformat()}))["ObservationList"]
        if series:
            s = series[0]
            origin = parse_time(s["effectivePeriod"]["start"])
            period = dt.timedelta(milliseconds=float(s["valueSampledData"]["period"]))
            for i, value in enumerate(s["valueSampledData"]["data"].split()):
                t = origin + i * period
                if start - period < t < end:
                    out.append({"time": t.astimezone(start.tzinfo).isoformat(timespec="minutes"),
                                "heart_rate": None if value == "E" else float(value)})
        day += dt.timedelta(days=1)
    return out


# -------------------------------------------------------------------------- output

def emit(rows: list[dict], fmt: str, out_path: str | None) -> None:
    import csv
    out = open(out_path, "w", newline="") if out_path else sys.stdout
    try:
        if fmt == "json":
            json.dump(rows, out, indent=1, ensure_ascii=False)
            out.write("\n")
        else:
            writer = csv.DictWriter(out, fieldnames=list(rows[0]) if rows else ["time"])
            writer.writeheader()
            writer.writerows(rows)
    finally:
        if out is not sys.stdout:
            out.close()


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    opt = lambda name: rest[rest.index(name) + 1] if name in rest else None
    positional = rest[0] if rest and not rest[0].startswith("--") else None
    if cmd == "token":
        print(token())
    elif cmd == "headers":
        print(json.dumps({"Authorization": f"Bearer {token()}"}))
    elif cmd == "graphql":
        if not rest:
            fail("graphql needs a query or @file")
        query = Path(rest[0][1:]).read_text() if rest[0].startswith("@") else rest[0]
        variables = json.loads(opt("--vars")) if "--vars" in rest else None
        result = graphql(query, variables)
        print(json.dumps(result, indent=1, ensure_ascii=False))
        return 1 if result.get("errors") else 0
    elif cmd == "codes":
        print(json.dumps(codes(positional), indent=1, ensure_ascii=False))
    elif cmd == "people":
        print(json.dumps(people(), indent=1, ensure_ascii=False))
    elif cmd == "workouts":
        print(json.dumps(list_workouts(opt("--person"), opt("--from"), opt("--to")), indent=1, ensure_ascii=False))
    elif cmd in ("route", "workout-hr"):
        if not positional:
            fail(f"{cmd} needs a workout UUID (see 'workouts')")
        rows = route_rows(positional, gps="--no-gps" not in rest) if cmd == "route" else workout_hr(positional)
        rows = rows[::max(int(opt("--every") or 1), 1)]
        emit(rows, opt("--format") or "csv", opt("--out"))
    else:
        fail(f"unknown command {cmd!r}; see --help")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except BrokenPipeError:            # output piped into head, less, ...
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
