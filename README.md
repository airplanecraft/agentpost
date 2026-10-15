# AgentPostHub

A multi-agent content distribution system built on LangGraph: it plans,
generates, gates, publishes, and verifies content across platforms
through one orchestrated pipeline — with human approval gates, tiered
rule enforcement, memory-driven trust, and tool output contracts.

This repo is the orchestration core: a runnable Python package with a
CLI approval surface (no UI), durable interrupt/resume, and a Composio
MCP gateway as the single exit point for all platform actions.

## How It Works

```
Scheduler → Discover → Generate(model-led, tool choice)
   → Dedup Guard(semantic, bounded regen)
   → ⏸ HUMAN: Approval Gate (CLI)
   → Policy Validator(three-tier rules, memory-driven)
   → Publish(Composio MCP, idempotency key)
   → Verify(read-back — confirm, never assume)
   → Record(idempotent ledger write)
        ↘ Failure SM(bounded retry → escalation → memory update)
```

| Mechanism | Where |
|---|---|
| Human approval gate with durable interrupt/resume (can hang for days) | `approve` node + SqliteSaver |
| Soft vs hard tool rules (prompt policy vs visibility gating) | `tools_for()` + `generate` |
| Memory-driven trust (incident → downgrade → next decision changes) | `Store` + `policy_validator` |
| Tool contracts (input schema / output validation / read-back) | `_check_output_contract`, `verify` |
| Phantom-publish protection (independent read-back, delivery receipts) | `agentposthub/composio.py` |
| Bounded retry with backoff (DeepSeek + Composio boundaries) | `generate`, `composio` |
| Idempotent ledger writes | `record` + sqlite primary keys |

Full design: [docs/design.md](docs/design.md). Diagram:
[workflow.png](workflow.png) (source: `workflow.drawio`).

## Quick Start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# fill in: DEEPSEEK_API_KEY, COMPOSIO_API_KEY
# optional: TELEGRAM_CHAT_ID, IMAGE_GATEWAY_URL
# default PUBLISH_MODE=dry_run sends nothing real

python -m pytest tests/          # unit tests, no network needed
```

### Dry run (no real posts)

```bash
python -m agentposthub run --platform telegram --cities 3 --days 10 --budget 1500
# ... suspends at the approval gate, prints task_id + draft ...
python -m agentposthub approve <task_id>     # or: reject | edit --text "..."
```

Generation is a real DeepSeek call (including autonomous tool choice for
images). Publishing is simulated and clearly marked `via: dry-run`.

### Live run (real Telegram post)

Requires: Composio account with the Telegram app connected (the bot
must be a member of the target chat), and in `.env`:

```
PUBLISH_MODE=live
TELEGRAM_CHAT_ID=-100xxxxxxxxxx
```

Then the same two commands — approve really publishes through the
Composio MCP gateway, verifies delivery, and records the ledger row.

### Why the resume can happen in another process

The graph suspends inside `approve` via LangGraph `interrupt()`, with
all state checkpointed to sqlite. `run` and `approve` are separate
processes; the approval can happen minutes or days later, after a
crash, or from a different machine sharing the DB. Try it: run, kill
the shell, approve later.

## Configuration

Everything lives in `.env` (gitignored). See [.env.example](.env.example).

| Variable | Purpose |
|---|---|
| `DEEPSEEK_API_KEY` | content generation (required) |
| `COMPOSIO_API_KEY` | platform actions via Composio MCP (required for live) |
| `COMPOSIO_MCP_URL` | MCP endpoint (default: `https://connect.composio.dev/mcp`) |
| `PUBLISH_MODE` | `dry_run` (default) or `live` |
| `TELEGRAM_CHAT_ID` | delivery target for live telegram publish |
| `IMAGE_GATEWAY_URL` | optional own image gateway (`{prompt,n}` → `{ok,job_id}`) |
| `APH_DB_PATH` / `APH_STORE_PATH` | override ledger / memory store locations |

## Project Layout

```
agentposthub/
  config.py       .env loading, settings, fail-loud validation
  flow.py         the LangGraph workflow (nodes, contracts, routing)
  composio.py     Composio MCP gateway: publish + read-back, bounded retry
  cli.py          run / list / show / approve / reject / edit
approval_api.py   optional FastAPI bridge for external approval surfaces
render_diagram.py regenerate workflow.png
tests/            contract, dedup, publish/verify unit tests
docs/design.md    full system design
```

## Platform Support

Telegram is fully implemented (publish + read-back). The gateway is a
registry (`PUBLISH_ACTIONS` / `READ_BACK_ACTIONS` in
`agentposthub/composio.py`): adding a platform is one action mapping
plus a params builder — Reddit and Facebook entries are the next step
and the failure paths around them are already covered by the contract
tests.

## Development

```bash
python -m pytest tests/                       # unit tests
python render_diagram.py                      # regenerate workflow.png
uvicorn approval_api:app --port 8899          # optional HTTP approval surface
```

## License

MIT — see [LICENSE](LICENSE).
