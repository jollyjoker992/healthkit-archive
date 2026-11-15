# healthkit-archive protocol, version 1

This document is normative for the three components: the iOS app (device),
the relay Worker (server) and the CLIs (`hka` processor, `hka-admin`).
Plaintext record formats are in [records.md](records.md). Test vectors are in
[vectors/](vectors/) and are produced by [tools/gen_vectors.py](tools/gen_vectors.py);
every implementation must pass them.

The key words MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119.

Trust model in one paragraph: the relay is trusted to stay available, not to
be honest. It never sees plaintext and cannot forge anything a device or
processor signs. It can withhold, delete or delay data; processors detect
that through the per-device hash chain (section 7). The admin decides who may
enrol devices but cannot read data unless a member chooses the admin's
processor. Each member decides, on the phone, which processors can decrypt
their data.

## 1. Encodings

| Name | Definition |
|---|---|
| `b64u` | RFC 4648 §5 base64url **without padding**, canonical. Decoders MUST reject `=`, whitespace, characters outside the alphabet, input whose length is 1 mod 4, and input whose unused trailing bits are not zero (re-encoding the decoded bytes MUST give the input back). |
| `hex` | Lowercase hexadecimal. Decoders MUST reject uppercase. |
| Instant | RFC 3339 in UTC with a `Z` suffix and exactly three fractional digits: `2026-11-13T08:00:00.000Z`. Sub-millisecond precision is truncated, not rounded. Receivers MUST reject other forms where this spec says "instant". |
| Unix time | JSON integer seconds since the epoch. |
| Text length | Limits on names count Unicode scalar values. |

### 1.1 JSON documents
- UTF-8. Decoding MUST be strict: invalid UTF-8 is an error, never replaced.
- A signed document is signed over its **exact bytes**. Receivers MUST verify those bytes and then parse them; they MUST NOT re-serialize to verify.
- Signers MUST NOT emit duplicate keys; receivers SHOULD reject them.
- Integer fields are emitted as plain decimal integers (`0` or `[1-9][0-9]*`, no sign, fraction or exponent) not above 2^53 − 1. Receivers MUST reject a non-integer or out-of-range value for an integer field. (A JSON parser may read `1.0` as `1`; that is harmless because only the signer controls the bytes, and processors are the final authority.)
- JSON booleans are never integers: receivers MUST reject `true`/`false` in an integer field (Python's `bool` is a subclass of `int`).
- Receivers MUST check the `v` and `t` fields of every document and reject unknown values. Other unknown fields MUST be ignored.

## 2. Keys, signatures and identifiers

### 2.1 Signing keys
All signing keys are ECDSA over P-256 (secp256r1) with SHA-256.

- **Public key:** SEC 1 uncompressed point, exactly 65 bytes (`0x04 || X || Y`), as `b64u` (87 characters). Receivers MUST check the length before parsing and MUST reject compressed points and points not on the curve.
- **Signature:** raw `r || s`, each 32 bytes big-endian, 64 bytes, as `b64u` (86 characters). DER signatures MUST be rejected. High-S signatures are accepted (no identity is derived from signature bytes).

### 2.2 Signing input and domain separation
Every signature is computed over

```
"hka-v1/" || purpose || "\n" || payload
```

where `purpose` is one of `invite`, `enroll`, `grant`, `manifest`, `request`, and `payload` is the exact bytes described for that purpose. A signature for one purpose MUST NOT verify for another.

### 2.3 Identifiers

| Identifier | Form | Derivation |
|---|---|---|
| `device_id` | `d_` + 32 hex | First 16 bytes of SHA-256(device public key bytes) |
| `processor_id` | `p_` + 32 hex | First 16 bytes of SHA-256(processor signing public key bytes) |
| `admin_id` | `a_` + 32 hex | First 16 bytes of SHA-256(admin public key bytes) |
| `user_id` | `u_` + 32 hex | 16 random bytes, chosen by the admin when inviting |

A receiver that is given both an identifier and its public key MUST recompute the identifier and reject a mismatch.

### 2.4 Encryption keys
Processors hold an `age` X25519 identity (`AGE-SECRET-KEY-1…`). Its recipient string (`age1…`, lowercase Bech32, 62 characters) is what devices encrypt to.

### 2.5 Fingerprints
A fingerprint is shown to people to compare keys across screens: the first 10 bytes of SHA-256 of the input, as 20 uppercase hex characters in groups of four, `3F2A-91C0-77D4-0B1E-A5C2`.

| Fingerprint of | Input |
|---|---|
| Device | device public key bytes |
| Processor | `sign_pub` bytes followed by the ASCII bytes of `age_recipient` |
| Admin | admin public key bytes |

The processor fingerprint covers both keys, so a card whose `age_recipient` was swapped shows a different fingerprint.

## 3. Documents exchanged out of band (QR codes)

### 3.1 Processor card
Printed by `hka card`, scanned by the app.

```
hka-card:<b64u(card JSON)>
```

```json
{"v":1,"t":"card","name":"Family Mac","age_recipient":"age1…","sign_pub":"<b64u>"}
```

`processor_id` is derived from `sign_pub`. `name` is 1 to 64 characters. The card is not signed; the member trusts it because they compare its fingerprint (2.5) with the one `hka card` prints on the processor's own screen. The app MUST show the fingerprint and MUST require confirmation before adding the processor.

### 3.2 Invite
Printed by `hka-admin invite`, scanned by the app.

```
hka-invite:<b64u(invite JSON)>.<b64u(signature)>
```

The signature has purpose `invite` and payload = the invite JSON bytes.

```json
{"v":1,"t":"invite","server":"https://hka.example.workers.dev","admin_id":"a_…",
 "user_id":"u_…","user_name":"Mom","exp":"2026-11-20T00:00:00.000Z","nonce":"<b64u of 16 random bytes>"}
```

- `user_name` is 1 to 64 characters.
- `server` is the relay's origin: `https://` and a host, optional `:port`, no path, no trailing slash. The app sends requests only to this origin. (Local development MAY use `http://localhost:<port>`.)
- `exp` is at most 7 days after the invite was made; the server rejects an invite whose `exp` is more than 7 days ahead of its clock.
- An invite is single-use: its `nonce` is consumed by the first successful enrolment. One invite enrols one device; a second device for the same user needs a new invite with the same `user_id`.

## 4. Signed documents sent to the relay

### 4.1 Enrolment
`POST /v1/enroll` with `Content-Type: application/json` and header `HKA-Signature`.

Body (at most 4 KiB):

```json
{"invite":"hka-invite:…","device_pub":"<b64u>","device_name":"Mom's iPhone","ts":1794556800}
```

`HKA-Signature` is the device key's signature with purpose `enroll` over the exact body bytes. It proves possession of the device key and binds it to the invite. `device_name` is 1 to 64 characters.

The server checks, in order, answering with the errors of section 6.2:
1. Body size, JSON and field formats.
2. The invite parses; `admin_id` is a configured admin key; the invite signature verifies.
3. The device signature verifies.
4. If the invite's nonce was already consumed by **this** `device_id`: return 200 with the original response (idempotent retry; `exp` and `ts` are not re-checked).
5. `server` equals the relay's configured origin; `exp` is in the future and at most 7 days ahead (403 `invite_invalid`); `ts` is within ±300 s (401 `stale_timestamp` with `server_time`, so the app can correct its clock and re-sign).
6. Nonce consumed by another device: 409 `invite_used`. `device_id` already enrolled: 409 `device_exists`. `user_id` purged: 409 `user_purged`.
7. Create the user if absent (name from the invite), create the device as `active`, consume the nonce, return 200:

```json
{"user_id":"u_…","device_id":"d_…","server_time":1794556801,"limits":{…}}
```

`limits` is the object of section 6.3. The device MUST check that `user_id` equals the invite's and `device_id` equals the id derived from its own key, and refuse to continue otherwise.

### 4.2 Grant
A grant lists the processors allowed to read a device's batches. It is signed by the device, purpose `grant`, payload = grant JSON bytes (at most 16 KiB).

```json
{"v":1,"t":"grant","user_id":"u_…","device_id":"d_…","grant_seq":2,
 "processors":[{"id":"p_…","name":"Family Mac","age_recipient":"age1…","sign_pub":"<b64u>"}],
 "created_at":"2026-11-13T08:00:00.000Z"}
```

Rules:
- `grant_seq` starts at 1 and increases by exactly 1 per grant.
- `processors` has 1 to 8 entries with distinct `id`s; each `id` MUST match its `sign_pub`; each `name` is 1 to 64 characters.
- `grant_sha256` (used by manifests) = hex SHA-256 of the grant JSON bytes.

Upload: `PUT /v1/devices/{device_id}/grant`, body `{"doc":"<b64u(grant JSON)>","sig":"<b64u>"}`. The grant is self-authenticating, so this request carries no request signature.

The server checks, in order: the device exists and the signature verifies with its key (401 `bad_signature`, also for an unknown device); `user_id` and `device_id` match the device and the path, and the rules above (400 `bad_request`). Then:
- A stored grant with this `grant_seq` and identical bytes: 200 (idempotent, whatever the device status).
- A stored grant with this `grant_seq` and different bytes: 409 `grant_conflict`.
- The device is not `active`: 403 `device_inactive`.
- `grant_seq` equal to the stored maximum + 1 (or 1 when none): store it, 200 `{"grant_seq": n}`.
- `grant_seq` greater than the stored maximum + 1: 409 `grant_out_of_order` (retryable once the earlier grants arrive).

In every grant error, and in the batch error `grant_unknown`, the extra field `grant_seq` is the stored maximum (0 when none).

Device rules:
- A grant is signed once and saved durably **before** its first upload; it is never re-signed with different bytes.
- The device uploads its grants in `grant_seq` order and MUST receive a 200 for a grant before uploading any batch that names it.
- Because grants are never skipped and never re-signed, every grant a batch names is eventually stored, so `grant_unknown` always clears, unless the grant itself is rejected with a non-retryable error. That is handled like a non-retryable batch error (section 4.3, device rules).

### 4.3 Batch manifest
Every batch has a manifest signed by the device, purpose `manifest`, payload = manifest JSON bytes (at most 4 KiB).

```json
{"v":1,"t":"manifest","user_id":"u_…","device_id":"d_…","seq":42,
 "prev":"<hex SHA-256 of manifest 41's JSON bytes, or null for seq 1>",
 "ciphertext_sha256":"<hex>","size":123456,
 "recipients":["p_…","p_…"],"grant_seq":3,"grant_sha256":"<hex>",
 "created_at":"2026-11-13T08:00:00.000Z"}
```

- `seq` starts at 1 and increases by exactly 1 per batch, per device.
- `manifest_sha256` = hex SHA-256 of the manifest JSON bytes. `prev` is the `manifest_sha256` of the device's own manifest `seq − 1`.
- `recipients` lists the processor ids the batch was encrypted to and equals the processor ids of grant `grant_seq`, in grant order.
- `size` is the ciphertext length in bytes.

#### Device rules (the chain belongs to the device)
- A device assigns a `seq` and signs its manifest once, when it seals the batch, and never signs a different manifest for that `seq`. It never renumbers and never takes `prev` from the server.
- It keeps sealed batches in its outbox until each gets a 200, and records the highest `seq` it has seen acknowledged.
- It MAY have up to `seq_window` (6.3) batches in flight; arrival order does not matter.
- If `GET …/state` reports `max_seq` lower than the highest `seq` the device saw acknowledged, the device MUST stop uploading; it MUST NOT continue the chain from the server's view. It confirms with a second `state` read at least 10 minutes later; if the condition persists, it alerts the member (the relay lost or rolled back data).
- On first launch after install, the app MUST delete any key left in the keychain under its tag and generate a new device key (keychain items can survive app deletion). If at any launch the device key is missing or does not match the stored `device_id` (for example app data restored from a backup onto another phone, without its Secure Enclave key), the app MUST discard all device state (ids, outbox, anchors, chain head) and start as a new, unenrolled device.
- A batch or grant rejected with a non-retryable error (section 6.2) can never be stored, so the device's chain can never pass it. The app MUST stop uploading and alert the member. Recovery is: the admin retires the old device, and the app enrols again as a new device and re-sends history. The same recovery applies after a confirmed rollback alarm (for example after the relay was restored from an old backup).
- Errors marked "after admin action" in section 6.2 are not permanent: the app pauses uploads, alerts the member, and retries at most once a day and on each app launch.
- The app SHOULD keep the outbox short (seal shortly before upload, at most about `seq_window` batches) to limit what remains readable by a processor that is removed while batches wait.

#### Upload
`PUT /v1/devices/{device_id}/batches/{seq}` with headers

| Header | Value |
|---|---|
| `HKA-Manifest` | `b64u(manifest JSON)` |
| `HKA-Signature` | `b64u(signature)` |
| `Content-Length` | ciphertext length; REQUIRED |
| `Content-Type` | `application/octet-stream` |

and the ciphertext as body. Batch uploads carry no timestamp: iOS may send a background upload long after it was created. The signature, the write-once `seq` and the hash chain make replays harmless.

The server checks, in order:
1. The device exists and the manifest signature verifies with its key (401 `bad_signature` otherwise, also for an unknown device).
2. `user_id`, `device_id` and `seq` match the device and the path (400 `bad_request`).
3. A batch with this `seq` is recorded: 200 if its `manifest_sha256` is equal (idempotent retry; the body is not read), else 409 `seq_conflict`.
4. The device is `active` (403 `device_inactive`).
5. `size` equals `Content-Length` (400 `bad_request`) and is at most `max_batch_bytes` (413 `batch_too_large`).
6. `seq` is at most `contiguous_seq + seq_window` (409 `seq_out_of_window`, retryable once earlier batches arrive).
7. Grant `grant_seq` is stored (409 `grant_unknown`, retryable after the grant upload), its `grant_sha256` matches and `recipients` equals its processor ids (409 `grant_mismatch`).
8. Quotas: per-device daily bytes (413 `quota_exceeded`, retryable, with `retry_after`); per-user total bytes (413 `quota_total_exceeded`, after admin action; see the device rules above).
9. Store the ciphertext at a key derived from `(device_id, seq)` without overwriting, verifying it against `ciphertext_sha256` (400 `hash_mismatch`, nothing stored). If an object already exists at that key (an earlier attempt stored it but failed before step 10, or a concurrent identical upload), the step succeeds when the stored object's SHA-256 equals `ciphertext_sha256`, and fails with 500 `internal` otherwise.
10. Record the batch (the record is the commit point) and return 200 `{"seq": 42, "received_at": "<instant>"}`. A concurrent identical upload that loses the race to record returns 200 as well.

`max_batch_bytes` is fixed for a deployment: operators MUST NOT lower it, because batches already sealed under the old value could never be stored.

Read consistency: every read the server uses for authentication, idempotency, `contiguous_seq`/`max_seq`, grants and `state` MUST reflect every write it has acknowledged (for example, read from the primary database, not a lagging replica). Otherwise a client can be told something has gone backwards and abandon a healthy chain.

The server does not verify `prev`; processors do (section 7). `contiguous_seq` is the highest `n` such that every `seq` from 1 to `n` is stored (0 if none).

### 4.4 Ciphertext
The batch body is an `age` v1 file in binary form (not armored), encrypted to the X25519 recipients of the grant named in the manifest, one stanza per processor, in grant order. The plaintext is a gzip stream (RFC 1952) of NDJSON records as defined in [records.md](records.md). Decompressed size MUST NOT exceed 256 MiB; processors MUST enforce this while decompressing.

## 5. Signed requests

Every endpoint other than enrolment, grant upload and batch upload is authenticated with a request signature.

| Header | Value |
|---|---|
| `HKA-Key` | `device_id`, `processor_id` or `admin_id` |
| `HKA-Timestamp` | Unix time, decimal |
| `HKA-Signature` | signature, purpose `request` |

Payload (lines joined with `\n`, no trailing newline):

```
METHOD
ORIGIN
TARGET
TIMESTAMP
hex(SHA-256(body))
```

- `METHOD` is uppercase. `ORIGIN` is the relay origin as in invites; the server uses its configured origin, so a signature made for one deployment fails on another.
- `TARGET` is the path plus `?query` when the query is non-empty, exactly as sent. All paths and queries in this protocol use only characters that need no percent-encoding; clients MUST NOT send an empty `?`. The server uses the URL's `pathname + search`.
- An empty body hashes as SHA-256 of zero bytes. Signed requests in this protocol have empty bodies.
- The server MUST reject a timestamp more than 300 s from its clock with 401 `stale_timestamp`. Every 401 response includes `"server_time"` so clients can correct their clock.
- Key lookup: `d_` ids from the device table (any status; each endpoint checks status); `p_` ids from the processors listed in **any** grant ever stored (grants are kept after a purge); `a_` ids from the configured admin keys. Unknown key: 401 `unknown_key`.

Requests are not single-use within the 300 s window. Every signed endpoint is read-only or idempotent, so a replay within the window has no further effect.

## 6. Relay API

### 6.1 Endpoints

| Method and path | Auth | Response (200) |
|---|---|---|
| `POST /v1/enroll` | enrolment (4.1) | `{"user_id","device_id","server_time","limits"}` |
| `PUT /v1/devices/{did}/grant` | grant document (4.2) | `{"grant_seq"}` |
| `PUT /v1/devices/{did}/batches/{seq}` | manifest (4.3) | `{"seq","received_at"}` |
| `GET /v1/devices/{did}/state` | request, that device | see below |
| `POST /v1/devices/{did}/purge` | request, that device, `active` or `purged` | `{"status":"purged"}` |
| `GET /v1/streams` | request, processor | see below |
| `GET /v1/devices/{did}/grants` | request, listed processor | `{"grants":[{"grant_seq","doc","sig","received_at"}]}`, ascending; also for `purged` devices |
| `GET /v1/devices/{did}/batches?after=N&limit=M` | request, granted processor | see below |
| `GET /v1/devices/{did}/batches/{seq}` | request, granted processor | ciphertext, `application/octet-stream` |
| `GET /v1/admin/users` | request, admin | see below |
| `POST /v1/admin/devices/{did}/retire` | request, admin | `{"status":"retired"}` |
| `DELETE /v1/admin/users/{uid}` | request, admin | `{"status":"purged"}` |

In every response, `doc`, `manifest` and `sig` are `b64u` of the exact signed bytes, `received_at` is an instant, and absent values are `null`, never omitted.

A device key may only call its own device's endpoints, and admin endpoints accept only admin keys (403 `forbidden` otherwise). For processor reads the server checks, in order: the device exists (404 `not_found`); the caller is a listed processor of it (403 `not_granted`); for batch reads, the device is not `purged` (403 `device_purged`) and the caller is granted (403 `not_granted`).

A **granted processor** for a device is one listed in the device's current (highest `grant_seq`) grant; only granted processors can list and download batches (403 `not_granted` otherwise). A **listed processor** is one that appears in any of the device's grants; it can read the device's grants, so it can verify a device-signed grant that removed it (section 7). Batches of `purged` devices are never served (403 `device_purged`).

`GET /v1/devices/{did}/state`:

```json
{"status":"active","contiguous_seq":41,"max_seq":42,"last_manifest_sha256":"<hex of manifest max_seq, or null>",
 "grant_seq":3,"grant_sha256":"<hex or null>","server_time":1794556801,"limits":{…},
 "batches":42,"bytes":98765432,"last_received_at":"<instant or null>","user_bytes":123456789}
```

`grant_seq` and `grant_sha256` describe the highest stored grant and are `null` when the device has none.

`batches` and `bytes` count the device's stored batches and their ciphertext size, `last_received_at` is the newest of their `received_at` (`null` when there are none), and `user_bytes` is the stored ciphertext of the user's devices that are not `purged`, the figure checked against `user_total_bytes` (6.3). They are for display: a device MUST NOT base chain or upload decisions on them.

`GET /v1/streams`: every device for which the caller is a listed processor. `granted` says whether it is in the current grant:

```json
{"streams":[{"user_id":"u_…","user_name":"Mom","device_id":"d_…","device_name":"Mom's iPhone",
  "device_pub":"<b64u>","status":"active","granted":true,"contiguous_seq":42,"max_seq":42,"grant_seq":3}]}
```

`GET /v1/devices/{did}/batches?after=N&limit=M`: stored batches with `seq > N`, ascending, at most `M`. `after` defaults to 0, `limit` to 500; both are integers, `limit` 1 to 1000; anything else is 400 `bad_request`. Gaps are possible while uploads are in flight.

```json
{"batches":[{"seq":42,"manifest":"<b64u>","sig":"<b64u>","size":123456,"received_at":"…"}],
 "contiguous_seq":41,"max_seq":42}
```

`GET /v1/admin/users`:

```json
{"users":[{"user_id","name","status","created_at",
  "devices":[{"device_id","name","status","enrolled_at","contiguous_seq","max_seq","batches","bytes","grant_seq"}]}]}
```

### 6.2 Errors
Errors are `{"error":"<code>","message":"<text>", …extra}`. "Retryable" tells clients whether repeating the same request later can succeed.

| Status | Code | Retryable | Extra fields | When |
|---|---|---|---|---|
| 400 | `bad_request` | no | | Malformed JSON, header, field, path or query |
| 400 | `hash_mismatch` | yes | | Body did not match `ciphertext_sha256` (corruption in transit) |
| 401 | `bad_signature` | no | `server_time` | Signature does not verify, or unknown device on upload |
| 401 | `unknown_key` | no | `server_time` | `HKA-Key` not found |
| 401 | `stale_timestamp` | yes, after fixing the clock | `server_time` | Request timestamp or enrolment `ts` outside ±300 s |
| 403 | `device_inactive` | no | `status` | Device is `retired` or `purged` |
| 403 | `device_purged` | no | | Reading a purged device |
| 403 | `not_granted` | no | | Processor not in the current grant |
| 403 | `forbidden` | no | | Key type not allowed on this endpoint |
| 403 | `invite_invalid` | no | | Unknown admin, bad invite signature, wrong server, expired or too-long `exp` |
| 404 | `not_found` | no | | Unknown path, device, user or batch |
| 409 | `invite_used` | no | | Nonce consumed by another device |
| 409 | `device_exists` | no | | Device key already enrolled |
| 409 | `user_purged` | no | | Invite for a purged `user_id` |
| 409 | `grant_conflict` | no | `grant_seq` | A different grant is stored at this `grant_seq` |
| 409 | `grant_out_of_order` | yes | `grant_seq` | `grant_seq` skips past the stored maximum + 1 |
| 409 | `grant_unknown` | yes | `grant_seq` | Manifest names a grant not yet stored |
| 409 | `grant_mismatch` | no | | Manifest's grant hash or recipients wrong |
| 409 | `seq_conflict` | no | | A different manifest is stored at this `seq` |
| 409 | `seq_out_of_window` | yes | `contiguous_seq` | `seq` more than `seq_window` ahead |
| 413 | `batch_too_large` | no | `max_batch_bytes` | `size` over the limit |
| 413 | `quota_exceeded` | yes | `retry_after` (seconds) | Daily device quota reached; also sent as `Retry-After` |
| 413 | `quota_total_exceeded` | after admin action | `user_total_bytes` | User's total quota reached; the admin must raise it |
| 429 | `rate_limited` | yes | `retry_after` | Too many requests |
| 500 | `internal` | yes | | Server error |

### 6.3 Limits object

```json
{"max_batch_bytes":8388608,"device_daily_bytes":536870912,"user_total_bytes":21474836480,"seq_window":64}
```

`device_daily_bytes` counts ciphertext accepted per device per UTC day and MUST be at least `max_batch_bytes`. `user_total_bytes` counts the stored ciphertext of the user's devices that are not `purged`; a purged device's bytes are released immediately. The admin may raise either quota at any time. Quota checks are not atomic with concurrent uploads, so usage can exceed a quota by at most `seq_window × max_batch_bytes` per device.

### 6.4 Device status, retirement and purge

| Status | Upload, grant | Served to processors | Signed requests |
|---|---|---|---|
| `active` | yes | yes | all of the device's endpoints |
| `retired` | no | yes | `state` only |
| `purged` | no | no | `state` and `purge` (repeat) |

- `retire` is for a lost or replaced phone. A retired device cannot purge, so a stolen phone cannot erase the member's history. It cannot be undone. Retiring a `retired` device returns 200 `{"status":"retired"}`; retiring a `purged` device returns 403 `device_inactive`. The recovery in section 4.3 skips the retire step when the device is already inactive.
- Status gates new writes only: an idempotent repeat of a grant or batch that was already stored returns 200 whatever the device's status.
- A member purges from an active device; the admin purges a whole user. Purge stops serving immediately; a scheduled job deletes the stored objects once the retention lock allows. A repeated purge of an already purged device or user returns 200. A purged `user_id` cannot be invited again.
- Processors learn about a purge from `/v1/streams` (`status: "purged"`). That status is only the relay's claim: processors MUST NOT delete their local copy because of it. What a processor does with its own copy is its owner's decision; `hka` reports it and never deletes automatically.
- `retire` and `purge` requests have empty bodies.

### 6.5 Rate limiting
The server SHOULD rate-limit unauthenticated endpoints (`/v1/enroll`) per client address and MAY rate-limit others; it answers 429 `rate_limited`.

## 7. Processor verification

A processor MUST, for each device it reads:
1. Pin the device public key on first use only after its owner confirms the device fingerprint against the app's screen; afterwards use only the pinned key, whatever the server returns. A device's `user_id` is pinned with it.
2. Verify every grant it relies on: device signature, `user_id`/`device_id`, and that **its own entry** carries its own `sign_pub` and `age_recipient`. A grant listing its `processor_id` with a different `age_recipient` is an attack or a bug and MUST be reported.

And for each batch, in `seq` order, before decrypting:
3. Verify the manifest signature with the pinned key, `user_id`/`device_id`, that `seq` is the next one, and that `prev` equals the previous manifest's `manifest_sha256`. A gap is not an error while it is recent (uploads arrive out of order); the processor stops at the gap and retries later, and reports a gap that persists.
4. Verify that grant `grant_seq` is one it verified (step 2), hashes to `grant_sha256`, and that `recipients` matches it.
5. Verify the ciphertext's SHA-256 equals `ciphertext_sha256` and its length equals `size`.
6. If its `processor_id` is not in `recipients`, record the manifest as verified and skip decryption; this is normal for batches sealed before it was added.
7. After decrypting, check the header record's `user_id` and `device_id` equal the manifest's.

A processor treats losing access to a device (`not_granted`, or `granted: false` in `/v1/streams`) as an alarm unless it holds a device-signed grant, newer than every grant that listed it, that leaves it out; then it records the removal and stops downloading batches. It keeps watching `/v1/streams`: when the device lists it again (`granted: true` and a newer verified grant), it resumes, verifying the chain across the batches it could not decrypt. It reports once, as information, that a removed device is still uploading (`max_seq` advancing), so its owner can ask the member whether the removal was intended; a relay withholding a newer grant looks the same, and only that conversation tells them apart.

A processor reports an `active` device with no new batch for 3 days (the app sends at least one batch a day). For `retired` and `purged` devices it reports the status change once instead.

## 8. Known limits
- A batch sealed while a processor was granted may still be uploaded after that processor is removed. The relay will not serve it to that processor, but it remains decryptable by it.
- A removed processor keeps whatever it already downloaded.
- The relay sees who uploads, when, how much, and to which processors.
- The relay can delete or withhold data. Processors detect gaps and silence, not prevent them; each processor's local copy is the second copy.

## 9. Limits

| Limit | Value |
|---|---|
| Invite, card | 1 KiB encoded |
| Enrolment body, manifest | 4 KiB |
| Grant document | 16 KiB |
| Invite lifetime | 7 days |
| Batch ciphertext | 8 MiB default; a deployment may choose a lower value when it is set up, and never changes it afterwards (see `limits`) |
| Decompressed batch | 256 MiB |
| Timestamp window | ±300 s |
| Processors per grant | 8 |
