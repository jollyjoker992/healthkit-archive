import { cloudflareTest, readD1Migrations } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";
import keys from "../spec/vectors/keys.json" with { type: "json" };
import documents from "../spec/vectors/documents.json" with { type: "json" };

const admin = keys.keys.find((k) => k.label === "admin 1")!;

export default defineConfig(async () => {
  const migrations = await readD1Migrations("./migrations");
  return {
    plugins: [
      cloudflareTest({
        wrangler: { configPath: "./wrangler.jsonc" },
        miniflare: {
          bindings: {
            TEST_MIGRATIONS: migrations,
            ORIGIN: documents.server,
            ADMIN_KEYS: JSON.stringify({ [admin.id]: admin.pub }),
          },
        },
      }),
    ],
    test: { setupFiles: ["./test/setup.ts"] },
  };
});
