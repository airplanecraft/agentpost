# Self-hosting Langfuse for AgentPostHub tracing

The tracing layer (`agentposthub/tracing.py`) is host-agnostic. This is
the local self-hosted setup that was actually run and debugged; the
same `.env` keys work against Langfuse Cloud by pointing
`LANGFUSE_HOST` at it instead.

## Stack

Official compose file + a small override (port remaps + the write-mode
fix below):

```bash
mkdir -p ~/work/langfuse-local && cd ~/work/langfuse-local
curl -fsSL -o docker-compose.yml \
  https://raw.githubusercontent.com/langfuse/langfuse/main/docker-compose.yml

cat > docker-compose.override.yml <<'YAML'
services:
  postgres:
    ports: !override          # host 5432 already taken
      - "127.0.0.1:15432:5432"
  langfuse-web:
    ports: !override          # host 3000 already taken
      - "127.0.0.1:13000:3000"
    environment:
      NEXTAUTH_URL: http://localhost:13000
      LANGFUSE_MIGRATION_V4_WRITE_MODE: dual
  langfuse-worker:
    environment:
      LANGFUSE_MIGRATION_V4_WRITE_MODE: dual
YAML

docker compose up -d
# first boot: web runs 454 prisma migrations; restart the worker AFTER
# web is healthy — a worker that boots first fails its background
# migrations to the half-initialized schema (race).
```

Then: sign up at http://localhost:13000, create a project, create API
keys, and put them in `.env`:

```
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=http://localhost:13000
```

## Gotchas found the hard way

1. **v4 `events_only` default leaves the UI/API empty on a fresh
   install.** OTel ingestion writes the new `events_full`/`events_core`
   tables correctly, but the traces API and UI read the legacy
   `traces`/`observations` tables — so everything looks lost while the
   data is actually in ClickHouse. Fix: `LANGFUSE_MIGRATION_V4_WRITE_MODE=dual`
   on **both** web and worker (dual-writes legacy + events). Symptom
   tracked in langfuse#14827.
2. **One bad score rejects its whole ingestion batch server-side.** A
   score carrying BOTH `trace_id` and `session_id` (API is XOR) gets
   the entire OTel batch 400'd — spans included. `tracing.score()`
   enforces the XOR for this reason.
3. **OTel trace ids must be 32-char hex.** Task ids like
   `eval_s1_00` crash span creation; `tracing.hex_trace_id()` maps
   task ids to deterministic sha1-based hex ids so spans, traces and
   scores stay correlated.
4. **Ordering:** worker after web. Postgres/clickhouse/redis/minio
   healthchecks gate the first boot, but a worker restart during web's
   migration phase must be followed by another worker restart.

## Verify ingestion end to end

```bash
python3 - <<'PY'
from agentposthub.tracing import span, score, flush
with span("smoke", trace_id="smoke1"):
    pass
score("smoke1", "smoke_ok", 1.0)
flush()
PY
sleep 10
curl -su "$PK:$SK" "$LANGFUSE_HOST/api/public/traces?limit=3"   # should list it
```

## Generating dashboard data

```bash
python -m agentposthub eval -n 3 --session s1
```

runs real generations to the approval gate (nothing publishes) and
pushes per-trace scores; the Langfuse UI then has per-task span trees
(graph nodes + the DeepSeek generation with token usage + Composio MCP
calls) and score trends.
