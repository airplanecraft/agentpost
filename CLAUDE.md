# CLAUDE.md

## Project

AgentPostHub — a multi-agent content distribution system: a
deterministic supervisor orchestrates model-led platform workers
(LangGraph, Send fan-out) behind durable human gates, publishing via
an MCP gateway (Composio) with grounding from an A2A agent. Python
3.11+. No UI: the gates are CLI-operated.

## Commands

```bash
pip install -r requirements.txt
python -m pytest tests/                      # 69 tests, no network

# a task: three platforms, one approval
python -m agentposthub run --platform telegram,facebook,reddit \
    --task-id t1 --days 10 --budget 1500 --notes "..."
python -m agentposthub list                  # suspended at gate #1
python -m agentposthub approve t1            # reject | edit --text "..."
python -m agentposthub show t1               # checkpointed state
python -m agentposthub trace t1              # observability tree, call order

# gate #2 (failed publishes): retry is targeted + idempotent
python -m agentposthub retry t1              # drop t1

# distilled-rule lifecycle (gate #3)
python -m agentposthub rules [--all]         # pending proposals
python -m agentposthub approve-rule <key>    # reject-rule | retire-rule

# guardrails
python -m agentposthub pause --reason "..."  # kill switch (fail-closed)
python -m agentposthub resume

# evaluation
python -m agentposthub eval -n 3 --session s1            # product eval
python -m agentposthub evalflow --label <model>          # flow benchmark
python -m agentposthub evalflow --diff A B               # model swap

# optional HTTP approval bridge
uvicorn approval_api:app --port 8899
```

## Environment

All config in `.env` (gitignored; template `.env.example`). Key
switches: `PUBLISH_MODE=dry_run` default — live requires
`TELEGRAM_CHAT_ID`/`FACEBOOK_PAGE_ID`; `DEEPSEEK_MODEL` is the
model-swap switch; `LANGFUSE_*` off = tracing fully off; vector
memory needs `DASHSCOPE_API_KEY` OR ollama (`QWEN_EMBED_URL`) —
absent = semantic dedup degrades to its stub. Never commit `.env`,
`*.db`, `aph_vectors/`, `evalruns/`.

## Architecture map

```
flow.py        the graph: retrieve → [Send platform_draft ×N] → ⏸approve
               → [Send platform_publish ×N] → record_all → escalate|END
composio.py    MCP gateway: publish + two-tier read-back (telegram
               receipt tier / facebook history tier), bounded retry
tripplanner.py A2A client: agent-card discovery → message/send;
               data part = structured plan, text-only = no_draft
vectormem.py   Chroma + Qwen embeddings: published-draft corpus for
               semantic dedup; rules indexed with their lifecycle
distill.py     incidents → LLM rule proposals → human gate;
               applied_count / failures_while_applied stamps
breaker.py     cross-session tool-health circuit breaker (Store)
judge.py       LLM-as-judge: product eval + advisory verdict at the
               gate — signal, NEVER routing
evalflow.py    scenario suite (S1 normal / S2 contract drift / S3
               planner outage / S4 safety) × 3 metric layers
pricing.py     call-time cost: usage × price table (tokens AND usd)
tracing.py     Langfuse spans/scores; trace_id persisted in FlowState
```

Docs: `docs/architecture.md` (components/stores/memory loops),
`docs/agent-flow.md` (diagrams), `docs/orchestration.md` (decisions,
identity keys, retry generations).

## Critical invariants (break these and the system lies)

1. **Interrupt replay**: `Command(resume)` re-executes the interrupted
   node FROM THE TOP. Code before `interrupt()` must be idempotent
   (`INSERT OR REPLACE`; deletes on both paths).
2. **Append-only channels can't be cleared**: failures/platform_results
   merge with `operator.add`. Retry generations use an `attempt` field
   — only current-generation failures route (`after_harvest`).
3. **Idempotency keys**: `req_id = task_id#draft_version`. Retry
   re-Sends the SAME draft → same key → platform dedupes. Never bump
   the version on retry.
4. **Hard rules in code, not prompts**: reddit has no image tool on
   its menu (`tools_for`) and its link is stripped in
   `platform_publish`. Prompt lines are soft reinforcement only.
5. **Degrade, never crash**: after a successful ledger write, vector
   add / judge / trace-name failures must not fail the task (each has
   its own guard). Expected remote failures return typed errors.
6. **Supervisor has zero LLM**: routing functions are deterministic
   and unit-tested; do not put model calls in control flow.
7. **Trace identity**: `trace_id` is written into FlowState at task
   creation and read back from the checkpoint on resume
   (`resolve_trace_id`) — manual spans nest under their node via OTel
   context, so do NOT pass `trace_context` for in-node spans.

## Testing conventions

- Gateway/planner seams are monkeypatched (`composio.publish`,
  `tripplanner.plan_trip`, `flow.platform_draft`); no network in
  tests. `conftest.py` isolates every store and SEALS env overrides
  (empty-string `LANGFUSE_*`, `AGENT_JUDGE`) that beat dotenv — keep
  new secrets sealed there too.
- Patching `flow.platform_draft` (or any node) requires
  `flow.get_graph.cache_clear()` — the compiled graph binds node
  functions at build time.
- Vector tests run on a deterministic fake-embedding backend;
  `APH_VECTOR_DIR` is read at CALL time (lazy) so the tmp override
  works after import.
- The task-id collision guard refuses reruns of an existing id unless
  `--force` (idempotent republish); tests that rerun use fresh ids.

## Common tasks

- **Add a platform**: entry in `composio.PUBLISH_ACTIONS` + a params
  builder + `PLATFORM_RULES` row; skip semantics come free.
- **Run a model-swap experiment**: `DEEPSEEK_MODEL=x python3 -m
  agentposthub evalflow --label x`, then `--diff`.
- **Inspect a task end-to-end**: `show` (state) → `trace` (spans) →
  ledger rows in `aph.db` → Langfuse session by label.
