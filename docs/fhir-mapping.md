# FHIR mapping (version 0.3.0)

`hka fhir map` turns each user's day folders into FHIR R4 resources; `hka fhir load` upserts them into a
FHIR server (Medplum, client credentials). Implementation: `cli/hka/fhir.py`. The structure follows
garmin-archive's mapping: one NDJSON file per day, stable identifiers, conditional upserts, and a
manifest that remaps a day only when its content hash or the mapping version changes.

FHIR is derived data. The day folders (and the encrypted inbox behind them) stay the source of truth.

## Output

```
users/<user>/fhir/days/YYYY/YYYY-MM-DD.ndjson   resources of the local day
users/<user>/fhir/shared.ndjson                  Patient, Device (one per HealthKit source)
users/<user>/fhir/manifest.json                  per day: source hash, mapping version, output hash
users/<user>/fhir/load-manifest.json             per batch: hash and identifiers on the server
```

## Rules

1. Base URL `HKA_FHIR_BASE`, default `https://github.com/jollyjoker992/healthkit-archive/fhir`. It must
   not change once data is loaded (identifiers are built from it).
2. Identifiers: `{base}/identifier/observation` with the HealthKit UUID for per-sample resources, and
   `<user_id>/<day>/<name>` for daily resources (unique per family member); `{base}/identifier/hka-user` for the Patient;
   `{base}/identifier/hka-source` (`<user_id>/<bundle id>`) for Devices; `{base}/identifier/document` for routes.
3. Every Observation carries the HealthKit type identifier as a coding in
   `{base}/CodeSystem/healthkit-type`, so it round-trips to its source type. LOINC is added only where a
   correct code exists (table below, each code checked against NLM LOINC). Values with no correct LOINC
   code use `{base}/CodeSystem/hka-metrics`. Both CodeSystems are loaded with the data.
4. Units are UCUM. HealthKit percentages are fractions and are multiplied by 100; pounds, stone, miles,
   feet, inches, °F, kJ and mmol/L glucose are converted to the unit the LOINC code expects. A sample
   whose unit has no correct conversion is not mapped and is counted in the `hka fhir map` report.
5. Times are written in the day's time zone (the zone the phone reported when it read that day).
6. Daily totals use one source per type (`summary.json`); `Observation.device` names that source.
7. `status` is `final` for final days and `preliminary` for provisional days, whose values can change.
8. A day whose records were all deleted is mapped as an empty file, and the loader deletes its resources.
   After a mapping-version or base change, batches the new mapping no longer produces are deleted,
   except days whose mapping failed in that run: they stay on the server until they map again. A delete
   the server refuses is kept and retried on the next load without blocking other batches.

## Observations

| What | Code | Value | Notes |
|---|---|---|---|
| Steps, day | LOINC 41950-7 | `/d` | summary total |
| Walking + running distance, day | LOINC 41953-1 | `m` | |
| Flights climbed, day | LOINC 93833-2 | `{#}` | |
| Active / basal energy, exercise minutes, cycling distance, day | hka-metrics | `kcal`, `min`, `m` | |
| Heart rate | hka-metrics `hr-minute-mean` (+ HealthKit type) | `valueSampledData`, period 60000 ms, one slot per minute of the local day (1380/1440/1500) | mean per minute; `E` where none. Not coded 8867-4/vital-signs: that profile allows only a Quantity |
| Heart rate min / max, day | LOINC 8883-1 / 8873-2 | `/min` | |
| Resting heart rate | LOINC 40443-4 | `/min` | per sample |
| HRV | LOINC 80404-7 | `ms` | Apple reports SDNN, which is what this code means |
| SpO2 | LOINC 2708-6 + 59408-5 | `%` | |
| Respiratory rate, body temperature | LOINC 9279-1, 8310-5 | `/min`, `Cel` | |
| Weight, height, BMI, body fat, lean mass, waist | LOINC 29463-7, 8302-2, 39156-5, 41982-0, 91557-9, 8280-0 | `kg`, `cm`, `kg/m2`, `%`, `kg`, `cm` | |
| Blood glucose | LOINC 2339-0 | `mg/dL` | mmol/L samples are converted (×18.015588) |
| Blood pressure | LOINC 85354-9, components 8480-6 / 8462-4 | `mm[Hg]` | from the correlation, only with both members; standalone systolic/diastolic samples are not mapped again |
| VO2 max | hka-metrics `vo2-max` | `mL/(kg.min)` | LOINC 60842-2 is VO2, not VO2 max |
| Sleep, night ending on the day | LOINC 93832-4; components 93831-6 deep, 93830-8 core (light), 93829-0 REM, 93828-2 awake; hka-metrics in bed, unspecified | `min` | from the chosen source (period from its records); `dataAbsentReason` when it has no asleep stage; stage timeline stays in the day folder |
| Workout | LOINC 55411-3 duration; components 73985-4 activity (text, e.g. `pickleball`), 55425-3 mean HR, 55426-1 max HR, 55424-6 energy, hka-metrics distance | `min` | `derivedFrom` the route document |
| Workout route | `DocumentReference` → `Binary` (GPX, uploaded by the loader from `fhir/binaries/`) | | kept out of searchable resources |
| ECG | HealthKit type; components hka-metrics classification (text), lead I `valueSampledData`, average heart rate (8867-4) | `uV` | period = 1000 / sampling Hz ms; no waveform if a part is missing |
| Other quantity types (nutrition, mobility, audio, …) | HealthKit type only | UCUM of the sample | |
| Other category types (mindful sessions, symptoms, cycle tracking, events) | HealthKit type only | `valueCodeableConcept.text` | |

## Known approximations

- LOINC 55424-6, 55425-3 and 55426-1 say "by Pedometer". The HL7 Physical Activity guide uses them for
  wearable workouts, and garmin-archive does too, so agents see the same codes across both archives.
- LOINC 8280-0 names the umbilicus site and tape method, which HealthKit does not record; there is no
  method-free code.
- LOINC 2339-0 is "in Blood"; continuous glucose monitors measure interstitial fluid (99504-3) and meters
  capillary blood (41653-7), but HealthKit does not record the specimen.
- The per-minute heart-rate series pools all sources and ignores multi-sample `series` details.

## Not mapped (kept in the day folders)

Heartbeat series, audiograms and other `object` records, food correlations (their nutrient samples are
mapped individually), routes without their workout, the per-interval sleep timeline, census and header
records, and HealthKit metadata beyond what the table uses.

## Loading

```sh
export HKA_FHIR_URL=http://localhost:8103 HKA_FHIR_CLIENT_ID=… HKA_FHIR_CLIENT_SECRET=…
hka fhir map && hka fhir load
```

Each batch is one transaction of conditional `PUT Type?identifier=system|value`; a batch whose hash is
unchanged is skipped, and identifiers a day no longer produces (for example a sample deleted in Health)
are deleted from the server.

Validation: `cd tools/fhir-validate && npm ci && node validate.mjs <dir>` uses Medplum's validator; the
test suite runs it on a mapped day.
