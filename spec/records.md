# Batch plaintext records, version 1

A batch's plaintext is a gzip stream of NDJSON: one JSON object per line,
UTF-8. Every line, including the last, ends with `\n`; there are no blank
lines. Every object has a `k` (kind) field. Receivers MUST keep records of
unknown kinds and unknown fields verbatim; they MUST NOT fail on them.

Common conventions:
- Instants are as in the protocol spec: UTC, `Z`, three fractional digits,
  truncated (not rounded) from HealthKit's `Date`.
- `uuid` is the HealthKit object's UUID, uppercase with hyphens, exactly as
  `UUID().uuidString` prints it. HealthKit never reuses a UUID and never
  modifies a stored object; it can only delete it.
- `type` is the HealthKit type identifier string, for example
  `HKQuantityTypeIdentifierHeartRate`, `HKCategoryTypeIdentifierSleepAnalysis`,
  `HKCorrelationTypeIdentifierBloodPressure`, `HKWorkoutTypeIdentifier`.
- Numbers are JSON numbers. `NaN` and infinities are not allowed; such values
  are omitted.
- `metadata` is the HealthKit metadata dictionary with keys as Apple names
  them. Values keep their type: strings and numbers as JSON strings and
  numbers, booleans as JSON booleans, dates as `{"date":"<instant>"}`,
  `HKQuantity` as `{"quantity":<number>,"unit":"<HK unit string>"}`. Any other
  value is `{"unsupported":"<type name>"}`.

## Header (first line, exactly one)

```json
{"k":"header","v":1,"user_id":"u_…","device_id":"d_…","created_at":"…",
 "tz":"Asia/Ho_Chi_Minh","app_version":"1.0 (12)","os_version":"iOS 26.1",
 "reason":"sync","counts":{"HKQuantityTypeIdentifierHeartRate":1830}}
```

- `tz` is the device's IANA time zone when the batch was built.
- `reason` is one of `sync` (anchored changes), `resend` (daily
  reconciliation window), `backfill` (history re-send), `census`.
- `counts` is, per `type`, the number of objects in this batch that the census table below would count (each ECG or heartbeat series once, not per part; correlations once, not their members; `deleted`, `route`, `object`, `profile` and `census` records not counted). Types with zero are omitted.

## sample

One HealthKit sample (quantity, category or correlation).

```json
{"k":"sample","kind":"quantity","type":"HKQuantityTypeIdentifierHeartRate",
 "uuid":"6F1C…","start":"…","end":"…","value":72,"unit":"count/min",
 "source":{"bundle_id":"com.apple.health.…","name":"Apple Watch","version":"26.1",
           "product_type":"Watch7,1","os_version":"26.1.0"},
 "device":{"name":"Apple Watch","manufacturer":"Apple Inc.","model":"Watch",
           "hardware_version":"Watch7,1","software_version":"26.1",
           "local_identifier":null,"udi":null},
 "metadata":{"HKMetadataKeyHeartRateMotionContext":1}}
```

| Field | Notes |
|---|---|
| `kind` | `quantity`, `category` or `correlation` |
| `value` | quantity: number in `unit`; category: the integer raw value |
| `unit` | quantity only: the unit string used for `value`, normally the type's preferred unit (`HKUnit.unitString`) |
| `value_name` | category only, optional: the case name for known enums, for example `asleepREM` |
| `count` | quantity only, optional: `HKQuantitySample.count` when greater than 1 (the sample aggregates a series) |
| `series` | quantity only, optional: when `count` > 1, the individual quantities from `HKQuantitySeriesSampleQuery` as `[[start, end, value], …]` (instants and numbers in `unit`) |
| `stats` | quantity only, optional: for `HKDiscreteQuantitySample` with `count` > 1, `{"min","max","avg","most_recent","most_recent_start","most_recent_end"}` |
| `objects` | correlation only: array of member `sample` records (without `k`) |
| `source` | `HKSourceRevision`; `bundle_id` and `name` are required, `version`, `product_type`, `os_version` optional |
| `device` | `HKDevice` or `null`: `name`, `manufacturer`, `model`, `hardware_version`, `firmware_version`, `software_version`, `local_identifier`, `udi`, each optional |

## workout

```json
{"k":"workout","type":"HKWorkoutTypeIdentifier","uuid":"…","start":"…","end":"…",
 "activity_type":37,"activity_name":"running","duration_s":1805.2,
 "stats":{"HKQuantityTypeIdentifierHeartRate":{"unit":"count/min","avg":148,"min":102,"max":171},
          "HKQuantityTypeIdentifierActiveEnergyBurned":{"unit":"kcal","sum":402.1}},
 "events":[{"type":"lap","start":"…","end":"…","metadata":{}}],
 "activities":[{"uuid":"…","activity_type":37,"start":"…","end":"…","stats":{},"metadata":{}}],
 "source":{…},"device":{…},"metadata":{…}}
```

`activity_type` is the `HKWorkoutActivityType` raw value; `activity_name` the
case name when known (`pickleball`, `running`, …). Heart-rate and other samples
recorded during a workout are separate `sample` records; processors relate them
to the workout by time window and source (the explicit HealthKit association is
not exported in version 1).

## route

A workout route (`HKWorkoutRoute`), split into parts of at most 5,000 points.

```json
{"k":"route","uuid":"…","workout_uuid":"…","part":0,"parts":3,"start":"…","end":"…",
 "points":[[1794556800123,10.7769,106.7009,12.5,4.0,3.0,2.9,181.2,0.5,10.0]]}
```

Deleting a workout deletes its routes: processors remove records whose `workout_uuid` is a deleted
uuid. A route saved after its workout was read reaches the processor through the daily re-send
window (48 hours), not through anchored changes, in version 1.

`start` and `end` are those of the whole route (the `HKWorkoutRoute` sample),
repeated in every part. Each point is `[epoch_ms, latitude, longitude,
altitude_m, horizontal_accuracy_m, vertical_accuracy_m, speed_mps, course_deg,
speed_accuracy_mps, course_accuracy_deg]`; unavailable values are `null`.

## ecg

```json
{"k":"ecg","uuid":"…","type":"HKDataTypeIdentifierElectrocardiogram","start":"…","end":"…",
 "classification":"sinusRhythm","avg_hr":64,"sampling_hz":512,"symptoms":"notSet",
 "part":0,"parts":1,"voltages_uv":[12.1,13.4],"source":{…},"device":{…},"metadata":{…}}
```

ECG voltages are split into parts of at most 5,000 measurements, in order; a measurement without a
Lead I value is `null`, so positions stay aligned with the sampling frequency.

A workout, ECG or heartbeat record whose series could not be read carries `"series_error":"<text>"`
and an empty series; a quantity sample whose value has no compatible unit carries
`"unit_error":"<text>"` with `value` and `unit` null. Both are kept rather than dropped.

## heartbeats

`HKHeartbeatSeriesSample`, split into parts of at most 5,000 beats.

```json
{"k":"heartbeats","uuid":"…","type":"HKDataTypeIdentifierHeartbeatSeries","start":"…","end":"…",
 "part":0,"parts":1,"beats":[[0.0,false],[0.81,false]],"source":{…},"device":{…},"metadata":{…}}
```

Each beat is `[seconds_since_start, preceded_by_gap]`.

## object

Any other HealthKit object (audiogram, state of mind, medication dose event,
vision prescription, clinical record, …), kept generically.

```json
{"k":"object","class":"HKAudiogramSample","type":"…","uuid":"…","start":"…","end":"…",
 "data":{…},"source":{…},"device":{…},"metadata":{…}}
```

`data` holds the class-specific properties, named after the Swift property
names in snake_case; the app's serializer for each class is the reference.
Files in `HKAttachmentStore` (for example vision prescription images) are not
exported in version 1.

## deleted

```json
{"k":"deleted","uuid":"…","type":"HKQuantityTypeIdentifierHeartRate","observed_at":"…"}
```

One per `HKDeletedObject` returned by an anchored query.

## profile

Characteristics, sent when they change and at least once per week.

```json
{"k":"profile","observed_at":"…","date_of_birth":"1990-01-31","biological_sex":"male",
 "blood_type":"oPositive","fitzpatrick_skin_type":null,"wheelchair_use":"no",
 "activity_move_mode":"activeEnergy"}
```

## census

Counts the device holds, so the processor can find what it is missing.

```json
{"k":"census","type":"HKQuantityTypeIdentifierHeartRate","day":"2026-11-12",
 "tz":"Asia/Ho_Chi_Minh","count":1830,"uuid_sha256":"<hex>"}
```

`day` is a local calendar date in `tz`; the census covers objects of `type`
whose `start` falls in it, as currently stored in HealthKit (deleted objects
are not counted). Counted objects per `type`:

| `type` | Counted |
|---|---|
| quantity and category types | top-level samples of that type |
| correlation types | the correlations themselves, not their members (members also count under their own type only if HealthKit returns them as standalone samples of that type) |
| `HKWorkoutTypeIdentifier` | workouts |
| `HKDataTypeIdentifierElectrocardiogram`, `HKDataTypeIdentifierHeartbeatSeries` | the samples, not their parts |

Routes and `object` records are not counted. The app sends a census only for
types the member has enabled. `uuid_sha256` is hex SHA-256 of the UUIDs sorted
ascending and joined with `\n` (no trailing newline).

Processors compare a census with their own index, recomputing the day of each
object from its `start` in the census `tz`; they do not compare it with
day-folder counts, which use different day rules.

## From HealthKit to records

How the app builds each record from what HealthKit returns. This section describes the reference
serializer (`ios/HKACore/Sources/HKACore/HealthKitSource.swift`) as of version 1; the sections above
are the format, and a field listed there but marked "not produced" below may appear from other or later
writers.

### Queries

| Record | HealthKit query |
|---|---|
| `sample`, `workout`, `ecg`, `heartbeats`, `object`, `deleted` | `HKAnchoredObjectQuery` per type, from the last committed anchor (reason `sync`, `backfill`); `HKSampleQuery` on start date for the re-send window (reason `resend`) |
| `route` | `HKSampleQuery` for `HKSeriesType.workoutRoute()` with `predicateForObjects(from: workout)`, then `HKWorkoutRouteQuery` for the points |
| `ecg` voltages | `HKElectrocardiogramQuery` |
| `heartbeats` beats | `HKHeartbeatSeriesQuery` |
| `profile` | `HKHealthStore` characteristic methods (`dateOfBirthComponents()`, `biologicalSex()`, …) |
| `census` | `HKSampleQuery` per type over the last 30 local days |

### Fields on every sample-based record

| Field | From |
|---|---|
| `uuid` | `HKObject.uuid.uuidString` |
| `type` | `HKSample.sampleType.identifier` |
| `start`, `end` | `startDate`, `endDate`, truncated to milliseconds |
| `source.bundle_id`, `source.name` | `sourceRevision.source.bundleIdentifier`, `.name` |
| `source.version`, `source.product_type` | `sourceRevision.version`, `.productType`, when present |
| `source.os_version` | `sourceRevision.operatingSystemVersion` as `major.minor.patch` (always present) |
| `device` | `HKDevice` properties `name`, `manufacturer`, `model`, `hardwareVersion`, `firmwareVersion`, `softwareVersion`, `localIdentifier`, `udiDeviceIdentifier`; `null` when the sample has no device |
| `metadata` | `HKObject.metadata`, converted value by value (below) |

Metadata values: `String` as is; `Date` as `{"date":…}`; `NSNumber` booleans as `true`/`false`, other
numbers as numbers (non-finite dropped); `HKQuantity` as `{"quantity","unit"}` in the first compatible
unit of `count`, `m`, `s`, `kcal`, `degC`, `%`, `count/min`, `kg`, `dBASPL`, otherwise
`{"unsupported":"HKQuantity"}`; anything else (arrays, dictionaries, other classes) as
`{"unsupported":"<Swift type name>"}`.

### sample

| Field | From |
|---|---|
| `kind` | `HKQuantitySample` → `quantity`, `HKCategorySample` → `category`, `HKCorrelation` → `correlation` |
| `unit` | The member's preferred unit for the type (`HKHealthStore.preferredUnits(for:)`, which follows the phone's region and Health settings, for example `lb` or `kg`); if that is unavailable or incompatible, the first compatible unit from a fixed list (`count`, `count/min`, `m`, `kcal`, `s`, `%`, `kg`, `degC`, `mmHg`, `mL`, `W`, `dBASPL`, `ms`, `m/s`, `S`, `Hz`, `mg/dL`, `IU`, `mL/kg·min`, `L/min`, `kcal/kg·hr`, `lx`, and Apple's effort score on iOS 18+) |
| `value` | quantity: `quantity.doubleValue(for: unit)`; category: `HKCategorySample.value` (the raw integer) |
| `value_name` | `HKCategoryTypeIdentifierSleepAnalysis` only: `inBed`, `asleepUnspecified`, `awake`, `asleepCore`, `asleepDeep`, `asleepREM`. Other category types carry only the integer; its meaning is the type's HealthKit enum (`HKCategoryValue…`) |
| `count` | `HKQuantitySample.count` when greater than 1 |
| `stats` | `HKDiscreteQuantitySample` with `count` > 1: `minimumQuantity`, `maximumQuantity`, `averageQuantity`, `mostRecentQuantity`, `mostRecentQuantityDateInterval`, in `unit` |
| `series` | Not produced in version 1: a sample aggregating a series is sent with `count` and its total or `stats` only |
| `objects` | `HKCorrelation.objects`, quantity members only, each serialized as a quantity `sample` without `k` |

### workout

| Field | From |
|---|---|
| `activity_type` | `HKWorkout.workoutActivityType.rawValue` |
| `activity_name` | The case name for about 30 common types (running, walking, cycling, swimming, hiking, pickleball, tennis, badminton, strength training, yoga, …), otherwise `null`; `activity_type` is always set |
| `duration_s` | `HKWorkout.duration` |
| `stats` | `HKWorkout.allStatistics`, per quantity type: `sumQuantity`, `averageQuantity`, `minimumQuantity`, `maximumQuantity` where HealthKit provides them, in the type's `unit` |
| `events` | `workoutEvents`: type name (`pause`, `resume`, `lap`, `marker`, `motionPaused`, `motionResumed`, `segment`, `pauseOrResumeRequest`), `dateInterval`, metadata |
| `activities` | `workoutActivities` (iOS 16+, multisport and interval workouts): `uuid`, `workoutConfiguration.activityType.rawValue`, dates, `allStatistics`, metadata |
| `series_error` | Set when the workout's routes could not be read; the workout is sent without routes |

### route

One record per part of each `HKWorkoutRoute` of the workout, sent right after the workout record.
Each point is a `CLLocation`: `timestamp` (epoch ms, truncated), `coordinate.latitude`,
`coordinate.longitude`, `altitude`, `horizontalAccuracy`, `verticalAccuracy`, `speed`, `course`,
`speedAccuracy`, `courseAccuracy`. A value whose accuracy (or itself, for speed and course) is negative,
which Core Location uses for "invalid", is `null`; altitude is `null` when `verticalAccuracy` is negative.

### ecg

| Field | From |
|---|---|
| `classification` | `HKElectrocardiogram.classification` case name (`sinusRhythm`, `atrialFibrillation`, `inconclusiveLowHeartRate`, `inconclusiveHighHeartRate`, `inconclusivePoorReading`, `inconclusiveOther`, `unrecognized`, `notSet`) |
| `avg_hr` | `averageHeartRate` in count/min, or `null` |
| `sampling_hz` | `samplingFrequency` in Hz, or `null` |
| `symptoms` | `symptomsStatus`: `notSet`, `none`, `present`. The symptoms themselves are separate category samples in Health |
| `voltages_uv` | Each `HKElectrocardiogram.VoltageMeasurement`'s `appleWatchSimilarToLeadI` value in µV, one entry per measurement |

### heartbeats

Each beat is one callback of `HKHeartbeatSeriesQuery`: `[timeSinceSeriesStart, precededByGap]`.

### object

Version 1 uses `object` only for `HKAudiogramSample`, and sends `class`, the common fields and an empty
`data`: the hearing thresholds (`sensitivityPoints`) are not exported yet.

### deleted

`HKDeletedObject.uuid` from the anchored query; `type` is the type queried, and `observed_at` is when
the app read the deletion (HealthKit does not say when it happened). Deleted object metadata is not
sent.

### profile

| Field | From | Values |
|---|---|---|
| `date_of_birth` | `dateOfBirthComponents()` | `YYYY-MM-DD`; omitted if unset |
| `biological_sex` | `biologicalSex()` | `notSet`, `female`, `male`, `other` |
| `blood_type` | `bloodType()` | `notSet`, `aPositive` … `oNegative` |
| `fitzpatrick_skin_type` | `fitzpatrickSkinType()` | `notSet`, `I` … `VI` |
| `wheelchair_use` | `wheelchairUse()` | `notSet`, `no`, `yes` |
| `activity_move_mode` | `activityMoveMode()` | `activeEnergy`, `appleMoveTime` |

An enum value unknown to the app is sent as `value_<raw>`, so a newer iOS does not lose it. A field the
app may not read is omitted; a profile with no readable field is not sent.

### What is not exported in version 1

- The individual quantities of a series sample (`series`), and audiogram thresholds (above).
- Which samples belong to a workout (HealthKit's workout associations); processors match by time and
  source.
- Files in `HKAttachmentStore`, clinical records, medications and other types the app does not request
  (see `HealthCatalog` for the requested list).

## Identity of a record

- Deduplication is per **user**, across all of that user's devices: HealthKit
  syncs between a person's devices through iCloud, so the same object can
  arrive from several of them.
- Records with a `uuid` are identified by `(uuid, part)` (`part` is 0 when
  absent). HealthKit objects are immutable, so copies differ at most in
  serialization (for example after an app update); the processor keeps the
  first copy it processed and ignores later ones.
- A `deleted` record removes every part with that `uuid` and is remembered, so
  a later copy of the same `uuid` is ignored. Any of the user's pinned
  devices can therefore delete any of the user's records, matching HealthKit,
  where a deletion on one device syncs to the others.
- `header` records are checked (section 7 of the protocol) and not stored as
  data. `profile` records: the one with the latest `observed_at` is current;
  all are kept. `census` records: the latest per `(device_id, type, day)` is
  used. Records of unknown kinds without a `uuid` are kept with the batch they
  came in.
