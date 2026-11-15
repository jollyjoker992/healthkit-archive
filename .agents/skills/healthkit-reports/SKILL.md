---
name: healthkit-reports
description: Produce a PDF report from a family member's Apple Health data in the local Medplum - a daily report for one day (heart rate through the day, sleep stages, steps and energy, compared with the previous 28 days), a workout report (duration, distance, pace, heart rate during the workout and its drift, splits when the route has usable times, comparison with recent workouts of the same type) or a period report for a week, month or range (sleep, resting heart rate, HRV, steps, distance, active energy, workouts, against the period before). Use when the user asks for a report, a PDF, a weekly or monthly summary, or a write-up of a run or other workout.
---

# HealthKit reports (PDF)

One generator, `tools/hka_report.py`, three report kinds, always in three steps. The script computes
the facts and draws the PDF; **you** write the evaluation and recommendations from those facts. Never
skip step 2 and never put a claim in the narrative that the facts do not support.

Data comes from the local Medplum with the read-only client (see the `healthkit-health-analysis`
skill and its rule 0: never from the processor's day folders).

## 0. Before you start

- Medplum must be running: `python3 tools/medplum_agent.py people` lists the family members. If it
  cannot reach Medplum, say so (`cd medplum && docker compose up -d`) and stop.
- **Pick the person.** Day and period reports take `--person <name | u_… | Patient id>`; it can be
  left out only when there is one person. A workout report finds its person from the workout.
- The PDF needs matplotlib and reportlab. If the script stops with "reports need matplotlib and
  reportlab", tell the person to run `python3 -m pip install -r tools/requirements-reports.txt`. Do not
  install it for them without asking.
- Work in a temporary folder for the facts and narrative, e.g. `mkdir -p /tmp/hka-report`.

## 1. Pick the subject and compute the facts

**One day** ("how was yesterday", "13 November"): the day against the previous 28 days. Today is
allowed: last night's sleep and today's readings count; steps, distance and energy are partial and
left out of the comparison.

```sh
python3 tools/hka_report.py day --date 2026-11-13 --person <who> --facts /tmp/hka-report/facts.json
```

**A workout:** find it with `python3 tools/medplum_agent.py workouts --person <who> --from <ISO with offset>`
(one entry per session even when several apps recorded it). "Longest" means farthest unless the person
says time; if the top sessions are within about 1% of each other, pick the longer one by time, and say it
is a near tie in the summary. If the request is ambiguous and you can ask, ask. Then

```sh
python3 tools/hka_report.py workout <workout uuid> --facts /tmp/hka-report/facts.json
```

Add `--route` only when asked for the route shape: it shows where the person lives and runs.

**A period:** a week (`--week 2026-W45`, ISO week, Monday to Sunday), a month (`--month 2026-10`) or a
range (`--from 2026-10-01 --to 2026-10-14`), against the period of the same length before it. "Last
week" is the previous complete ISO week (Monday to Sunday) before the current one, even on a Sunday;
"this week" is the current one (in progress). "Last month" is the previous calendar month. Without a
clear period, use the last complete week or month and say so. A one-day range becomes a day report.

```sh
python3 tools/hka_report.py period --month 2026-10 --person <who> --facts /tmp/hka-report/facts.json
```

Read the whole facts file (a few KB; it holds no raw series, only results computed from them).

## 2. Write the narrative

Write `/tmp/hka-report/narrative.json`:

```json
{"language": "en",
 "headline": "one sentence: the single most important finding",
 "summary": "3-5 sentences: what happened, with the key numbers",
 "evaluation": ["3-6 findings, each with its numbers and what they mean"],
 "recommendations": ["2-5 concrete next steps, each tied to a finding above"],
 "data_notes": ["optional: the facts' data_notes, translated"]}
```

Rules:

- **Every number must come from the facts file** (or, to explain which workout you picked, from the
  `workouts` listing). Round sensibly, keep units, use local dates. If something is missing (no HRV
  readings, no heart rate during a workout, days without data), say so instead of guessing.
- **Write units as the report's language does**, so the narrative matches the charts and tables:
  `bpm`, `min`, `km` in English; `nhịp/phút`, `phút`, `km` and decimal commas (`57,6`) in Vietnamese.
- **Respect coverage.** Read `coverage` (period) and `days_with_data` (day comparison). If a metric
  covers few days in either period (the facts add a "Thin coverage" note), say the comparison is weak;
  never present a change based on a handful of days as a trend.
- **Compare with the person, not the population**: the previous 28 days, the previous period
  (`change_vs_previous_avg`), the same workout type over the previous 8 weeks
  (`same_type_previous_8_weeks`, `efficiency_m_per_beat`). Population figures only where they are
  standard (WHO: 150 minutes of moderate activity per week).
- **Read the data notes.** They flag what changes the meaning of a number: merged duplicate workouts,
  a source that writes the same basal energy every day (an estimate, not a measurement), thin coverage,
  partial days. Carry what matters into the narrative.
- **HealthKit specifics:** HRV is SDNN, spot readings, comparable only with the person's own values.
  Active and basal energy are separate. A workout's heart rate is the one-minute series cut to the
  workout: with few readings (`readings`), say the shape is approximate. Without usable route times
  there are no splits and no in-workout pace; only the average pace from duration and distance.
- **Recommendations are specific and doable**: a bedtime, a step target, a heart-rate cap for easy
  runs, a number of sessions. No diagnosis, no supplements or medication; for worrying patterns
  (resting heart rate rising for days, very short sleep for a week) suggest rest and, if unwell, a
  clinician.
- Plain words. Explain a term the first time (HRV, cardiac drift, metres per heartbeat).

What to look at:

| Facts | What it tells you |
|---|---|
| day `sleep` | window, asleep vs in bed, stages and their share, source |
| day `heart_rate` | resting, day min-max, asleep vs awake average, `hourly_avg`, minutes with a reading |
| day `activity`, `workouts` | steps, distance, flights, active / total energy, exercise minutes; each workout's heart rate |
| day `compared_with_previous_28_days` | each measure: this day, average, range, difference, `days_lower` of `days_with_data` |
| period `summary`, `change_vs_previous_avg` | averages and totals, and their change against the period before |
| period `coverage.by_metric`, `best_and_worst` | how many days each metric has; the extreme days |
| workout `summary`, `heart_rate_from_minute_series` | duration (and elapsed), distance, pace, energy; average, max, first- vs second-half heart rate (`drift`) |
| workout `heart_rate_by_10_min`, `splits_km` | how effort built up; per-km pace and heart rate (only with usable route times) |
| workout `that_day`, `efficiency_m_per_beat` | sleep and resting heart rate that day; metres per heartbeat against the median of recent workouts of the type |

## Language

Write the report in the language the person asked for, or the one they wrote in. Set `"language"`
(`"en"`, `"vi"`, `"ja"`, ...).

- **English and Vietnamese are built in**: titles, chart labels, table headers, weekday and month
  names, number separators and workout types follow the language.
- **Other languages**: run `python3 tools/hka_report.py labels --lang en`, translate every value (keep
  the keys, the `{placeholders}`, the weekday list Monday first and the month lists January first; set
  `decimal_sep` / `thousands_sep`), and put the result in `"labels"`. The script warns about every
  label left in English.
- **Data notes**: translate the facts' `data_notes` into `"data_notes"`; without it the English notes
  are printed.
- **Fonts**: the default font (DejaVu Sans) covers Latin including Vietnamese, Greek and Cyrillic. For
  Chinese, Japanese, Korean, Thai, Devanagari and other scripts add `--font FILE.ttf` (and
  `--font-bold`), e.g. a Noto Sans `.ttf`. If the script stops with "has no glyphs for ...", pick a font
  that covers those characters; never drop or transliterate them. Right-to-left scripts are not laid
  out correctly by the PDF library: say so before making such a report.

## 3. Render and hand over

The same command as step 1, with `--narrative` instead of `--facts`:

```sh
python3 tools/hka_report.py day --date 2026-11-13 --person <who> --narrative /tmp/hka-report/narrative.json
python3 tools/hka_report.py workout <workout uuid> --narrative /tmp/hka-report/narrative.json
python3 tools/hka_report.py period --month 2026-10 --person <who> --narrative /tmp/hka-report/narrative.json
```

The PDF goes to `reports/<user>-day-<date>.pdf`, `reports/<user>-workout-<uuid>.pdf` or
`reports/<user>-period-<period>.pdf` in the repository (mode 600, ignored by git), or `$HKA_REPORT_DIR`;
`--out` sets another path. Tell the person the path and the headline. To check the layout,
`pdftoppm -r 60 -png <pdf> /tmp/hka-report/page` and look at the pages.

## Privacy

The PDF is personal health data. Keep it on this machine unless the person asks to share it. Only use
`--route` when asked.
