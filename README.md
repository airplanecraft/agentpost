# AgentPostHub

A production multi-agent system that plans, generates, reviews, and
publishes travel content across multiple platforms — one brief in,
differentiated platform-native posts out, behind human approval gates,
with full tracing and evaluation.

**One brief** ("Beijing, 10 days, $1500, one adult + one 8-year-old")
fans out to per-platform worker agents: a Reddit worker writes a
text-only, link-free first-person post (the image tool is structurally
absent from its menu — hard rule), while Telegram and Facebook workers
may earn media through the model's own tool choice. Every publish goes
through a human gate, carries an idempotency key, and is verified by an
independent read-back.

```
brief ─▶ grounding ─▶ [draft worker ×N] ─▶ ⏸ human gate ─▶ [publish worker ×N]
                      (parallel, Send)                    (parallel, Send)
                                                             │
                                              ledger ◀─ read-back verify ─┘
                                                 └─▶ ⏸ escalation gate (retry/drop)
```

## What makes it interesting

| Area | Highlights |
|---|---|
| **Multi-agent orchestration** | Deterministic supervisor (policy code, zero LLM in control flow) + model-led platform workers, parallel fan-out via LangGraph `Send`, append-reducer result merging |
| **Dual agent protocols** | **MCP** for agent-to-tools (Composio gateway to Telegram/Facebook) and **A2A** for agent-to-agent (the grounding source is a discoverable agent: card discovery + `message/send` + structured artifacts) |
| **Memory, three loops** | Thread-scoped checkpoints (durable interrupts, cross-process resume); cross-thread Store (trust tiers, tool-health circuit breakers, distilled rules with a full lifecycle + effectiveness counters); vector memory (local Chroma + Qwen embeddings) for semantic dedup |
| **Tool contracts** | Output contract `{id, ok, req_id}` with idempotency-key echo; phantom-publish protection via independent read-back; fail-soft planner contract with typed errors |
| **Failure recovery** | Bounded retry with backoff at both external boundaries, circuit breakers per dependency, batch escalation gate whose retry re-Sends only failed platforms with unchanged idempotency keys |
| **Human-in-the-loop** | Three durable interrupt gates: batch content approval (one decision covers all platforms), escalation retry/drop, rule approval (LLM-distilled rules cannot self-activate) |
| **Safety controls** | Hard rules in code not prompts (tool visibility gating, link stripping); Reddit public-community posting is credential-gated by default; kill switch; per-task budget breaker; dry-run default |
| **Tracing** | Self-hosted Langfuse: node spans (auto), LLM generations with token usage, MCP tool spans — one trace per task across both invoke halves, human-gate latency measurable in the trace |
| **Evaluation, two levels** | Product eval (deterministic checks + optional LLM judge) and a **flow benchmark**: fixed scenario suite × whole-graph runs × three metric layers, labelled reports, `--diff` for model-swap experiments |

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # fill keys; PUBLISH_MODE=dry_run by default
python -m pytest tests/     # 70 tests, no network needed
```

Run a task (three platforms, one approval):

```bash
python -m agentposthub run --platform telegram,facebook,reddit \
    --task-id demo1 --days 10 --budget 1500 \
    --notes "Beijing 10-day family trip, one adult + one child"
python -m agentposthub list          # see suspended drafts
python -m agentposthub approve demo1 # or reject / edit --text "..."
python -m agentposthub trace demo1   # observability tree, true call order
```

### Secrets policy

All keys live in `.env` — **gitignored**. The committed template is
`.env.example` (every variable documented, no real values). Never
commit `.env`, `*.db`, `aph_vectors/`, or `evalruns/` (all ignored).

## Configuration

| Variable | Purpose |
|---|---|
| `DEEPSEEK_API_KEY` | generation LLM (required) |
| `DEEPSEEK_MODEL` | model name — the model-swap experiment switch |
| `COMPOSIO_API_KEY` / `COMPOSIO_MCP_URL` | platform actions via Composio MCP |
| `PUBLISH_MODE` | `dry_run` (default) or `live` |
| `TELEGRAM_CHAT_ID` / `FACEBOOK_PAGE_ID` | live publish targets (own channel/page) |
| `REFERENCE_SITE_URL` | A2A grounding agent (agent card + /a2a) |
| `LANGFUSE_PUBLIC_KEY` / `SECRET_KEY` / `HOST` | tracing; unset = fully off |
| `DASHSCOPE_API_KEY` or ollama (`QWEN_EMBED_URL`) | vector-memory embeddings |
| `AGENT_JUDGE` | 1 = LLM judge in eval + advisory verdict at the gate |
| `TASK_BUDGET_USD` / `APH_RECURSION_LIMIT` | budget breaker / explicit superstep cap |

## Documentation

- [docs/architecture.md](docs/architecture.md) — components, memory
  layers, data stores, supervisor/worker pattern
- [docs/agent-flow.md](docs/agent-flow.md) — the flow with diagrams:
  nodes, gates, Send fan-out, both invoke halves
- [docs/orchestration.md](docs/orchestration.md) — routing decisions,
  idempotency, retries, escalation semantics
- [docs/design.md](docs/design.md) — original system design
- [docs/langfuse-self-host.md](docs/langfuse-self-host.md) — tracing
  stack setup incl. the v4 write-mode pitfall

## Project layout

```
agentposthub/
  flow.py         the graph: nodes, Send fan-out, gates, contracts
  composio.py     MCP gateway: publish + two-tier read-back, retries
  tripplanner.py  A2A client: agent-card discovery, message/send
  vectormem.py    Chroma + Qwen embeddings (semantic dedup corpus)
  distill.py      incidents → LLM rule proposals → human gate
  breaker.py      cross-session tool-health circuit breaker
  judge.py        LLM-as-judge (quality layer, never control flow)
  evalharness.py  product eval; evalflow.py — flow benchmark harness
  pricing.py      call-time cost accounting; tracing.py — Langfuse
  cli.py          run/list/show/trace/approve/retry/rules/pause/eval...
tests/            70 tests (gateway seam mocked, no network)
docs/             architecture, flow, orchestration, design
workflow.png      rendered graph (source: workflow.drawio)
```

## Multi-model evaluation

The flow benchmark runs the whole graph under any model and diffs the
reports — model swap is one env var:

```bash
python -m agentposthub evalflow --label deepseek-chat
DEEPSEEK_MODEL=deepseek-reasoner \
  python -m agentposthub evalflow --label deepseek-reasoner
python -m agentposthub evalflow --diff deepseek-chat deepseek-reasoner
```

| metric | deepseek-chat | reasoner (before fix) | reasoner (after) |
|---|---|---|---|
| tool_call_rate | 0.667 | 0.0 | 0.0 |
| placeholder_rate | 0.0 | **1.0** | **0.0** |
| number_consistency | 1.0 | 0.0 | 0.0 |
| system/orchestration/safety | all 1.0 | all 1.0 | all 1.0 |
| wall_s (S1) | 7.8 | 55 | 69 |

The benchmark surfaced a real model-dependent defect (the placeholder
chase only fired under tool calls), which was generalized and
re-measured — placeholder 1.0 → 0.0. The residual consistency drift is
documented, not papered over.

## License

MIT — see [LICENSE](LICENSE).
