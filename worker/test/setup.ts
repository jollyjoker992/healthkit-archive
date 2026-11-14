import { applyD1Migrations, env } from "cloudflare:test";

declare global {
  namespace Cloudflare {
    interface Env {
      DB: D1Database;
      BATCHES: R2Bucket;
      ORIGIN: string;
      ADMIN_KEYS: string;
      TEST_MIGRATIONS: import("cloudflare:test").D1Migration[];
    }
  }
}

await applyD1Migrations(env.DB, env.TEST_MIGRATIONS);

// Fresh storage for every test.
import { beforeEach } from "vitest";

beforeEach(async () => {
  for (const table of ["batches", "grant_processors", "grants", "invites_used", "devices", "users"]) {
    await env.DB.prepare(`DELETE FROM ${table}`).run();
  }
  let cursor: string | undefined;
  do {
    const listed = await env.BATCHES.list({ cursor });
    if (listed.objects.length) await env.BATCHES.delete(listed.objects.map((o) => o.key));
    cursor = listed.truncated ? listed.cursor : undefined;
  } while (cursor);
});
