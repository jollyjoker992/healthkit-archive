# FHIR mapping (version 0.6.0)

How the processor's day folders (`users/<user>/days/YYYY/YYYY-MM-DD/`) become FHIR R4 resources. This is
the specification that `hka fhir map` implements (`cli/hka/fhir.py`). FHIR is a view: it can be rebuilt
from the day folders at any time, and the day folders (and the encrypted inbox behind them) stay the
source of truth.

The structure follows garmin-archive's mapping, so an agent that reads both archives sees the same
patterns: one NDJSON file per day, stable identifiers, conditional upserts, LOINC first and a documented
CodeSystem for the rest.

## Output

```sh
hka fhir map            # map days whose day folder, time zone or status changed, or every day after a mapping change
hka fhir map --force    # map every day again
hka fhir load           # load changed days into the FHIR server (Medplum)
```

| File (under `users/<user>/fhir/`) | Content |
|---|---|
| `days/YYYY/YYYY-MM-DD.ndjson` | All resources of one local day, one per line. |
| `shared.ndjson` | The Patient and one Device per data source. |
| `binaries/<sha256>.gpx` | Workout routes as GPX, uploaded by the loader as `Binary`. |
| `manifest.json` | Per day: source fingerprint, status, resource count, SHA-256 of the file; the mapping version and base; days that failed. |
| `load-manifest.json` | Per day: SHA-256 of what was loaded, the identifiers and Binary ids on the server, deletions still to do. |

A day is mapped again when its day folder changes (new, changed or deleted records), when its time zone
or `final`/`provisional` status changes, when `MAPPING_VERSION` or the base URL changes, or with
`--force`. A day that cannot be mapped is reported and listed under `failed`; the command exits 1. A day
whose records were all deleted is written as an empty file, and the loader removes its resources.

Resources have no `id`. References between them use the conditional form
`Type?identifier=<system>|<value>`; the server resolves them during the transaction.

## Files in this repository

| File | Content |
|---|---|
| [`cli/hka/terminology/CodeSystem-hka-metrics.json`](../cli/hka/terminology/CodeSystem-hka-metrics.json) | 11 codes in 6 groups for values with no correct LOINC code. Each has a definition, its UCUM unit, its FHIR value type and its source. |
| [`cli/hka/terminology/ValueSet-hka-metrics.json`](../cli/hka/terminology/ValueSet-hka-metrics.json) | Every selectable code of that CodeSystem. |
| [`cli/hka/terminology/CodeSystem-healthkit-type.json`](../cli/hka/terminology/CodeSystem-healthkit-type.json) | The 201 HealthKit type identifiers the app reads, with their kind and how each is mapped. |
| [`fhir/examples/`](../fhir/examples/) | One resource per pattern below, produced by the real mapper from made-up records. |

The terminology ships inside the CLI package, so `hka fhir load` finds it however the CLI is installed.
All four are generated, never edited by hand: `python3 tools/fhir_terminology.py` (from the tables in
`cli/hka/fhir.py` and the type lists in `ios/HKACore/Sources/HKACore/HealthKitSource.swift`) and
`python3 tools/fhir_examples.py`. CI runs both with `--check` and validates every file with Medplum's
validator (`tools/fhir-validate`, `@medplum/core` 5.2.17).

## Rules

1. **LOINC first, never a near miss.** A value uses LOINC only when the LOINC concept means the same
   thing. Every LOINC code below was checked against the NLM LOINC search API
   (clinicaltables.nlm.nih.gov) on 2026-11-14. Tempting codes that do not fit are listed under
   "Rejected LOINC codes".
2. **Everything else uses `hka-metrics`**:
   `https://github.com/jollyjoker992/healthkit-archive/fhir/CodeSystem/hka-metrics`.
3. **Every Observation also carries its HealthKit type** as a coding in
   `https://github.com/jollyjoker992/healthkit-archive/fhir/CodeSystem/healthkit-type` (code = Apple's
   identifier, e.g. `HKQuantityTypeIdentifierStepCount`). Types with no LOINC or hka-metrics code are
   coded with this alone, so nothing is mapped to a guessed code.
4. **Stable identifiers make loading idempotent.** Every resource has an `identifier` in
   `<base>/identifier/<kind>`; the loader upserts by it, so mapping a day again replaces its resources.
   Per-sample resources use the HealthKit UUID (unique across devices and users). Daily resources use
   `<user_id>/<day>/<name>`, so two family members never share one.
5. **Times carry the offset.** HealthKit gives UTC instants; the mapper writes them in the day's time zone
   (the zone the phone reported in the batch, or the sample's `HKMetadataKeyTimeZone`), e.g.
   `2026-11-12T08:30:00.000+07:00`. Daily resources span 00:00 to 24:00 local.
6. **Units are UCUM.** HealthKit unit strings are converted: fractions to `%` (×100), pounds and stone to
   `kg`, miles, yards and feet to `m` or `cm`, °F to `Cel`, kJ to `kcal` where the code expects kcal,
   glucose mmol/L to mg/dL (×18.015588). A value whose unit has no correct conversion is not mapped and is
   counted in the `hka fhir map` report (`unmapped_samples`); it stays in the day folder.
7. **One source per daily total.** iPhone and Apple Watch both count steps; adding them double counts.
   `summary.json` keeps one source per type (the configured priority, else the largest total), and
   `Observation.device` names it. The other sources stay in `summary.json` under `sources`.
8. **Provisional is preliminary.** A day still open for late data has `status: preliminary`; once final
   (no change for 7 days) it is `final`. A late batch reopens the day.
9. **Missing means absent.** No value, no Observation or component. Sleep with no asleep stage carries
   `dataAbsentReason: unknown` instead of a zero.
10. **Personal data.** The Patient carries the name the admin gave in the invite (pinned when the device
    was confirmed), sex and date of birth from HealthKit characteristics. Workout routes (GPS) are loaded
    as a `Binary` behind a DocumentReference, not in searchable resources. HealthKit metadata beyond what
    the tables use is not copied.

## Resources

| Resource | One per | Identifier (`<base>/identifier/…`) | Notes |
|---|---|---|---|
| `Patient` | family member | `hka-user` = user id (`u_…`) | `name.text`, `gender`, `birthDate`. |
| `Device` | data source per member | `hka-source` = `<user_id>/<bundle id>` | `deviceName` (user-friendly name, e.g. "Apple Watch"). Observations reference it in `device`. |
| `Observation` | see tables below | `observation` | `subject` = Patient. |
| `DocumentReference` | workout with a route | `document` = `route/<workout uuid>` | `content.attachment`: `application/gpx+xml`, size, SHA-1, `url` → Binary. |
| `Binary` | workout route | (by hash) | The GPX file. Reused while unchanged; replaced and the old one deleted when it changes. |
| `CodeSystem`, `ValueSet` | | `url` | The three terminology files, loaded with the data. |

Categories (`http://terminology.hl7.org/CodeSystem/observation-category`): `activity`, `vital-signs`,
`laboratory` (glucose) and `procedure` (ECG, audiogram) where they apply; none otherwise. No `meta.profile` is set
in this version; the vital-signs profiles add constraints the mapper does not check yet.

## Daily Observations

`effectivePeriod` = the local calendar day. Identifier: `<user_id>/<day>/<HealthKit type or name>`.
Value from `summary.json`. Example: [`Observation-steps.json`](../fhir/examples/Observation-steps.json).

| HealthKit type | Code | Unit | Category |
|---|---|---|---|
| `StepCount` | LOINC 41950-7 Number of steps in 24 hour Measured | `/d` | activity |
| `DistanceWalkingRunning` | LOINC 41953-1 Walking distance 24 hour Calculated | `m` | activity |
| `FlightsClimbed` | LOINC 93833-2 Flights climbed 24 hour | `{#}` | activity |
| `ActiveEnergyBurned` | `active-energy-daily` | `kcal` | activity |
| `BasalEnergyBurned` | `basal-energy-daily` | `kcal` | activity |
| `AppleExerciseTime` | `exercise-minutes-daily` | `min` | activity |
| `DistanceCycling` | `distance-cycling-daily` | `m` | activity |
| `HeartRate` minimum (name `hr-min`) | LOINC 8883-1 Heart rate 24 hour minimum | `/min` | vital-signs |
| `HeartRate` maximum (`hr-max`) | LOINC 8873-2 Heart rate 24 hour maximum | `/min` | vital-signs |
| `HeartRate` series (`hr-minutes`) | `hr-minute-mean`, `valueSampledData` | `/min` | none |

(Types are written without the `HKQuantityTypeIdentifier` prefix in these tables.)

## Per-sample Observations

One Observation per HealthKit sample; identifier = the sample's UUID; `effectiveDateTime` for an instant,
`effectivePeriod` for an interval; `device` = its source.

| HealthKit type | Code | Unit | Category | Example |
|---|---|---|---|---|
| `RestingHeartRate` | LOINC 40443-4 Heart rate --resting | `/min` | vital-signs | [resting-heart-rate](../fhir/examples/Observation-resting-heart-rate.json) |
| `HeartRateVariabilitySDNN` | LOINC 80404-7 R-R interval.standard deviation (Heart rate variability) | `ms` | vital-signs | [hrv-sdnn](../fhir/examples/Observation-hrv-sdnn.json) |
| `OxygenSaturation` | LOINC 2708-6 + 59408-5 (by pulse oximetry) | `%` | vital-signs | [spo2](../fhir/examples/Observation-spo2.json) |
| `RespiratoryRate` | LOINC 9279-1 Respiratory rate | `/min` | vital-signs | |
| `BodyTemperature` | LOINC 8310-5 Body temperature | `Cel` | vital-signs | |
| `BodyMass` | LOINC 29463-7 Body weight | `kg` | vital-signs | [weight](../fhir/examples/Observation-weight.json) |
| `Height` | LOINC 8302-2 Body height | `cm` | vital-signs | |
| `BodyMassIndex` | LOINC 39156-5 Body mass index (BMI) [Ratio] | `kg/m2` | vital-signs | |
| `BodyFatPercentage` | LOINC 41982-0 Percentage of body fat Measured | `%` | vital-signs | |
| `LeanBodyMass` | LOINC 91557-9 Lean body weight | `kg` | vital-signs | |
| `WaistCircumference` | LOINC 8280-0 Waist Circumference at umbilicus by Tape measure | `cm` | vital-signs | |
| `BloodGlucose` | LOINC 2339-0 Glucose [Mass/volume] in Blood | `mg/dL` | laboratory | [blood-glucose](../fhir/examples/Observation-blood-glucose.json) |
| `VO2Max` | `vo2-max` | `mL/(kg.min)` | none | [vo2-max](../fhir/examples/Observation-vo2-max.json) |
| Blood pressure correlation | LOINC 85354-9 panel; components 8480-6 systolic, 8462-4 diastolic | `mm[Hg]` | vital-signs | [blood-pressure](../fhir/examples/Observation-blood-pressure.json) |
| Any other quantity type | the HealthKit type only | UCUM of the sample | none | [dietary-water](../fhir/examples/Observation-dietary-water.json) |
| Any category type except sleep | the HealthKit type only; `valueCodeableConcept.text` = HealthKit value name | | none | [mindful-session](../fhir/examples/Observation-mindful-session.json) |

Blood pressure: the panel needs both members. A reading with only one is kept with the HealthKit
correlation code alone; standalone systolic or diastolic samples are not mapped again (they are the
correlation's members).

## Sleep

One Observation per night, on the day the night ends: sleep records belong to the sleep day running from 18:00 to 18:00 local time, named after its morning ([design §5.4](design.md)), so a night that starts before midnight is one Observation. Sleep ending from 18:00 (an evening nap) belongs to the next day's Observation and widens its `effectivePeriod`. Identifier `<user_id>/<day>/sleep`. Source: the
chosen sleep source in `summary.json` (one source, so a watch and a sleep app are not added together);
`effectivePeriod` runs from its first to its last record. Example:
[`Observation-sleep.json`](../fhir/examples/Observation-sleep.json).

| Part | Code | Unit | From (HealthKit sleep value) |
|---|---|---|---|
| value | LOINC 93832-4 Sleep duration | `min` | all asleep stages |
| component | LOINC 93831-6 Deep sleep duration | `min` | `asleepDeep` |
| component | LOINC 93830-8 Light sleep duration | `min` | `asleepCore` |
| component | LOINC 93829-0 REM sleep duration | `min` | `asleepREM` |
| component | LOINC 93828-2 Nighttime awakening duration | `min` | `awake` |
| component | `sleep-in-bed` | `min` | `inBed` |
| component | `sleep-unspecified` | `min` | `asleepUnspecified` |

Stage components also carry `code.text` with the HealthKit stage name. The per-interval timeline stays in
the day folder (`HKCategoryTypeIdentifierSleepAnalysis.ndjson`).

## Workouts and routes

One Observation per workout, on the day it starts. Identifier = workout UUID; `effectivePeriod` = start →
end; category `activity`. Example: [`Observation-workout.json`](../fhir/examples/Observation-workout.json).

| Part | Code | Unit | From |
|---|---|---|---|
| value | LOINC 55411-3 Exercise duration | `min` | `duration_s` |
| component | LOINC 73985-4 Exercise activity, `valueCodeableConcept.text` | | `activity_name` (e.g. `running`, `pickleball`) |
| component | LOINC 55425-3 mean heart rate | `/min` | stats HeartRate `avg` |
| component | LOINC 55426-1 maximum heart rate | `/min` | stats HeartRate `max` |
| component | LOINC 55424-6 calories burned | `kcal` | stats ActiveEnergyBurned `sum` |
| component | `workout-distance` | `m` | the largest distance statistic |

A workout with a route gets `derivedFrom` → a DocumentReference
([example](../fhir/examples/DocumentReference-workout-route.json)) whose attachment is the GPX track
(points with time and elevation). The loader uploads the file from `fhir/binaries/` as a `Binary` and
sets `attachment.url`; the extension `<base>/StructureDefinition/archive-path` names the local file.

## Electrocardiograms

One Observation per Apple Watch ECG, category `procedure`, coded with the HealthKit type
(`HKDataTypeIdentifierElectrocardiogram`). Example: [`Observation-ecg.json`](../fhir/examples/Observation-ecg.json).

| Component | Code | Value |
|---|---|---|
| classification | `ecg-classification` | text, e.g. `sinusRhythm` |
| voltage | `ecg-lead-i` | `valueSampledData` in `uV`, `period` = 1000 / sampling Hz ms |
| average heart rate | LOINC 8867-4 Heart rate | `/min` |

A recording whose parts are incomplete keeps classification and heart rate but no waveform.

## Audiograms

One Observation per audiogram (a hearing test: AirPods Pro's, or one entered from an audiologist's chart),
category `procedure`, coded LOINC 89015-2 Pure tone air conduction threshold audiometry panel with the
HealthKit type (`HKDataTypeIdentifierAudiogram`). It has no value of its own; each ear at each frequency
is a component, in dB HL (`unit` `dB HL`, UCUM code `dB`, LOINC's example unit). Example:
[`Observation-audiogram.json`](../fhir/examples/Observation-audiogram.json).

| Frequency | Left ear | Right ear |
|---|---|---|
| 250 Hz | 91375-6 | 91374-9 |
| 500 Hz | 89024-4 | 89025-1 |
| 750 Hz | 91379-8 | 91378-0 |
| 1000 Hz | 89016-0 | 89017-8 |
| 1500 Hz | 91373-1 | 91372-3 |
| 2000 Hz | 89018-6 | 89019-4 |
| 3000 Hz | 89020-2 | 89021-0 |
| 4000 Hz | 89022-8 | 89023-6 |
| 5000 Hz | 91377-2 | 91376-4 |
| 6000 Hz | 89026-9 | 89027-7 |
| 8000 Hz | 89028-5 | 89029-3 |

All are LOINC "Hearing threshold Ear - left|right --<n> Hz" (class AUDIO, checked ACTIVE on 2026-11-16).

- **Which value.** With per-test data (iOS 18.1+, `tests`), only an air-conduction test is used, since the
  panel is air conduction; a bone-conduction test is not mapped. When one ear at one frequency has both a
  masked and an unmasked test, the masked threshold is used (the valid one when masking was needed).
  Without `tests`, the point's `left_db_hl` / `right_db_hl` is used.
- **Bounds, not measurements.** A test can only play sounds within its clamping range. A threshold at the
  lower bound means the ear heard the quietest sound the test could play: the real threshold is at or
  below it, so the Quantity carries `comparator` `<=`. At the upper bound it carries `>=`. AirPods tests
  of normal hearing are mostly at the lower bound; read them as "at or better than", never as exact.
- **Masking.** With per-test data, the component has the extension
  `<base>/StructureDefinition/audiogram-masked` (`valueBoolean`). Without it, masking is unknown and the
  extension is absent.
- **Not mapped:** frequencies without a LOINC code (below 250 Hz, 7000 Hz, above 8000 Hz) and
  bone-conduction tests; they stay in the day folder and count in `unmapped_samples`. An audiogram with
  no mappable threshold has no Observation.

## Time series

`valueSampledData` needs evenly spaced values, so:

| Series | Encoding |
|---|---|
| Heart rate (`hr-minute-mean`) | One slot per minute of the local day: 1440, or 1380/1500 on daylight-saving days. Each slot is the mean of all samples (every source) starting in that minute, one decimal; `E` where there is none. `origin` 0 `/min`, `period` 60000. [Example](../fhir/examples/Observation-heart-rate-minutes.json). |
| ECG voltage | The recording's own sampling rate (`period` = 1000 / Hz ms). `E` for a missing value. |

The raw heart-rate samples (with their exact times and sources) stay in the day folder.

## Custom codes: `hka-metrics`

System `https://github.com/jollyjoker992/healthkit-archive/fhir/CodeSystem/hka-metrics`, version = mapping
version. The full definitions are in the CodeSystem (`CodeSystem/$lookup` on the server).

| Group | Code | Display | Unit | Value type |
|---|---|---|---|---|
| Daily activity | `active-energy-daily` | Active energy burned, day total | `kcal` | Quantity |
| Daily activity | `basal-energy-daily` | Basal energy burned, day total | `kcal` | Quantity |
| Daily activity | `exercise-minutes-daily` | Exercise minutes, day total | `min` | Quantity |
| Daily activity | `distance-cycling-daily` | Cycling distance, day total | `m` | Quantity |
| Heart | `hr-minute-mean` | Heart rate, 1-minute means | `/min` | SampledData |
| Sleep | `sleep-in-bed` | Time in bed | `min` | Quantity |
| Sleep | `sleep-unspecified` | Asleep, stage not specified | `min` | Quantity |
| Workout | `workout-distance` | Workout distance | `m` | Quantity |
| Electrocardiogram | `ecg-lead-i` | ECG voltage, lead I (Apple Watch) | `uV` | SampledData |
| Electrocardiogram | `ecg-classification` | ECG classification | | CodeableConcept (text) |
| Fitness | `vo2-max` | VO2 max | `mL/(kg.min)` | Quantity |

## HealthKit types: `healthkit-type`

System `https://github.com/jollyjoker992/healthkit-archive/fhir/CodeSystem/healthkit-type`. Codes are
Apple's identifiers, exactly as HealthKit spells them. Each concept has a readable `display` and two
properties: `kind` (`quantity`, `category`, `correlation`, `workout`, `data`) and `mapping`, which
states how that type appears in FHIR. For example:

```
GET /fhir/R4/CodeSystem/$lookup?system=https://github.com/jollyjoker992/healthkit-archive/fhir/CodeSystem/healthkit-type&code=HKQuantityTypeIdentifierStepCount
→ display "Step count"; mapping "Daily total (one source) as one Observation per day: LOINC 41950-7 in /d."
```

The meaning of each type is Apple's (developer.apple.com/documentation/healthkit). Values keep the unit
the sample was recorded in, converted to UCUM; check `valueQuantity.code` rather than assuming a unit.

## Loading

```sh
export HKA_FHIR_URL=http://localhost:8203 HKA_FHIR_CLIENT_ID=… HKA_FHIR_CLIENT_SECRET=…   # from medplum/setup.py
hka fhir map && hka fhir load
```

Uses the loader client (OAuth2 client credentials). `medplum/` runs a local Medplum; see
[`medplum/README.md`](../medplum/README.md).

1. Puts the three terminology resources on the server when they changed (compared by content hash).
2. Loads `shared.ndjson` (Patient, Devices), then each changed day, as one FHIR transaction of
   conditional updates (`PUT Observation?identifier=…`). The server resolves the conditional references.
3. Before a day's transaction, uploads each route GPX as a `Binary` and sets `attachment.url`. An
   unchanged file reuses its Binary; if the transaction fails, new uploads are removed.
4. Deletes resources the day had on the server that its new mapping no longer produces (a sample deleted
   in Health, a day emptied), by conditional delete on the identifier. Deletions not yet done are kept in
   `load-manifest.json` and retried on the next load without blocking other days; an identifier that has
   moved to another day (a route re-filed with its workout) is not deleted.
5. A day whose file is unchanged is skipped. When the server URL changes, everything is loaded again.
6. After a mapping-version or base change, days the new mapping no longer produces are removed, except
   days whose mapping failed in that run: they stay until they map again.

Medplum limits FHIR "points" per client per minute. A transaction with `429` entries waits 60 s and is
sent again, up to 10 times; resending is safe because every entry is an upsert. HTTP 429/5xx and network
errors are retried with backoff; a 401 gets one new token. A `DELETE` answered 200, 204, 404 or 410
counts as done.

## Querying the loaded data

Days are local calendar days, and every time carries its offset. Medplum reads a bare date in a search as
UTC, so give the offset of the member's time zone:

```
Observation?code=http://loinc.org|41950-7&date=ge2026-11-07T00:00:00%2B07:00&date=lt2026-11-14T00:00:00%2B07:00&_sort=date
Observation?code=https://github.com/jollyjoker992/healthkit-archive/fhir/CodeSystem/healthkit-type|HKQuantityTypeIdentifierDietaryWater&_sort=-date
Observation?code=https://github.com/jollyjoker992/healthkit-archive/fhir/CodeSystem/hka-metrics|vo2-max&_sort=-date&_count=10
Observation?subject:Patient.identifier=https://github.com/jollyjoker992/healthkit-archive/fhir/identifier/hka-user|u_…&code=http://loinc.org|93832-4
CodeSystem/$lookup?system=https://github.com/jollyjoker992/healthkit-archive/fhir/CodeSystem/hka-metrics&code=sleep-in-bed
```

To find everything of one HealthKit type, search by its `healthkit-type` code: every Observation carries
one, including those that also have LOINC.

## Rejected LOINC codes

| Value | Tempting LOINC code | Why not |
|---|---|---|
| Heart-rate series | 8867-4 Heart rate | Correct concept, but the vital-signs heart-rate profile allows only a Quantity; a 1-minute series is SampledData. 8867-4 is used for single values (ECG average). |
| Active energy, day total | 41979-6 Calories burned in 24 hour | Total calories (active + resting); HealthKit's value is active only. |
| Basal energy, day total | 69429-9 Metabolic rate --resting | A rate, and a measurement; HealthKit's value is an estimated daily total. |
| Exercise minutes | 77592-4 / 77593-2 Moderate / vigorous physical activity [IPAQ] | Questionnaire items split by intensity; Apple's minutes are one sensor-based total. |
| VO2 max | 60842-2 Oxygen consumption (VO2) | VO2 at a moment, not the maximum. |

## Known approximations

- LOINC 55424-6, 55425-3 and 55426-1 say "by Pedometer". The HL7 Physical Activity guide uses them for
  wearable workouts, and garmin-archive does too, so agents see the same codes across both archives.
- LOINC 55424-6 (calories burned) does not say whether resting energy is included; the value mapped to it
  is the workout's *active* energy (HealthKit `ActiveEnergyBurned`), never active plus basal.
- LOINC 41953-1 is named "Walking distance"; HealthKit's `DistanceWalkingRunning` includes running. It
  is the closest daily distance code, and garmin-archive uses it the same way.
- LOINC 8280-0 names the umbilicus site and tape method, which HealthKit does not record; there is no
  method-free code.
- LOINC 2339-0 is "in Blood"; continuous glucose monitors measure interstitial fluid (99504-3) and
  meters capillary blood (41653-7), but HealthKit does not record the specimen.
- LOINC 80404-7 is SDNN; Apple's `HeartRateVariabilitySDNN` is SDNN, so it fits (garmin-archive rejects it
  because Garmin reports RMSSD).
- LOINC 89015-2 is the *air conduction* panel. HealthKit audiograms without per-test data (before iOS
  18.1) do not say the conduction; AirPods tests and charts entered by hand are air conduction, so they are
  mapped to it. The hearing-threshold codes themselves name no method.
- The heart-rate series pools all sources and uses each sample's value; the individual readings of a
  series sample (`series`) stay in the day folder.

## Not mapped (kept in the day folders)

| Data | Reason |
|---|---|
| Heartbeat series, State of Mind, GAD-7 and PHQ-9 assessments (`object` records) | Not mapped yet; the day folder has them in full. |
| Audiogram thresholds without a LOINC code, bone-conduction tests | See [Audiograms](#audiograms). |
| Food correlations | Their nutrient samples are mapped one by one. |
| Standalone blood-pressure systolic / diastolic samples | They are the correlation's members, mapped there. |
| Routes without their workout | No workout to attach them to. |
| Sleep timeline (per interval) | The night's totals are mapped; the intervals stay in the day folder. |
| Census and header records | Bookkeeping for the processor, not measurements. |
| HealthKit metadata beyond the tables, workout events and sub-activities | Not needed yet; kept in the day folder. |
| Values whose unit cannot be converted correctly | Counted in `unmapped_samples`. |

## Changing the mapping

1. Change `cli/hka/fhir.py`: a new code goes into `METRICS` (with group, display, unit, value type,
   source and definition); a new LOINC code into `LOINC`, checked against the NLM search API first.
2. Bump `MAPPING_VERSION`. The CodeSystem and ValueSet carry the same version.
3. Run `python3 tools/fhir_terminology.py` and `python3 tools/fhir_examples.py`, and validate:
   `cd tools/fhir-validate && npm ci && node validate.mjs ../../fhir ../../cli/hka/terminology`.
4. Update this document. The test suite checks that every hka-metrics code and every LOINC code in the
   mapper appears here.
5. After deployment, `hka fhir map` maps every day again and `hka fhir load` replaces what changed.

## Decisions

1. **Base URL:** `https://github.com/jollyjoker992/healthkit-archive/fhir`, configurable with
   `HKA_FHIR_BASE`. It must not change once data is loaded (identifiers are built from it).
2. **Routes:** GPX in a `Binary`, behind a DocumentReference, not in searchable resources.
3. **Patient name:** the name the admin gave in the invite.
4. **Durations:** minutes.
