# Local Medplum

A Medplum FHIR server on this machine for the FHIR view of the archive. It is garmin-archive's stack
(`medplum/` there), based on Medplum's official `docker-compose.full-stack.yml`, pinned to **Medplum
5.2.17** and reachable only from localhost. It uses its own compose project (`hka-medplum`), volumes and
ports, so it runs beside garmin-archive's stack without touching it.

| Service | Address | Notes |
|---|---|---|
| FHIR API | http://localhost:8203/fhir/R4 | garmin-archive's is on 8103. |
| Admin app | http://localhost:3200 | Sign in with `MEDPLUM_ADMIN_EMAIL` / `MEDPLUM_ADMIN_PASSWORD` from `medplum/.env` (a Medplum super admin). |
| MCP | http://localhost:8203/mcp/stream (and `/mcp/sse`) | Beta feature, enabled with `MEDPLUM_MCP_ENABLED`. |
| Postgres, Valkey | not published | Reachable only inside the compose network. |

## First start

```sh
cd medplum
python3 setup.py init            # writes medplum/.env with random passwords
docker compose up -d             # first start runs migrations; allow a few minutes
python3 setup.py project         # project, policy, clients, terminology → ../.env
python3 setup.py project --test  # a separate test project → ../.env.test (for the end-to-end tests)
python3 setup.py check           # verifies both clients and the CodeSystem (add --test for the test project)
```

`setup.py project` is idempotent. It creates, or finds again:

| What | Name | Purpose |
|---|---|---|
| Project | `HealthKit Archive` | Holds the family's resources. Owned by the admin user. |
| ClientApplication | `healthkit-archive-loader` | Read-write. Used by `hka fhir load`. |
| AccessPolicy | `healthkit-archive-agent-read-only` | Read and search only, on Patient, Device, Observation, DocumentReference, Binary, CodeSystem, ValueSet. |
| ClientApplication | `healthkit-archive-agent` | Uses that policy. For AI agents and other read-only consumers. |
| CodeSystem, ValueSet | `hka-metrics`, `healthkit-type` (version = mapping version) | From `cli/hka/terminology/`, so `$lookup`, `$validate-code` and `$expand` work. `hka fhir load` updates them when they change. |

With `--test` the same is created under `HealthKit Archive Test`, with `-test` client names. The
end-to-end tests load made-up people into that project only, never into the real one. Each run adds a
new made-up person there; to clear them, delete the test project in the admin app (Project Admin) and
run `python3 setup.py project --test` again.

The credentials go to the repository's `.env` (`.env.test` for the test project):
`HKA_FHIR_URL`, `HKA_FHIR_PROJECT_ID`, `HKA_FHIR_CLIENT_ID` / `_SECRET` (loader) and
`HKA_FHIR_AGENT_CLIENT_ID` / `_SECRET` (agent). Both files are mode `600` and ignored by git. `hka` reads
`HKA_*` settings from `.env` in the folder it runs in, so after this step `hka fhir map && hka fhir load`
works from the repository root.

A client gets an access token with the OAuth2 client-credentials grant:

```sh
curl -s http://localhost:8203/oauth2/token \
  -d grant_type=client_credentials -d client_id=$ID -d client_secret=$SECRET
```

Sign-in is limited to a few attempts a minute; `setup.py` waits and retries when Medplum says so.

## Verified on this setup (2026-11-14)

- `/healthcheck` reports `5.2.17-4e75ed3`, Postgres and Redis up; the admin app on 3200 talks to 8203.
- Both projects, their clients and the terminology are created; both client tokens work.
- `cli/tests/test_e2e_local.py` against the test project: a day is loaded as one transaction of
  conditional updates, conditional references resolve to `Patient/<id>`, loading again changes nothing,
  and a sample deleted on the phone is removed from the server.

## Rate limits

The compose file raises Medplum's defaults for a single-user, localhost-only server, as garmin-archive
does (FHIR quota 10,000,000 points per minute per client, 600,000 HTTP requests per minute, 300
token requests per minute). The password-login limit is left at Medplum's default. `hka fhir load`
waits and retries when a transaction comes back with 429 entries.

## Day-to-day

```sh
docker compose ps                   # status
docker compose logs -f medplum-server
docker compose stop                 # stop, keep data
docker compose start
```

Data lives in two Docker volumes: `hka-medplum_postgres-data` (all FHIR resources) and
`hka-medplum_binary-data` (route GPX files). Everything in them can be rebuilt from the day folders with
`hka fhir map --force && hka fhir load`, so they need no backup of their own.

To start over: `docker compose down -v` (deletes both volumes and every loaded resource; ask the owner
first), then `docker compose up -d` and `python3 setup.py project` (and `--test`). The passwords in
`medplum/.env` are reused; the client ids and secrets in `.env` and `.env.test` are replaced.

## Upgrading Medplum

Change the two image tags `medplum/medplum-server:5.2.17` and `medplum/medplum-app:5.2.17` together, and
`@medplum/core` / `@medplum/definitions` in `tools/fhir-validate/package.json` to the same version. The
server migrates the database on start. Read the release notes first: https://github.com/medplum/medplum/releases

## Notes

- The server image runs as uid 1000 and has no shell. The one-shot `binary-permissions` container gives
  the Binary volume to that uid; without it, Binary uploads fail with `EACCES`.
- The admin app image takes the API address from `MEDPLUM_BASE_URL` at start, which is how it reaches
  port 8203.
- Medplum creates six default AccessPolicies in every project; they are not ours and are left alone.
