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

1. `npx wrangler d1 create hka` and put the printed id in `wrangler.jsonc`.
2. `npx wrangler r2 bucket create hka-batches`.
3. Add a 30-day retention lock on the `v1/` prefix (dashboard: R2 → bucket → Settings →
   Bucket lock rules). It blocks deleting or overwriting recent objects; the purge job
   retries until the lock allows.
4. Set `ORIGIN` in `wrangler.jsonc` to the deployed origin (`https://hka-relay.<account>.workers.dev`
   or a custom domain), and `ADMIN_KEYS` to the JSON printed by `hka-admin init`.
5. `npx wrangler d1 migrations apply hka --remote`, then `npm run deploy`.

`MAX_BATCH_BYTES` is chosen once and must never be lowered (spec 4.3).

## To verify on real Cloudflare (not covered by local tests)

- A put whose body does not match the `sha256` option is rejected (the Worker also
  checks the hash itself before writing, so this is defence in depth).
- `onlyIf: If-None-Match: *` returns `null` for an existing key.
- CPU time of an 8 MiB upload on your plan (the free plan allows 10 ms per request).
- D1 is used without read replication, as the spec's read-consistency rule requires.
