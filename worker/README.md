# hka relay (Cloudflare Worker)

Implements the relay side of [../spec/README.md](../spec/README.md): ciphertext in R2,
allowlist, grants and batch index in D1. It never sees plaintext.

## Develop

Needs an arm64 Node 24 (`nvm use` reads `.nvmrc`).

```sh
npm ci
npm test            # vitest in the Workers runtime: spec vectors + behaviour tests
npm run typecheck
npm run dev         # local Worker on http://localhost:8787 (local D1/R2 under .wrangler/)
npm run db:migrate:local
```

Tests pin the clock to the vectors' reference time by calling `handle(request, env, now)`
from `src/app.ts`; `src/index.ts` passes `Date.now()`.

## Deploy

`wrangler.jsonc` stays generic. Your account's values go in a copy, `wrangler.deploy.jsonc`, which git
ignores; every remote command below passes `--config wrangler.deploy.jsonc`. R2 must be enabled for the
account first (dashboard → R2).

1. `cp wrangler.jsonc wrangler.deploy.jsonc`.
2. `npx wrangler d1 create hka` and put the printed id in `wrangler.deploy.jsonc`.
3. `npx wrangler r2 bucket create hka-batches`.
4. Add a 30-day retention lock on the `v1/` prefix:
   `npx wrangler r2 bucket lock add hka-batches v1-30-days v1/ --retention-days 30`. It blocks deleting
   or overwriting recent objects; the purge job retries until the lock allows.
5. Set `ADMIN_KEYS` to the JSON printed by `hka-admin init`.
6. `npx wrangler d1 migrations apply hka --remote --config wrangler.deploy.jsonc`, then
   `npx wrangler deploy --config wrangler.deploy.jsonc`. It prints the Worker's URL.
7. Set `ORIGIN` to that origin (`https://hka-relay.<subdomain>.workers.dev`, or a custom domain) and
   deploy again. Then `hka-admin --server <origin> init` saves it for the admin tool.

Keep `wrangler.deploy.jsonc` in step when `wrangler.jsonc` changes (bindings, limits, migrations).

`MAX_BATCH_BYTES` is chosen once and must never be lowered (spec 4.3).

## Checked on real Cloudflare (2026-11-14, Workers free plan, D1 in APAC)

Run with a made-up user, a throwaway device and processor key, then retired and purged:

| Check | Result |
|---|---|
| Enrol, grant, upload, idempotent repeat, processor pull and build | All 200; the processor verified and built 3 batches. |
| Body that does not match `ciphertext_sha256` | 400 `hash_mismatch`, nothing stored; the same seq then uploads correctly. This is the Worker's own check, which runs before R2; R2's `sha256` option (defence in depth) is not reached. |
| A different batch for a stored seq | 409 `seq_conflict`. |
| `onlyIf: If-None-Match: *` on an existing key | With the D1 row removed, different bytes for that seq get 500 `internal` and the stored object is kept; the original bytes are then re-recorded with 200. |
| 7.6 MiB upload | 200 in 2.2 s; 101 ms CPU, 1.5 s wall (`wrangler tail`). Small requests use 2–10 ms CPU. |
| D1 read replication | `disabled`, as the spec's read-consistency rule requires. |

CPU: the free plan's limit is 10 ms per request, and the 7.6 MiB upload used about 100 ms but was not
stopped. Cloudflare does not promise to allow that, so expect `exceededCpu` errors on large batches on
the free plan; Workers Paid (30 s CPU per request) removes the risk. Uploads are retried, so a refused
upload delays a batch rather than losing it.

Clients must send a `User-Agent`: Cloudflare's browser integrity check answers 403 (error 1010) to
Python's default `Python-urllib` agent. `hka`, `hka-admin` and the app send their own.

Purged test data: the relay stops serving it at once; the hourly job deletes its R2 objects once the
30-day lock allows.
