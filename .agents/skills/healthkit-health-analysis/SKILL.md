---
name: healthkit-health-analysis
description: Analyse a family member's Apple Health (HealthKit) data stored in the local Medplum FHIR server - steps, distance, flights, active and basal energy, heart rate (daily min/max, resting, one-minute series), HRV (SDNN), SpO2, respiration, weight and body measurements, blood pressure, glucose, sleep with stages, workouts with routes, ECGs and any other HealthKit type - with read-only GraphQL, the Medplum MCP and tools/medplum_agent.py. Use for questions like "how did I sleep this month", "is my resting heart rate going up", "how far did Mom walk last week", "compare my runs", or any trend, comparison or correlation over the HealthKit data.
---

# HealthKit health analysis (Medplum, read-only)

The data is FHIR R4 in the local Medplum (http://localhost:8203), loaded from the processor's day
folders by `hka fhir load`. Run everything from the repository root. Everything here is read-only: the
agent client cannot change data (writes get HTTP 403).

## 1. Check the server and pick the person

```sh
python3 tools/medplum_agent.py people
```

It lists every family member: `patient` (the Patient id), `user` (`u_…`), `name`, `gender`,
`birthDate`, `utc_offset` and the time of their latest Observation. If it fails with "cannot reach
Medplum", the stack is down (`cd medplum && docker compose up -d`); say so and stop. Do not use the
loader client or the admin login to read data.

**Every question is about one person.** If there is more than one and the question does not say who,
ask. Filter every query with `subject: "Patient/<patient>"`.

## 2. Two ways in

- **GraphQL (preferred for analysis):** `python3 tools/medplum_agent.py graphql '<query>' [--vars '<json>']`.
  Returns compact JSON with only the fields you ask for; pipe it into `python3 -c` for the maths.
- **MCP:** if your agent has the `hka-medplum` MCP server connected (`.mcp.json` for Claude Code;
  other agents: `docs/agents.md`), its `fhir-request` tool runs FHIR REST (`GET Observation?...`).
  Same data, same read-only client. Use it when you cannot run shell commands.

## 3. Rules that prevent wrong answers

0. **Medplum is the only source.** Do not read anything under the processor's data directory
   (`users/<u>/days/`, `fhir/`, `inbox/`) to answer a question. If Medplum is unavailable, say how to
   fix it and stop. Fall back to the day folders only if the person explicitly says so.
1. **Dates carry the person's UTC offset** (`utc_offset` from `people`; members can live in different
   zones; the examples below use `+07:00`, replace it with the person's). Days are local calendar days 00:00 to 24:00. In GraphQL use
   `_filter: "date ge 2026-11-01T00:00:00+07:00 and date lt 2026-12-01T00:00:00+07:00"`, or
   `date: "eq2026-11-13T00:00:00+07:00"` for one day's daily resources. In REST URLs encode `+` as
   `%2B`. A bare date is read as UTC and shifts or loses days.
2. **Daily totals and single readings look different.** Daily resources (steps, energy, HR min/max,
   the HR series, sleep) have `effectivePeriod`; single readings (resting HR, weight, SpO2, blood
   pressure, HRV) have `effectiveDateTime`, and a day can have several or none. Ask for both fields
   when unsure.
3. **One source per daily total; never add sources.** iPhone, Apple Watch and other apps that write
   into Health (a fitness band's app, a running app, Garmin Connect) can all count steps. Each daily total already
   uses one source, named in `device { display }`. Do not sum Observations of the same metric and
   day. If a person also keeps a [garmin-archive](https://github.com/jollyjoker992/garmin-archive) and
   Garmin Connect writes into their Health, the same days exist in both archives: compare them, never
   add them.
4. **One workout can be recorded by several apps.** A watch's app and a running app (e.g. Garmin Connect
   and Strava) often both write the same session into Health, each with its own UUID. `workouts` merges
   workouts of the same type that overlap for most of their time and lists the others under
   `also_recorded_by`; count and compare sessions from it, not from a raw GraphQL list (`workouts --all`
   shows every record). If you query workouts in GraphQL, merge overlapping ones yourself.
5. **Look up a custom code before interpreting it:** `python3 tools/medplum_agent.py codes <text>`
   prints definition, unit and value type of `hka-metrics` codes.
6. **Missing is not zero.** No Observation, or an absent component, means no measurement (watch off,
   not synced, source not shared). Report gaps; never fill them with 0. A 0 that is present is real.
   Sleep with no asleep stage has `dataAbsentReason` instead of a value.
7. **HRV is SDNN** (Apple's measure, LOINC 80404-7), not RMSSD. Do not compare it with Garmin or
   Oura HRV numbers, and compare a person with their own history, not population norms.
8. **Sleep belongs to the morning it ends.** A night is one Observation on the day it ends (the sleep
   day runs 18:00 to 18:00); an evening nap after 18:00 counts toward the next day. The value is
   minutes asleep; `sleep-in-bed` is time in bed (not asleep). Sources without stage tracking report
   only `sleep-unspecified`.
9. **Energy is split.** `active-energy-daily` and `basal-energy-daily` are separate; total burned is
   their sum. A workout's LOINC 55424-6 is its active energy only.
10. **Preliminary days can still change** (`status: preliminary`): late syncs reopen a day, and a day
   turns `final` after 7 quiet days. After a phone re-sends its history, every day is preliminary for
   a week; that says nothing about data quality. The latest one or two days are often incomplete.
11. **Ask for enough rows.** A list stops at `_count` (at most 1000): ten years of steps is over 3,600
    days. Page with `_offset: 1000`, `2000`, ... until a page comes back shorter than `_count`.
12. **Not medical advice.** Describe patterns and how they compare with the person's own history.
    For symptoms or worrying values, suggest a clinician.
13. **Personal data stays local.** Do not send raw data to external services. Routes are home and
    running locations: use `--no-gps` unless the question is about the route, and never paste
    coordinates into an answer unless asked.

## 4. Where each metric is

`value` is `valueQuantity.value`. LOINC system `http://loinc.org`; custom system
`https://github.com/jollyjoker992/healthkit-archive/fhir/CodeSystem/hka-metrics` (below `hka|code`);
every Observation also has its HealthKit type in
`https://github.com/jollyjoker992/healthkit-archive/fhir/CodeSystem/healthkit-type`. In GraphQL
`code:` arguments write `<system>|<code>`.

| Question | Code | Kind | Unit / notes |
|---|---|---|---|
| Steps | `41950-7` | daily | `/d` |
| Walking + running distance | `41953-1` | daily | `m` (includes running) |
| Cycling distance | `hka|distance-cycling-daily` | daily | `m` |
| Flights climbed | `93833-2` | daily | `{#}` |
| Active / basal energy | `hka|active-energy-daily`, `hka|basal-energy-daily` | daily | `kcal`; total = sum |
| Exercise minutes (Apple ring) | `hka|exercise-minutes-daily` | daily | `min` |
| Daily min / max heart rate | `8883-1` / `8873-2` | daily | `/min` |
| Heart rate through the day | `hka|hr-minute-mean`, `valueSampledData` | daily | one slot per minute, `E` = no reading; 1440 slots (1380/1500 on DST days) |
| Resting heart rate | `40443-4` | reading | `/min` |
| HRV | `80404-7` | reading | `ms`, SDNN |
| SpO2 | `2708-6` | reading | `%` |
| Respiratory rate | `9279-1` | reading | `/min` |
| Body temperature | `8310-5` | reading | `Cel` |
| Weight, height, BMI, body fat, lean mass, waist | `29463-7`, `8302-2`, `39156-5`, `41982-0`, `91557-9`, `8280-0` | reading | `kg`, `cm`, `kg/m2`, `%`, `kg`, `cm` |
| Blood pressure | `85354-9`, components `8480-6` systolic, `8462-4` diastolic | reading | `mm[Hg]` |
| Blood glucose | `2339-0` | reading | `mg/dL` (mmol/L converted) |
| VO2 max | `hka|vo2-max` | reading | `mL/(kg.min)` |
| Sleep | `93832-4`, `effectivePeriod` = first to last record | daily (by morning) | `min` asleep; components `93831-6` deep, `93830-8` light (Apple "core"), `93829-0` REM, `93828-2` awake, `hka|sleep-in-bed`, `hka|sleep-unspecified` |
| Workouts | `55411-3` Exercise duration, one per workout | per workout | `min`; components `73985-4` type (text), `55425-3` avg HR, `55426-1` max HR, `55424-6` active kcal, `hka|workout-distance` (m); route via `derivedFrom` |
| ECG | `healthkit-type|HKDataTypeIdentifierElectrocardiogram` | reading | components `hka|ecg-classification` (text), `hka|ecg-lead-i` (µV, `valueSampledData`), `8867-4` average HR |
| Anything else (water, nutrients, mindful minutes, ...) | `healthkit-type|<Apple identifier>` | reading | unit of the sample; category values in `valueCodeableConcept.text` |

Not in Medplum (kept only in the day folders): heartbeat series, audiograms, State of Mind, GAD-7 /
PHQ-9, the per-interval sleep timeline, workout events. Say so if asked.

## 5. Query recipes (tested)

Daily series for one person and range:

```sh
python3 tools/medplum_agent.py graphql 'query($s: String, $r: String) {
  ObservationList(subject: $s, code: "http://loinc.org|41950-7", _filter: $r, _sort: "date", _count: 1000) {
    status effectivePeriod { start } valueQuantity { value unit } device { display } } }' \
  --vars '{"s": "Patient/<patient>",
           "r": "date ge 2026-11-01T00:00:00+07:00 and date lt 2026-12-01T00:00:00+07:00"}'
```

Several metrics at once (aliases), e.g. to correlate; readings use `effectiveDateTime`:

```graphql
{ steps: ObservationList(subject: "Patient/<patient>", code: "http://loinc.org|41950-7", _sort: "date", _count: 1000) { effectivePeriod { start } valueQuantity { value } }
  rhr:   ObservationList(subject: "Patient/<patient>", code: "http://loinc.org|40443-4", _sort: "date", _count: 1000) { effectiveDateTime valueQuantity { value } }
  sleep: ObservationList(subject: "Patient/<patient>", code: "http://loinc.org|93832-4", _sort: "date", _count: 1000) { effectivePeriod { end } valueQuantity { value } } }
```

Sleep with stages (key the components by code, see the Python below):

```graphql
{ ObservationList(subject: "Patient/<patient>", code: "http://loinc.org|93832-4", _sort: "-date", _count: 30) {
    effectivePeriod { start end } valueQuantity { value } dataAbsentReason { text } device { display }
    component { code { coding { system code } } valueQuantity { value unit } } } }
```

Heart rate through one day (the series starts at local midnight; slot i is minute i):

```graphql
{ ObservationList(subject: "Patient/<patient>",
    code: "https://github.com/jollyjoker992/healthkit-archive/fhir/CodeSystem/hka-metrics|hr-minute-mean",
    date: "eq2026-11-13T00:00:00+07:00", _count: 1) {
    effectivePeriod { start } valueSampledData { period data } } }
```

Blood pressure readings:

```graphql
{ ObservationList(subject: "Patient/<patient>", code: "http://loinc.org|85354-9", _sort: "-date", _count: 100) {
    effectiveDateTime component { code { coding { code } } valueQuantity { value unit } } } }
```

Any other HealthKit type (find the identifier in Apple's documentation, e.g. `HKQuantityTypeIdentifierDietaryWater`):

```graphql
{ ObservationList(subject: "Patient/<patient>",
    code: "https://github.com/jollyjoker992/healthkit-archive/fhir/CodeSystem/healthkit-type|HKQuantityTypeIdentifierDietaryWater",
    _sort: "-date", _count: 100) { effectiveDateTime effectivePeriod { start end } valueQuantity { value unit } valueCodeableConcept { text } } }
```

Components into a dict in Python. A component can carry two codings (its LOINC or `hka-metrics` code
and the HealthKit type), so pick the code by system, not by position:

```python
def code_of(cc):
    return next(x["code"] for x in cc["coding"] if not x["system"].endswith("/healthkit-type"))
c = {code_of(x["code"]): (x.get("valueQuantity") or {}).get("value")
     or (x.get("valueCodeableConcept") or {}).get("text") for x in obs.get("component") or []}
```
(Ask for `coding { system code }` in the query.)

## 6. Inside a workout

HealthKit has no second-by-second workout file like Garmin's FIT. What there is:

```sh
python3 tools/medplum_agent.py workouts --person <patient|u_…|name> [--from ISO] [--to ISO]   # list, newest first
python3 tools/medplum_agent.py workout-hr <workout> > hr.csv          # heart rate per minute during it
python3 tools/medplum_agent.py route <workout> --no-gps --every 10    # GPS points: distance, speed, pace, altitude
```

- `<workout>` is the HealthKit UUID that `workouts` prints (one per session; other apps' records of it are in
  `also_recorded_by`). `route: true` means a GPS route exists.
- "Longest" means farthest unless the question says time; when the top sessions are within about 1% of
  each other, say it is a near tie and name them.
- `minutes` is HealthKit's workout duration as the recording app set it, usually without pauses;
  `start` to `end` is the elapsed time. Say which one a pace is based on.
- There is no second-by-second data (pace, cadence, power per second) in HealthKit. If the person also
  keeps a garmin-archive and the workout came from Garmin Connect, its FIT file is there: say so, and
  analyse it with that archive's own skill rather than mixing the two archives in one answer.
- `workout-hr` cuts the day's one-minute heart-rate series to the workout (all sources pooled). Empty
  minutes are gaps, not zero. How dense it is depends on the source: an Apple Watch records every few
  seconds during a workout; heart rate synced from Garmin Connect arrives about every 2 minutes, so
  every other minute is empty. That is normal, not a dropout; say how many readings you used. It is the only in-workout heart rate; `avg_hr`/`max_hr` are often absent
  for workouts written by third-party apps.
- `route` computes distance and speed from consecutive points. Some apps write every point with one
  timestamp; the tool then says the times are unusable and leaves out time, speed and pace. Report
  the distance and shape only, never a pace from those routes.
- Routes are cached in `.cache/routes/` (mode 600, ignored by git).

## 7. How to answer

1. Say whose data and which date range, and how many days actually have data. When comparing periods,
   give the coverage of each (e.g. "October: 10 of 31 days"); a thin period makes the comparison weak.
2. Give the numbers (averages, ranges, trends) with units, from the query results; show the query if
   the person may want to rerun it.
3. Compare with the person's own earlier periods, not population norms, unless asked.
4. Name the source when it matters (`device.display`), and call out gaps, preliminary days and
   sources that lack a metric (no Apple Watch means no HRV, SpO2 or exercise minutes).
