---
name: healthkit-archive
description: Operate the healthkit-archive backup - check the processor's status and alarms, pull and build new batches, pin a family member's new iPhone, map and load FHIR into the local Medplum, invite or retire phones with hka-admin. Use when the user asks about the backup itself (is it working, why is a device stuck, add a phone, reload Medplum). NOT for questions about the health data (steps, sleep, heart rate, workouts, weight): those go to the healthkit-health-analysis skill, which reads the data through Medplum.
---

# healthkit-archive

Work from the repository root. The rules every agent must follow are in `AGENTS.md`; the wire
protocol is `spec/README.md`; the CLI is described in `cli/README.md`; the FHIR mapping in
`docs/fhir-mapping.md`.

How it fits together: each family member's iPhone app encrypts their Health data and uploads it to
the relay (a Cloudflare Worker). The relay stores only ciphertext. The **processor** (`hka`, on the
family computer) downloads, verifies and decrypts it into day folders, then maps the days to FHIR and
loads them into the local Medplum. `hka-admin` is the relay owner's tool: it invites phones and never
decrypts anything.

## Find the processor's data directory

`hka` (installed with `pip install -e cli`, see `cli/README.md`; inside the repository's virtual
environment it may be `.venv/bin/hka`) uses `$HKA_DATA_DIR`, else `./data`. If `hka status` says "no
processor identity", the processor lives elsewhere: ask the owner where, or look for a folder holding
`keys/sign.pem` and `config.json`, then `export HKA_DATA_DIR=<that folder>` (or pass `--data-dir` to
every command). Never create a second processor with `hka init` to work around it.

```sh
hka status --json
```

No network, no side effects. For each device report `pin` (`pending` means not yet confirmed),
`server_status`, `granted`, `verified_seq` against `server_max_seq`, `gap_since` and
`last_advanced_at`; for each user `days`, `final`/`provisional`, `range` and `census_mismatches`.

## Get new data

```sh
hka run            # pull + build; safe to repeat
hka fhir map       # changed days only; --user u_... for one person
hka fhir load      # into Medplum with the loader client from .env
```

- `run` exits `0` ok, `1` alarms or errors, `2` configuration, `3` another run holds the lock (wait;
  never delete the lock file).
- A full history (thousands of days) takes a few minutes to build and about 10 minutes to load; run
  long loads in the background. Loading again is safe: unchanged days are skipped, every entry is an
  upsert.
- `fhir map --force` re-maps every day (after a mapping change). Never edit files under `users/` or
  `inbox/` by hand.

## Read the alarms

| Message | Meaning | What to do |
|---|---|---|
| `new device d_… (Name, iPhone), fingerprint XXXX-…` / `awaits hka confirm` | A phone enrolled and is not trusted yet. | See "Pin a new phone". |
| `the relay no longer lists this device` | Expected after that phone started over, was purged, or the relay was reset. | Tell the owner; it repeats on every pull. Never delete its local data. |
| `no new verified batch since …` (3 days) | The phone stopped uploading: app not opened, Health access off, signing profile expired, or the phone is gone. | Ask the member to open the app; check the app's status screen. |
| `stuck at seq N (relay has up to M)` | A gap: one upload missing on the relay while later ones arrived. Normal for minutes; an alarm after 24 h. | Usually clears when the phone retries. If it persists, ask the member to open the app. |
| `hash chain broken`, `not signed by the pinned device key`, `different key or user than the one pinned`, `two different device-signed grants`, `grant … lists this processor with a different key` | Possible tampering or a bug. | Stop and report to the owner word for word. Do not re-pin, do not delete anything. |
| `access lost without a device-signed grant` | The relay stopped serving this processor without the member removing it. | Report to the owner; the relay may be withholding data. |
| `removed as a processor by device-signed grant` | The member removed this computer in the app. | Information only. |
| `census mismatches` in status | The phone's weekly count differs from the archive for some days. | Report the days; often late-synced data that the next daily re-send fixes. |

## Pin a new phone (the one security step that needs a person)

A new device must be confirmed by comparing fingerprints, so the relay cannot slip in a key of its
own. Never confirm from the relay's word alone.

1. `hka pull` prints `new device d_… fingerprint XXXX-XXXX-XXXX-XXXX-XXXX`.
2. Ask the owner to read the fingerprint on that phone: Health Backup → Backup Details → This iPhone →
   Fingerprint.
3. Only if they match: `hka confirm d_… --fingerprint XXXX-…`, then
   `run`, `fhir map`, `fhir load`.

## Admin tasks (hka-admin, changes the relay: ask first)

```sh
hka-admin users                          # read-only: users, devices, storage
hka-admin invite --name "Mom"             # a new person
hka-admin invite --name "Mom" --user u_… # that person's next phone
hka-admin retire d_…                     # a lost or replaced phone
hka-admin purge u_…                      # delete a person's data from the relay
```

- `users` is read-only and safe. Everything else changes the shared relay: ask the owner first.
  `purge` cannot be undone and needs an explicit request naming the person.
- The admin key is `admin.pem` in `$HKA_ADMIN_DIR` (default `~/.config/hka-admin`); it signs invites. Never print it, never create a
  new one (`init`) unless asked: the relay only trusts the key in its configuration.
- An invite is single use and valid 7 days. Show it as a QR image for the phone to scan.

## Medplum

```sh
cd medplum && docker compose ps          # status; up -d to start
python3 medplum/setup.py check           # server, project, both clients, terminology
```

Rebuilding Medplum from scratch is `docker compose down -v` in `medplum/` (deletes every loaded
resource), `docker compose up -d`, `python3 setup.py project` and `project --test`, then `hka fhir load`
for each person. Ask before `down -v`. It must only ever touch the `hka-medplum` compose project; a
garmin-archive stack (`garmin-medplum`, port 8103) may run beside it.

## Rules

- Questions about the health data go to the **healthkit-health-analysis** skill, through Medplum.
  Day folders (`users/<u>/days/`) hold decrypted health data: do not open them to answer questions
  unless the owner asks.
- The processor never deletes local data because the relay says so (purged, no longer listed).
- Never print or copy `keys/`, `~/.config/hka-admin/admin.pem`, `.env`, `.env.test` or
  `medplum/.env`.
- Cloud backup ([docs/backup.md](../../../docs/backup.md)): `hka status` shows each target per user
  (`backup r2: 412/412 final days, up to date`). `hka run` backs up when the owner configured a
  target; running `hka backup` or `hka restore` yourself sends or fetches data: ask first.
- Deploying the Worker or changing the relay (invites, retire, purge, D1/R2) changes shared state:
  ask first. `hka-admin users` is read-only; `hka pull`/`run` only download, plus this processor's
  signed receipt that tells the phones what it holds.
