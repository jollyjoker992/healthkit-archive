import { handle } from "./app";
import type { Env } from "./config";
import { runPurgeJob } from "./purge";

export default {
  fetch(request: Request, env: Env): Promise<Response> {
    return handle(request, env, Date.now());
  },
  async scheduled(_controller: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(runPurgeJob(env, Date.now()).then((r) => console.log("purge job", JSON.stringify(r))));
  },
} satisfies ExportedHandler<Env>;
