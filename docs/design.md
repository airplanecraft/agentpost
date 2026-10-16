# AgentPostHub — System Design

> This is the design of the system **as implemented**. It originated as
> a migration plan (port a hand-rolled orchestration runtime onto
> LangGraph without discarding domain data — strangler pattern), and
> the core of that plan is now built and running against production
> endpoints. Sections marked **Roadmap** are designed but not yet
> implemented. The original Chinese design note is preserved in
> project history.

---

## 0. Evolution

```
Phase 1 (DONE — this repo):  the gated publish graph
    grounding → [draft workers ×N] → ⏸approve → [publish workers ×N]
    → record → ⏸escalation → rule distillation
Phase 2 (Roadmap):           discover + strategy gate subgraph
Phase 3 (Roadmap):           scheduler as graph trigger (tick → invoke)
```

What was deliberately kept from the pre-LangGraph system: the sqlite
domain stores (ledger, pending reviews), the platform rule table, and
the approval surface semantics.

---

## 1. Graph topology

See [agent-flow.md](agent-flow.md) for diagrams. Summary:

```
START → retrieve_context
  → [Send: platform_draft ×N, parallel]     (supervisor decision ①)
  → ⏸ approve                               (human gate ①, all drafts)
  → [Send: platform_publish ×N, parallel]   (supervisor decision ②)
  → record_all → after_harvest              (supervisor decision ③)
      clean → END
      failures → ⏸ escalate                 (human gate ②, batch)
          retry → re-Send failed platforms only (idempotent)
          drop  → incidents → LLM distill → ⏸ rule gate (gate ③)
```

### Autonomous decisions (no manual selection)

| Decision | Mechanism | Status |
|---|---|---|
| Which platforms get draft workers | `route_drafts` over the platform list; budget guard routes over-cap tasks to the gate | ✅ |
| Per-platform content shape | hard rules structurally (reddit: no image tool on menu, no link) + model-led tool choice elsewhere | ✅ |
| Grounding degradation | breaker-gated A2A planner; outage → "estimated" marking | ✅ |
| Retry targeting | escalate re-Sends only failed platforms, same idempotency keys | ✅ |
| Which due tasks enter the graph | scheduler trigger | **Roadmap** |
| Pacing (when to actually post) | pacer with dual trigger (event + timer) | **Roadmap** |

### Graph state (channels)

```
FlowState:
  task_id / trace_id / platforms[] / brief
  grounding                    shared facts pack (A2A planner + policy + lessons)
  drafts[]         append     one per platform (Send merge)
  approval / retry_count / escalation_action / retry_platforms
  platform_results[] append   publish outcomes (audit channel)
  verifications[]  append     read-back results
  failures[]       append     structured failures, stamped with `attempt`
  budget_usd / spend_usd[]    per-task cost ledger (usage-priced per call)
```

Append reducers are what let parallel Send workers merge without
locks; because append channels cannot be cleared, retry generations
stamp failures with `attempt` and only the current generation routes.

---

## 2. State, memory, and the three loops

| Layer | Store | Contents | Access |
|---|---|---|---|
| Task state | sqlite checkpointer (SqliteSaver) | every superstep snapshot, parent-linked | `get_state(thread_id)`; resume across processes/days |
| Long-term KV | sqlite store (SqliteStore) | trust tiers, tool breakers, rule lifecycle + usage stamps | exact namespace+key |
| Vector memory | local Chroma + Qwen embeddings | published draft texts, rule texts | cosine top-k (semantic dedup) |
| Ledger | sqlite table | verified publishes, PK (task_id, platform) | idempotent INSERT OR REPLACE |

**Loop 1 — tool health**: `("tool", name)` circuit breaker. N
consecutive *availability* failures (rate-limit, http, transport) open
a circuit with cooldown; input-shape failures (e.g. `no_draft`) do not
count. Next tasks self-degrade without human touch.

**Loop 2 — account trust**: `("account", platform)` tier. A read-back
failure (phantom publish) downgrades to `building` and the next run
strips that platform's links. Full 4-tier ladder (new/building/
established/quarantined with karma gates and human-only restore) is
**Roadmap**; the implemented subset is established/building + one
demotion edge.

**Loop 3 — rule distillation**: incidents accumulate
(`("incident",)`); at threshold an LLM distills ONE proposed rule
(`RULE:`/`WHY:` JSON); proposals cannot self-activate — human approves
(CLI), approved rules are injected into future prompts as strict
lessons and indexed into vector memory; retirement is equally explicit
and data-driven (`applied_count`, `failures_while_applied`).

Writing safety: memory writes come only from deterministic events or
human decisions — **the model never writes its own memory**.

---

## 3. Tool contracts

| Contract | Implementation |
|---|---|
| Publish output | response must be `{id: str, ok: bool, req_id}` echoing our idempotency key (`task_id#draft_version`); violations → structured failures |
| Idempotency | retry re-Sends the SAME draft → same key → platform dedupes → nothing double-posts |
| Read-back (phantom-publish post-condition) | success confirmed against the platform, never assumed: history tier first (facebook page posts; telegram chat where readable), provider delivery-receipt tier where the platform forbids history (telegram channels), degradation reason recorded |
| A2A grounding | agent-card discovery; `message/send`; structured data part = plan; text-without-data = clarifying question (`no_draft`); typed errors map onto the breaker taxonomy |
| Hard rules in code | tool visibility gating (reddit never sees the image tool), link stripping (reddit, and trust-building everywhere) — prompt lines are soft reinforcement only |

---

## 4. Human gates and safety controls

Three durable interrupt gates (state survives process death; resume
replays the node from its top, so pre-interrupt code is idempotent):

1. **Approve** — one human decision covers all platform drafts
   (approve / reject / per-platform edits); optional LLM-judge advisory
   verdict rides the payload as signal, never routing.
2. **Escalate** — batch retry/drop for failed publishes; bounded at 2;
   budget burn forces a deterministic drop.
3. **Rule gate** — distilled rules pending until approved; retirement
   explicit.

Structural safety: dry-run default; kill switch (fail-closed pause of
all publish workers — a human pause is not an incident); per-task
budget breaker on both fan-outs; explicit recursion cap; public-community
publishing (reddit) credential-gated by default — auto-posting
marketing into public subreddits is a ban risk deliberately not
automated (visible `skipped` state until a human opts in).

---

## 5. Error handling matrix (as implemented)

| Stage | Failure | Detection / recovery |
|---|---|---|
| LLM call | 5xx/timeout | bounded retry with backoff |
| Planner (A2A) | rate-limit / outage / no-draft | breaker (availability class only) → "estimated" degradation |
| MCP publish | contract violation / gateway error | structured failure → batch escalation gate → targeted idempotent retry |
| Read-back | not visible / removal | trust downgrade (loop 2) + failure entry |
| Vector memory | backend down / store error | degrade to stub similarity, never fail the task |
| Judge | any failure | returns None; evals/gate simply omit it |
| Cost | budget burn | fan-outs refuse to spread; gate carries spend; retries over cap force drop |
| Runaway graph | loops | explicit `recursion_limit` (24; worst legal path ~15) |
| Panic | operator action | kill switch: fail-closed, paused branches wait at the gate |
| Process death | crash/OOM | same thread_id re-invoked from last checkpoint; gates unaffected |

---

## 6. Tracing, evaluation, measured results

**Tracing** — self-hosted Langfuse (see
[langfuse-self-host.md](langfuse-self-host.md)): node spans via
callback handler, raw LLM calls as typed generations with token usage,
MCP calls as typed tools; manual spans nest under their node via OTel
context. The trace id is derived once at task creation and persisted
inside FlowState, so both invoke halves (and every worker branch)
aggregate into one tree; human-gate latency is directly measurable as
the span gap.

**Evaluation** — two levels:

*Product* (`eval`): deterministic checks — number consistency against
the grounding source's authoritative total, length, real-copy — plus
an opt-in LLM judge (narrow rubric; scores, never routing).

*Flow* (`evalflow`): four fixed scenarios (normal multi-platform /
injected contract drift / planner outage / safety invariants) run the
whole graph with mocked gateway+planner and a REAL model worker;
three metric layers (system / orchestration / model-behaviour);
labelled JSON reports; `--diff` for model-swap experiments; Langfuse
sessions group runs per label. First swap findings are recorded in
[orchestration.md](orchestration.md).

**Measured** (personal scale, production endpoints): 10+ live verified
publishes with receipts matching the ledger; three real
external-interface failures caught by contracts and distilled into
rules; human-gate and per-node latencies in traces; benchmark green
across scenarios including hit-and-idempotent-retry on injected drift.

---

## 7. Platform integration

**MCP gateway (agent→tools)** — Composio is the single exit for all
platform actions. The Composio MCP server exposes only COMPOSIO_*
meta-tools; platform actions must route through
`COMPOSIO_MULTI_EXECUTE_TOOL` — calling tool names directly
*silently fails while returning success* (the phantom-publish trap).
Defence in depth: forced routing in the gateway wrapper + read-back
after every irreversible call + contract tests on that path.
Adding a platform = one action mapping + params builder (+ optional
`PLATFORM_RULES` row); unconfigured platforms skip visibly (~1ms).

**A2A (agent→agent)** — the grounding source is a discoverable agent:
`/.well-known/agent-card.json` for capability discovery, `message/send`
JSON-RPC with a structured data part. The site's agent loop and rate
limiter are untouched behind the new protocol front.

---

## 8. Roadmap (designed, not yet built)

- **Scheduler + discover + strategy gate** (Phase 2/3): due-task
  triggers, target selection, candidate filtering.
- **Pacer with dual trigger** — platform-global pacing decoupled from
  the graph: event trigger (approval → immediate judgement) + timer
  trigger (delayed items via a `scheduled` table), single
  `pacer.decide()` entry, data-driven per-platform quotas
  (max/day, min-interval, quiet hours) reading the verified ledger as
  the single truth for quota counting.
- **Three-tier rules engine** — platform (ban-level) / sub-community
  (removal-level) / self-imposed, in a rules table with enforcement
  points (discover_filter / pacer / validator / post-check) and
  semi-automatic sub-rule change detection behind a human approval.
- **Full trust ladder** — new/building/established/quarantined with
  karma gates, slow automatic promotion, fast automatic demotion,
  human-only restoration.
- **Full RAG stack** — structure-aware chunking, hybrid retrieval,
  retrieval evals; the current system is deliberately "grounded
  generation" (structured A2A facts + semantic dedup corpus), not a
  retrieval product.
- **Kafka surfaces** — gates are message-shaped today (CLI resume);
  queue consumers and `posts.ready`/`posts.published` topics are the
  production transport plan.
- **Model fallback ladder** — chat-family primary → cross-vendor
  OpenAI-compatible backup → reasoner last resort; benchmark data
  already quantifies the degradation of each hop.
