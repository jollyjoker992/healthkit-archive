# hka and hka-admin

Python 3.11+. Dependencies: `cryptography` (P-256), `pyrage` (age), `segno` (terminal QR codes).
`pyrage` ships no Alpine (musl) wheels; use a Debian-based image for Docker.

```sh
python3 -m venv .venv && .venv/bin/pip install -e cli
```

## hka-admin (relay owner)

The admin key lives in `$HKA_ADMIN_DIR` (default `~/.config/hka-admin`), apart from any
processor. It enrols devices; it cannot read data.

```sh
hka-admin --server https://hka-relay.example.workers.dev init   # prints ADMIN_KEYS for wrangler.jsonc
hka-admin invite --name "Mẹ"                                     # QR for a new person's phone
hka-admin invite --name "Mẹ" --user u_…                          # QR for that person's next phone
hka-admin users
hka-admin retire d_…                                             # lost or replaced phone
hka-admin purge u_…
```

## hka (processor)

Data directory `$HKA_DATA_DIR` (default `./data`).

```sh
hka --server https://hka-relay.example.workers.dev init --name "Hieu's Mac"
hka card --qr                 # members scan this in the app and compare the fingerprint
hka init --recovery --out /Volumes/USB/hka-recovery --name Recovery   # offline second recipient
hka pull                      # new devices are listed with their fingerprint
hka confirm d_…               # after comparing with the app's screen
hka run                       # pull + build; schedule this
hka status [--json]
hka verify                    # re-check signatures, chain and hashes of everything stored
hka export --user u_… --out mom.zip [--from 2026-11-01 --to 2026-11-30]
```

Exit codes: 0 ok, 1 partial (alarms or errors), 2 configuration, 3 another run holds the lock.

### Data directory

```
data/
  config.json, pins.json
  keys/sign.pem, keys/age.txt          age.txt is a standard age identity: back it up offline
  inbox/<user>/<device>/
    grants/<grant_seq>.json            device-signed grants, verified
    batches/<seq>.manifest.json        signed manifests, verified
    batches/<seq>.age                  ciphertext (only batches addressed to this processor)
    state.json                         verified prefix, chain head, gaps, alarms
  users/<user>/
    manifest.json                      day status, file hashes, archives
    index.sqlite                       uuid -> day/file, tombstones, census (rebuildable)
    days/YYYY/YYYY-MM-DD/<HealthKit type>.ndjson, summary.json, _day.json
    archives/YYYY/YYYY-MM-DD.tar.gz    reproducible archive of a final day
```

`inbox/` is the immutable, encrypted copy: back it up anywhere. `users/` is derived from it
and holds plaintext health data.

### Restore with stock tools only

```sh
age -d -i keys/age.txt inbox/u_…/d_…/batches/0000000001.age | gunzip
```

### Rules it follows (spec section 7)

- A device key is used only after `hka confirm`; afterwards a different key from the relay is an alarm.
- Grants are accepted only if signed by the pinned key, and only if this processor's own entry
  carries its own keys (a swapped card is reported).
- Batches are verified in `seq` order: signature, hash chain, grant, ciphertext hash. A gap waits;
  a gap older than 24 h, a broken chain, or 3 days without a batch is an alarm.
- Losing access is an alarm unless a device-signed grant removed this processor.
- A purge reported by the relay never deletes local data.

## Tests

```sh
.venv/bin/python -m unittest discover -s cli/tests
HKA_E2E=1 HKA_NODE_BIN=~/.nvm/versions/node/v24.22.2/bin .venv/bin/python -m unittest discover -s cli/tests -p test_e2e_local.py
```

The end-to-end test runs the real Worker under `wrangler dev` (local D1 and R2) with a software
device (`tests/fakedevice.py`).
