# CLAUDE.md

## Project

AgentPostHub — LangGraph content distribution agent. Python 3.11+, no UI;
the approval gate is CLI-operated.

## Commands

```bash
pip install -r requirements.txt

# tests (no network, isolated sqlite per test)
python -m pytest tests/

# dry-run end to end
python -m agentposthub run --platform telegram --task-id t1 --cities 3 --days 10 --budget 1500
python -m agentposthub approve t1        # also: reject | edit --text "..."

# inspect checkpointed state
python -m agentposthub list
python -m agentposthub show <task_id>

# optional approval API
uvicorn approval_api:app --port 8899
```

## Environment

Secrets/config live in `.env` (gitignored; template in `.env.example`).
`PUBLISH_MODE=dry_run` by default — live mode requires
`COMPOSIO_API_KEY` + `TELEGRAM_CHAT_ID` and posts for real. Never commit
`.env` or `*.db`.

## Architecture notes

- `agentposthub/flow.py` — one graph per publish task, thread_id =
  task_id. Nodes are pure state→patch functions; routing lives in
  `after_*` conditionals. State persists in sqlite (checkpointer +
  Store), so run/approve can be separate processes.
- `agentposthub/composio.py` — the only exit point for platform
  actions. Composio MCP exposes meta-tools only; platform actions route
  through `COMPOSIO_MULTI_EXECUTE_TOOL`. Every publish is followed by
  an independent read-back (chat history, falling back to the Bot API
  delivery receipt for channels — bots cannot read channel history).
- Output contract: platform responses must be `{id, ok, req_id}` and
  echo the idempotency key; violations become structured failures, never
  silent success.
- Trust memory: an incident (read-back mismatch) downgrades the
  platform's trust tier in the Store; `policy_validator` strips links
  while tier = building. Seeded only when absent — do not re-put on
  import.
- Failure handling: bounded retry with backoff at both external
  boundaries (DeepSeek in `generate`, MCP in the gateway); the graph
  records structured `failures` entries instead of crashing mid-task.

## Testing conventions

Tests monkeypatch the gateway seam (`agentposthub.composio.publish` /
`read_back`) — no network in unit tests. `conftest.py` redirects every
storage cache to a throwable sqlite file; if you add a module-level
cache, clear it there.
