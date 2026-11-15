# Connecting agents to the health data

The local Medplum (see [`medplum/README.md`](../medplum/README.md)) serves the family's HealthKit data
as FHIR R4 over REST, GraphQL and MCP. Agents use the **read-only agent client**
(`HKA_FHIR_AGENT_CLIENT_ID` / `_SECRET` in `.env`, written by `python3 medplum/setup.py project`). Its
access policy allows read and search on Patient, Device, Observation, DocumentReference, Binary,
CodeSystem and ValueSet; every write is refused (HTTP 403, or `Forbidden` in GraphQL and MCP).

| Interface | Address | For |
|---|---|---|
| MCP | `http://localhost:8203/mcp/stream` (streamable HTTP), `/mcp/sse` | Agents with MCP support. Tools: `fhir-request`, `search`, `fetch`. |
| GraphQL | `POST http://localhost:8203/fhir/R4/$graphql` | Analysis: ask for exactly the fields needed. |
| REST | `http://localhost:8203/fhir/R4/` | Anything else. |

Tokens come from `POST /oauth2/token` with `grant_type=client_credentials` and last one hour.
`tools/medplum_agent.py` does this, caches the token in `.cache/medplum-agent-token.json` (mode 600,
ignored by git) and renews it five minutes before it expires.

## Skills

The skills are in `.agents/skills/` (`SKILL.md` format: a `name` and `description` header, then
instructions):

- [`healthkit-health-analysis`](../.agents/skills/healthkit-health-analysis/SKILL.md): analysing a
  family member's data (where each metric is, rules that prevent wrong answers, tested GraphQL recipes,
  workout heart rate and routes).
- [`healthkit-archive`](../.agents/skills/healthkit-archive/SKILL.md): operating the backup (processor
  status and alarms, pinning a new phone, FHIR map and load, admin tasks).

| Agent | Finds them through |
|---|---|
| Claude Code | `.claude/skills`, a link to `.agents/skills` |
| Antigravity CLI, Codex, Cursor, Gemini CLI, Copilot CLI and coding agent | `.agents/skills` |
| GitHub Copilot in VS Code | `.github/skills` only: add `ln -s ../.agents/skills .github/skills` locally if you use it |

`AGENTS.md` holds the rules every agent must follow (Medplum, not the day folders; no secrets; ask
before changing the relay). Claude Code reads it through `CLAUDE.md`; Antigravity, Codex, Cursor and
Copilot read it directly; Gemini CLI through the committed `.gemini/settings.json` (`context.fileName`).

On Windows, Git checks out the `.claude/skills` link as a plain file unless symlinks are enabled
(`git config core.symlinks true` and Developer Mode).

## Claude Code (MCP)

The repository's `.mcp.json` declares the server:

```json
{
  "mcpServers": {
    "hka-medplum": {
      "type": "http",
      "url": "http://localhost:8203/mcp/stream",
      "headersHelper": "python3 tools/medplum_agent.py headers"
    }
  }
}
```

`headersHelper` runs at the start of each session, on reconnect and after a 401/403, and prints
`{"Authorization": "Bearer <token>"}` (about 0.2 s with a cached token; the limit is 10 s). It gets no
secret-looking environment variables, which is why the script reads the client secret from `.env`
itself. The file holds no secret and is safe to commit.

One-time setup, interactive:

1. Start Medplum (`cd medplum && docker compose up -d`).
2. Run `claude` in the repository folder, accept the workspace trust prompt (Claude Code does not run a
   project's `headersHelper` before that) and approve the `hka-medplum` server.
3. Check with `/mcp`, or `claude mcp get hka-medplum` in a shell: it should be connected.

Until the folder is trusted, the server shows `Pending approval`, and `claude -p` reports that
`headersHelper` did not run. A garmin-archive Medplum (`garmin-medplum`, port 8103) is a separate
server with its own client; the two never share a token.

## Other agents

Agents that can run shell commands do not need MCP: the skills use
`python3 tools/medplum_agent.py people|graphql|codes|workouts|workout-hr|route`, which gets and renews
the read-only token by itself.

To connect MCP anyway, give the agent the read-only token in an environment variable. A token lasts one
hour, so set it right before starting the agent:

```sh
export HKA_MEDPLUM_TOKEN="$(python3 tools/medplum_agent.py token)"
```

| Agent | Config (not committed; add it yourself) |
|---|---|
| Codex | `~/.codex/config.toml`: `[mcp_servers.hka-medplum]` with `url = "http://localhost:8203/mcp/stream"` and `bearer_token_env_var = "HKA_MEDPLUM_TOKEN"`. |
| Cursor | `.cursor/mcp.json`: `{"mcpServers": {"hka-medplum": {"url": "http://localhost:8203/mcp/stream", "headers": {"Authorization": "Bearer ${env:HKA_MEDPLUM_TOKEN}"}}}}` |
| GitHub Copilot CLI | `~/.copilot/mcp-config.json` with `"type": "http"`, the URL and `"headers": {"Authorization": "Bearer <token>"}` (it reads `.mcp.json` but not `headersHelper`). |
| Antigravity CLI | `~/.gemini/config/mcp_config.json`: `{"mcpServers": {"hka-medplum": {"serverUrl": "http://localhost:8203/mcp/stream", "headers": {"Authorization": "Bearer <token>"}}}}`. Replace the token when it expires; use this user-level file, not a workspace file. |

These formats follow garmin-archive's `docs/agents.md` (checked against each vendor's documentation in
November 2026); check the vendor's documentation if something does not connect. Never commit a file
that contains a token. Do not sign in to MCP with the browser OAuth flow for agent use: that logs in as
the admin user, who can change data.

## Command line

```sh
python3 tools/medplum_agent.py people                                  # who is in the archive, UTC offsets
python3 tools/medplum_agent.py codes sleep                             # meaning and unit of custom codes
python3 tools/medplum_agent.py graphql '{ ObservationList(code: "http://loinc.org|40443-4", _sort: "-date", _count: 7) { effectiveDateTime valueQuantity { value unit } } }'
python3 tools/medplum_agent.py workouts --person <name> --from 2026-09-01T00:00:00+07:00
python3 tools/medplum_agent.py workout-hr <workout uuid>
python3 tools/medplum_agent.py route <workout uuid> --no-gps
```

Date filters must carry the person's UTC offset (the examples use `+07:00`; `people` prints each person's); ranges use
`_filter: "date ge <from> and date lt <to>"`.
