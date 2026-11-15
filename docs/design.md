# healthkit-archive: design for the app, the relay server and the CLI

> The wire protocol in [`spec/`](../spec/README.md) is normative and supersedes the protocol details below where they differ.

> Platform assumptions were checked against Cloudflare, Apple, PyPI and the `age` spec on 2026-11-13. Items still marked **[test]** are documented only in part and need a real test before relying on them.

## Context

The goal is a family-scale, backup-first archive of Apple Health (HealthKit) data that later feeds FHIR R4 and AI health-advisor agents, the same way the author's garmin-archive project does for Garmin. A survey of existing projects (November 2026) found none that ships this combination: existing HealthKit exporters store plaintext in a database behind bearer tokens. Because the relay runs on Cloudflare (storage operated by someone else), health data is end-to-end encrypted on the phone and the server only ever holds ciphertext.

Decisions made before the design:
- HealthKit only: data from other vendors' apps (for example Huawei Health) is read through HealthKit, not through their own APIs.
- End-to-end encryption with the `age` file format; signed batches.
- No passwords or bearer tokens: identity is a public key, the server keeps an allowlist.
- Each member decides in the app who can decrypt their data ("processors"), and can add or remove them.
- The CLI is a processor (and the admin tool), written in Python, and includes FHIR mapping.
- The relay is a Cloudflare Worker.

Outcome: this repository, with `docs/design.md` (this document), `spec/` (formats and test vectors), `worker/`, `cli/`, `ios/`.

## 1. System overview

```
iPhone app ── encrypted + signed batches ──▶ Worker ──▶ R2 (ciphertext) + D1 (index, allowlist)
   ▲  scans processor card (QR)                │
   │                                           ▼
Processor CLI ◀── downloads ciphertext, verifies, decrypts
   └─ per-day folders → summary → FHIR R4 NDJSON → Medplum
```

Three roles, all identified by keys:

| Role | Key | Lives in | Used for |
|---|---|---|---|
| Device (one per app install) | P-256 signing key | iPhone Secure Enclave, non-exportable | Signs every batch and every request |
| Processor (one per CLI install) | `age` X25519 identity + P-256 signing key | CLI key file (mode 600) + two offline copies | Decrypts batches; signs download requests |
| Admin (the server owner) | P-256 signing key | `hka-admin` key file, separate from the processor | Signs invites and admin requests; its public key is in the Worker config |

The phone never holds a decryption key. A member who wants to restore without the admin runs the CLI themself and adds their own processor card.

## 2. Shared formats (`spec/`)

### 2.1 Batch plaintext
Gzip-compressed NDJSON, one JSON object per line, UTF-8.

- Line 1, header: `{"k":"header","v":1,"user_id","device_id","seq","created_at","tz":"Asia/Ho_Chi_Minh","app_version","os_version","counts":{"<type>":n}}`
- `{"k":"sample","type":"HKQuantityTypeIdentifierHeartRate","uuid","start","end","value","unit","source":{"bundle_id","name","version","product_type","os_version"},"device":{…},"metadata":{…}}`
  - Category samples carry `value` (int) and `value_name`.
  - Correlations (blood pressure, food) nest their member samples.
  - Workouts carry activity type (number and name), duration, statistics, events and sub-activities.
  - Series objects are separate records linked by UUID: `{"k":"route","workout_uuid","points":[[t,lat,lon,alt,hacc,vacc,speed,course]…]}`, `{"k":"ecg",…,"voltages":[…]}`, `{"k":"heartbeats",…}`; long series are split across records with a `part` index.
- `{"k":"deleted","uuid","type","observed_at"}` for each `HKDeletedObject`.
- `{"k":"profile",…}` for characteristics (date of birth, sex, blood type), sent when they change.
- `{"k":"census","type","day","count","uuid_digest"}` for reconciliation (section 3.5).

Instants are UTC ISO-8601 with milliseconds. Units are HealthKit unit strings; UCUM conversion happens in the CLI. A batch is capped at 8 MB of ciphertext.

### 2.2 Encryption
`age` v1, binary (not armored), X25519 recipients only: one stanza per processor in the device's current grant. The app has its own encrypt-only writer on CryptoKit, tested against the reference `age` CLI and the published test vectors.

### 2.3 Manifest and signature
A small JSON object, signed as the exact bytes sent (no canonicalization step):

```json
{"v":1,"user_id":"u_…","device_id":"d_…","seq":42,
 "prev":"<sha256 of manifest 41 bytes, null for seq 1>",
 "ciphertext_sha256":"…","size":123456,
 "recipients":["p_…","p_…"],"grant_seq":3,"grant_sha256":"…","created_at":"…"}
```

Signature: ECDSA P-256 with SHA-256, raw 64-byte `r‖s`, base64url (CryptoKit `rawRepresentation`; Python converts with `decode_dss_signature`/`encode_dss_signature`). `prev` chains each device's batches so a dropped, reordered or rolled-back batch is detectable. `grant_seq` and `grant_sha256` bind the batch to the grant it was encrypted under, so a processor can detect a rolled-back or substituted grant.

### 2.4 Identifiers
`device_id` = `d_` + first 16 hex of SHA-256(device public key). `processor_id` = `p_` + first 16 hex of SHA-256(processor signing public key). `user_id` = `u_` + random, assigned by the admin at invite time.

### 2.5 Request signing (all endpoints except batch upload)
Headers `HKA-Key` (key id), `HKA-Timestamp`, `HKA-Signature` over `method \n path?query \n timestamp \n sha256(body)`; the server accepts ±5 minutes and returns its own time on a 401 so a client with a wrong clock can correct. Batch upload is authorized by the signed manifest instead, because iOS may send a background upload hours after it was created.

## 3. iOS app (`ios/`, Swift, SwiftUI, iOS 17+)

### 3.1 Modules
| Module | Responsibility |
|---|---|
| `HealthCatalog` | List of every readable type by iOS version; authorization request; per-type enable switches |
| `SyncEngine` | Anchored query per type, paging (about 2,000 objects per page), batch assembly, anchor bookkeeping |
| `Serializer` | HealthKit object → NDJSON record (section 2.1) |
| `AgeWriter` | Encrypt-only `age` v1 writer |
| `DeviceKey` | Secure Enclave P-256 key: create, sign, public key export |
| `Outbox` | Sealed batches on disk with their pending anchors; background `URLSession` uploads; retry |
| `Enrollment` | Scan invite QR, register device |
| `Processors` | Scan processor cards, show fingerprints, add/remove, publish grant |
| `Reconciler` | Daily re-send window and census |
| `StatusView` | Home: one status (with the size the relay confirms), processors, a link to Backup Details. Backup Details: what the relay stores, last read and upload, recent uploads, errors, device fingerprint, advanced actions |

### 3.2 Enrolment
1. Admin runs `hka-admin invite --name "Mom"`; the tool shows a QR: `{server, user_id, name, exp, nonce}` signed by the admin key.
2. The app scans it, creates the Secure Enclave key, and POSTs `{invite, device_pub, device_name}`.
3. The Worker verifies the admin signature, the expiry and that the nonce is unused, then stores the device.
4. The app asks for HealthKit read access to all catalog types.
5. The member scans at least one processor card (section 3.4). Sync does not start with an empty processor list.

### 3.3 Sync loop
Triggers: HealthKit background delivery (observer queries), `BGAppRefreshTask`/`BGProcessingTask`, app foreground, and a manual "Sync now".

1. For each enabled type, run an anchored query from the latest sealed anchor.
2. Serialize new and deleted objects into a batch; stop at the size cap.
3. Encrypt to the current processors, assign `seq = last + 1`, build and sign the manifest.
4. Write the ciphertext, manifest and the anchors-to-commit to the outbox, then start a background upload (a file upload task, which sends the `Content-Length` the server requires).
5. On a 200 response, commit those anchors and delete the outbox entry. On failure, retry the same file; the server treats a repeat as a no-op.

Rules that come from iOS behaviour:
- HealthKit is unreadable while the phone is locked, so steps 1 to 4 only run while it is unlocked; step 5 runs any time. A background wake while locked usually produces nothing.
- The observer query's completion handler is always called, even when nothing could be read; three misses and HealthKit stops waking the app.
- Up to `seq_window` (64) batches may be in flight at once and may arrive in any order; the server accepts any `seq` within the window above its contiguous prefix.
- A user force-quit cancels background uploads, so every launch re-enqueues whatever is still in the outbox.
- The chain belongs to the device: a `seq` and its manifest are signed once and never renumbered. If `GET …/state` shows the server holding less than the device saw acknowledged, the app stops and alerts the member instead of continuing from the server's view.
- A grant is uploaded and acknowledged before any batch that names it.

The first sync is the same loop over the whole history, run in the foreground with a progress screen.

### 3.4 Processors and grants
- A processor card is a QR from `hka card`: `{name, age_recipient, sign_pub}`. Cards are scanned directly, never fetched from the server, so the server cannot inject a recipient.
- The app shows each processor's name and fingerprint. Adding or removing one creates a new device-signed grant `{device_id, grant_seq, processors:[…], created_at}` and uploads it.
- New batches are encrypted to the new list. The server serves a device's batches only to processors in its current grant.
- A newly added processor cannot decrypt older batches. "Re-send history" resets the anchors and uploads the history again for the current list; the processor deduplicates by UUID.
- Removing a processor stops future access. Anything it already downloaded stays readable to it; the app says so.
- With a single processor, losing that processor's key makes the backup unreadable. The app warns when the list has only one entry and suggests adding a second (for example an offline recovery identity created with `hka init --recovery`).

### 3.5 Reconciliation
Anchors are not trusted alone. Once a day the app re-sends every sample from the last 48 hours (duplicates are dropped by UUID in the CLI); this daily batch is also the heartbeat that lets the CLI tell "no new data" from "app stopped". Once a week it sends a census of per-type, per-day counts for the last 30 days, which the CLI compares with its day folders and reports mismatches.

### 3.6 Key and file handling
- Device key: `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`, private-key usage only, no biometric flag. A keychain "interaction not allowed" error means retry later, never regenerate. **[test]** Apple documents this only by implication; confirm signing works on a locked phone.
- Outbox files: protection class "until first user authentication", so uploads work while locked **[test]**.
- A new or restored phone gets a new device key and re-enrols; it uploads history again as a new device stream. The CLI deduplicates by HealthKit UUID across all of a user's devices.

### 3.7 Distribution
Apple's capability table lists HealthKit for free Apple Developer accounts as well as paid ones (developer.apple.com/help/account/reference/supported-capabilities-ios, checked 2026-11-14). A free account signs for 7 days and cannot distribute; whether its profiles include background delivery is **[test]**. Options for about five family phones:

| Option | Expiry | Catch |
|---|---|---|
| TestFlight, internal testers | Each build stops after 90 days | Silent stop unless a new build ships every quarter; testers must be added to the developer account |
| Ad hoc | Reported as about one year, not confirmed in Apple's docs | Each phone's device ID must be registered |
| Unlisted App Store app | None | Full App Review; Apple describes it for organisations |

Start with TestFlight during development. For steady use, ad hoc is the working assumption; the daily heartbeat makes an expired build visible in `hka status` within days.

## 4. Relay server (`worker/`, Cloudflare Worker, TypeScript)

### 4.1 Storage
- **R2** (source of truth for data): `v1/<user_id>/<device_id>/<seq 10 digits>.age` and `…<seq>.manifest.json` (manifest + signature). Objects are written once and never overwritten. The bucket alone is a complete backup.
- **D1** (index and allowlist):
  - `users(user_id, name, created_at, status)`
  - `devices(device_id, user_id, pub, name, status, enrolled_at, retired_at, last_seq)`
  - `invites_used(nonce, exp)`
  - `grants(device_id, grant_seq, doc, sig, received_at)`
  - `batches(device_id, seq, ciphertext_sha256, size, manifest, sig, received_at)` with a unique key on `(device_id, seq)`
- **Config**: admin public key(s), max batch size (8 MB), per-device daily byte quota, per-user total quota.

Why both: R2 alone would make quota, grant and "after seq" lookups into list scans, and KV is eventually consistent. D1's unique key gives write-once ordering without Durable Objects. Free-tier D1 limits (500 MB, 100k rows written per day) are far above family scale.

Retention: a 30-day R2 bucket lock on the `v1/` prefix blocks deletes and overwrites of recent objects. It is a guard against bugs and a stolen admin key, not against the Cloudflare account owner, who can remove the rule.

Plan: the free Workers plan allows 10 ms of CPU per request, which hashing an 8 MB upload may exceed **[test]**. Budget for Workers Paid, or keep batches small enough to pass.

### 4.2 API
| Endpoint | Caller | Behaviour |
|---|---|---|
| `POST /v1/enroll` | new device + invite | Verify invite; create user if new; add device |
| `PUT /v1/devices/{did}/grant` | device | Store grant if `grant_seq` is higher than current |
| `GET /v1/devices/{did}/state` | device | `last_seq`, missing seqs, current `grant_seq`; stored batches, bytes and quota use, for display |
| `PUT /v1/devices/{did}/batches/{seq}` | device (signed manifest) | See 4.3 |
| `GET /v1/streams` | processor | Devices whose current grant lists this processor, with device public key and `last_seq` |
| `GET /v1/devices/{did}/batches?after=&limit=` | granted processor | Manifests and signatures |
| `GET /v1/devices/{did}/batches/{seq}` | granted processor | Ciphertext stream from R2 |
| `POST /v1/devices/{did}/purge` | device | Stop serving this device's batches at once; a scheduled job deletes the objects when the 30-day lock allows |
| `GET /v1/admin/users`, `GET /v1/admin/usage` | admin | Listing and storage per user |
| `POST /v1/admin/devices/{did}/retire` | admin | Mark retired; uploads refused, reads still served |
| `DELETE /v1/admin/users/{uid}` | admin | Purge a user |

### 4.3 Batch upload checks
1. Device exists and is active.
2. Signature over the manifest header verifies with the device key (WebCrypto ECDSA P-256).
3. Manifest `device_id` and `seq` match the path; `size` equals `Content-Length` (required, because R2 needs a known length) and is within the cap and quotas.
4. If `(device_id, seq)` exists: same `manifest_sha256` → 200 (idempotent); different → 409.
5. Otherwise `seq` must be within `seq_window` of the contiguous prefix. The server does not check `prev`; processors do.
6. Stream the body to R2 with a conditional put (fails if the key exists) and the manifest's SHA-256 as the integrity check **[test]** that a mismatch rejects the write and that the conditional form behaves as documented.
7. Write the manifest object, insert the D1 row, return 200.

R2 is written before D1. If the D1 insert fails, the retry finds the object already present with the same hash and only inserts the row.

### 4.4 What the server can and cannot do
It sees user and device ids, sizes, timings and who the processors are. It cannot read data, forge a batch (no device key), or add a recipient (cards are pinned on the phone). It could withhold or delete batches; the hash chain and a liveness alarm in the CLI detect that.

## 5. CLI (`cli/`, Python 3.11+, command `hka`)

Conventions are taken from `garmin-archive/garmin_archive.py`: one `manifest.json` per data set, `provisional`/`final`/`failed` day status, atomic writes (tmp + `os.replace`), reproducible `tar.gz` archives of final days, `fcntl` lock file, exit codes `0/1/2/3`, `.env` config, `unittest` with a local stub server. Unlike garmin-archive it needs two third-party packages: `pyrage` (age; 1.4.0, August 2026, wheels for macOS and glibc Linux but not Alpine, so Docker images must be Debian-based) and `cryptography` (P-256).

### 5.1 Commands
| Command | Does |
|---|---|
| `hka init [--recovery]` | Create processor identity (age + signing key); print the two-offline-copies warning. `--recovery` makes an offline-only identity whose card members add as a second recipient |
| `hka card [--qr]` | Show the processor card for members to scan |
| `hka pull` | Download new ciphertext and manifests into `inbox/`; verify |
| `hka build` | Decrypt new batches; update day folders, index, summaries, archives |
| `hka fhir map`, `hka fhir load` | Map changed days to FHIR R4 NDJSON; load into Medplum |
| `hka run` | `pull` + `build` + `fhir map` (+ `load` if configured) |
| `hka status [--json]` | Per user: range, final/provisional days, gaps, silent devices, census mismatches |
| `hka verify` | Re-check every signature and chain; test-decrypt a sample |
| `hka export --user U [--from --to]` | Decrypted day folders as a zip |

### 5.1a Admin tool (`hka-admin`)
A separate command with its own key file, so the admin key never has to sit on the machine that runs the processor unattended, and a member running their own processor gets no admin commands. It shares only a small module with `hka` (`hkacommon`: request signing, API client, QR output) and needs `cryptography` but not `pyrage`.

| Command | Does |
|---|---|
| `hka-admin init` | Create the admin key (`~/.config/hka-admin/admin.key`, mode 600); print the public key for the Worker config |
| `hka-admin invite --name N [--user U]` | Signed invite QR (new user, or a new device for an existing user) |
| `hka-admin users`, `usage` | List users and devices; storage per user |
| `hka-admin retire DEVICE` | Stop a device uploading |
| `hka-admin purge USER` | Delete a user's data |

Repository layout: `cli/hkacommon/`, `cli/hka/`, `cli/hka_admin/`, one `pyproject.toml` with two entry points.

### 5.2 Data directory
```
data/
  keys/                       processor identity only (600)
  pins.json                   device public keys, pinned on first use
  inbox/<user_id>/<device_id>/<seq>.age + .manifest.json     immutable ciphertext mirror
  users/<user_id>/
    manifest.json             days, status, hashes (garmin-archive layout)
    index.sqlite              uuid → day, type (rebuildable from inbox)
    days/YYYY/YYYY-MM-DD/
      _day.json
      <HealthKitType>.ndjson  one file per type
      workouts.ndjson, routes/<workout_uuid>.gpx
      summary.json            daily aggregates
    archives/YYYY/YYYY-MM-DD.tar.gz
    fhir/days/YYYY/YYYY-MM-DD.ndjson, fhir/manifest.json
  .lock, logs/
```

### 5.3 `pull`
1. `GET /v1/streams`; for a new device, show its fingerprint and pin its key (the member confirms it matches the app's status screen).
2. For each device, list manifests after the last stored `seq`; download ciphertext.
3. Verify: device signature, ciphertext hash, `prev` chain, `seq` continuity, and that the grant named in each manifest is one this processor has seen signed by the device.
4. Report gaps, chain breaks and devices with no batch for 3 days (exit code 1).

Device keys are trusted on first use, so a malicious relay could serve a forged key for a brand-new device. The fingerprint comparison with the app's status screen is the defence; `hka pull` refuses to build from an unconfirmed device.

The `inbox/` mirror is ciphertext, so it can be copied to a second remote with rclone as a redundant backup.

### 5.4 `build`
1. Decrypt each new batch with the processor identity.
2. For each record: assign a day and merge by UUID into that day's type file. A UUID already present is skipped.
3. `deleted` records are resolved through `index.sqlite` and remove the sample from its day; the tombstone is kept in `_day.json`.
4. Recompute `summary.json` for touched days; mark them `provisional`.
5. A day untouched for 7 days becomes `final` and is archived; a late batch reopens it and drops the archive.

Day rules: local calendar date of the sample start in the batch time zone (or the sample's own time-zone metadata); a workout belongs to the day it starts. Sleep belongs to the *sleep day* it ends in: 18:00 to 18:00 local time, named after the morning it ends on, so a night that starts before midnight stays whole (and sleep ending from 18:00, such as an evening nap, counts toward the next night). The archive records its day-rule version in `manifest.json` (`day_rules`); version 1 put each sleep record on the calendar date it ended, splitting such nights at midnight, and `hka build` moves records filed under it once.

`summary.json` holds what Garmin's API gave ready-made: steps, distance and energy totals with one source chosen per type (configurable source priority, to avoid double counting iPhone and watch), heart-rate min/max/resting, sleep-stage minutes, workout list. The FHIR mapper reads mostly this file.

### 5.5 FHIR
Port the structure of `garmin-archive/garmin_fhir.py`: per-day mapper, `MAPPING_VERSION`, `fhir/manifest.json` re-map detection by day content hash, conditional upserts by identifier, the `sampled()` helper, the Medplum loader.

- Identifier: HealthKit UUID for per-sample resources; `<day>/<metric>` for daily ones.
- One `Patient` per user; `Device` per source device.
- Standard LOINC codes where correct (the table agreed earlier); the HealthKit type identifier as a second coding; a `healthkit-metrics` CodeSystem only for values with no correct LOINC code.
- Workouts: LOINC 55411-3 with components, type as text in 73985-4; the route as a generated GPX behind `DocumentReference`/`Binary`.
- Heart-rate series: resampled to a 1-minute grid for `valueSampledData`; raw samples stay in the day folder.

The detailed mapping table is its own document (`docs/fhir-mapping.md`), written when real data exists.

## 6. Failure and recovery

| Event | Result |
|---|---|
| Phone lost | Admin retires the device; nothing readable was on it; new phone re-enrols and re-sends history |
| Processor key lost | Batches encrypted only to it are unreadable; other processors unaffected; member adds a new card and re-sends history |
| Server data lost | `inbox/` mirror on each processor still holds the ciphertext; phones can re-send history |
| Server drops or reorders batches | Chain or gap alarm in `hka pull` |
| Upload retried or replayed | Same seq and hash is a no-op |
| App reinstalled | New device key; treated as a new device |

## 7. Build order

1. `spec/`: record schema, manifest, request signing, with test vectors.
2. Worker with local tests (`wrangler dev`), driven by a Python fake device.
3. `hka-admin`, then `hka` `init`, `card`, `pull`, `build`, `status`, tested end-to-end against the local Worker with the fake device.
4. iOS app: enrolment, age writer, sync, outbox; then processors UI and reconciliation.
5. CLI `fhir map|load`, once real day folders exist.

### Review gate after every phase
No phase starts until the previous one has passed this loop:

1. **Review round:** when a phase's code and tests are done and passing, run a comprehensive independent review of that phase's diff (correctness, security and crypto handling, conformance to `spec/`, test coverage). Reviewers get the code and the spec, not my conclusions.
2. **Verdict each finding independently:** for every point, check it against the code, the spec or a reproducing test, and rule it real or not real with the evidence.
3. **Act:** fix the real ones, with a test where one applies. Skip the others and write down the objection.
4. **Next round:** review again, giving the reviewers the list of skipped findings with their objections, so they can accept the objection or rebut it with new evidence.
5. **Exit:** repeat until the review accepts the phase, or the only findings left are ones that do not matter (skipped with a stated reason). Then move to the next phase.

Each phase ends with a short report: rounds run, findings fixed, findings skipped and why.

## 8. Verification

- **Formats:** the published `age` vectors test decryption, so the Swift writer is tested the other way round: its output must decrypt with both the reference `age` CLI and `pyrage` in CI, across sizes around the 64 KiB chunk boundary. Signature vectors verify identically in Swift, TypeScript and Python.
- **Worker:** tests for idempotent re-upload, 409 on a different hash, retired device refused, expired or reused invite refused, ungranted processor refused, stale timestamp refused.
- **CLI:** `unittest` with a stub server, as in garmin-archive: merge, deletion through the index, late batch reopening a final day, chain break detection, deterministic archives.
- **End to end, no phone:** fake device uploads synthetic batches to `wrangler dev`; `hka run` produces the expected day folders.
- **On device:** background delivery and upload while locked; Secure Enclave signing in the background; a full-history backfill; a sample deleted in Health disappearing from its day folder; adding a second processor and re-sending history.
- **Restore drill:** decrypt the inbox with only the offline key copy and the stock `age` tool.

## 9. Tests to run first, before building on the assumption
- R2: a put with a wrong SHA-256 is rejected; the conditional put refuses an existing key.
- Worker CPU time for an 8 MB upload on the free plan.
- iOS: Secure Enclave signing and outbox upload while the phone is locked; custom headers surviving on a background upload task.
- Whether a free Apple Account's profile includes HealthKit background delivery (Apple lists HealthKit itself as supported), whether SideStore keeps the HealthKit entitlements when it re-signs, and the real ad hoc profile lifetime.

## 10. Known limits accepted
- Sync is opportunistic: data arrives when the phone is unlocked and iOS allows, typically hours late.
- A removed processor keeps whatever it already downloaded.
- The relay sees who uploads, when, how much, and to which processors.
- The Cloudflare account owner can delete the bucket; each processor's `inbox/` mirror is the second copy.
