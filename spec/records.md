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
