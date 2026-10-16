# Architecture

AgentPostHub is a multi-agent content distribution system: a
deterministic supervisor orchestrates model-led platform workers over
durable checkpoints, publishing through an MCP gateway and grounding
from an A2A agent, with three memory loops and layered evaluation.

## Component map

```
                        ┌──────────────────────────────────────────┐
                        │              SUPERVISOR (policy)         │
                        │  route_drafts · after_approve ·          │
                        │  after_harvest · escalate targeting      │
                        │  (deterministic code + human gates —     │
                        │   zero LLM in control flow)              │
                        └───────┬──────────────────────┬───────────┘
                                │ Send fan-out         │ Send fan-out
              ┌─────────────────▼───────┐   ┌──────────▼────────────┐
              │   DRAFT WORKERS ×N      │   │  PUBLISH WORKERS ×N   │
              │   model-led: prompt +   │   │  hard-rule strip →    │
              │   tool menu (soft rule  │   │  MCP publish →        │
              │   on top of hard rules) │   │  contract check →     │
              └─────────┬───────────────┘   │  read-back verify     │
                        │                   └──────────┬────────────┘
              ┌─────────▼──────────────────────────────▼────────────┐
              │ grounding (A2A agent) · tools (Composio MCP)        │
              │ LLM (DeepSeek) · judge · vector memory (Chroma)     │
              └────────────────────────────────────────────────────┘
```

### Why the supervisor is not an LLM

Routing decisions here are safety decisions (retry vs drop, escalate
vs end, which platforms to fan out). Those must be testable,
auditable, and deterministic — the LLM's autonomy is confined to the
reversible domain (content generation). *"Judgment lives in the
workers; control lives in the code."*

## Data stores (four, each with one job)

| Store | Engine | Holds | Access pattern |
|---|---|---|---|
| Checkpoints | SQLite (LangGraph `SqliteSaver`) | per-task state, one row per superstep, parent-linked chain | `get_state(thread_id)` / history replay |
| Long-term memory | SQLite (LangGraph `SqliteStore`) | trust tiers, tool-health breakers, rule lifecycle + usage counters | exact namespace+key |
| Vector memory | Chroma (local, persistent) | published draft texts, rule texts — the things recalled BY MEANING | cosine top-k (Qwen embeddings) |
| Ledger | SQLite table | verified publishes, PK (task_id, platform) | INSERT OR REPLACE (idempotent) |

Rule of thumb: **if it will be looked up by meaning later, it goes
into vectors; if by identity, into the Store; if it is process
history, into checkpoints; if it is a business fact, into the ledger.**

## Memory: three loops

1. **Tool health** — `("tool", name)` breaker: N consecutive
   availability failures open a circuit with cooldown; the next task
   self-degrades (e.g. "estimated" grounding) without human touch.
2. **Account trust** — `("account", platform)` tier: a read-back
   failure (phantom publish) downgrades the tier; the next run strips
   that platform's links automatically.
3. **Rule distillation** — failed tasks record incidents
   `("incident",)`; at threshold, an LLM distills ONE proposed rule
   (JSON `RULE:`/`WHY:`); proposals **cannot self-activate** — a human
   approves via CLI, and only approved rules are injected into future
   prompts as `Operational lessons (obey strictly)`. Rules carry
   `applied_count` / `failures_while_applied` stamps so retirement is
   data-driven; retirement is as explicit as approval.

## Tool contracts

- **Publish output contract**: the platform response must be
  `{id: str, ok: bool, req_id}` and echo our idempotency key
  (`task_id#draft_version`). Violations become structured failures —
  never silent success.
- **Idempotency**: retry re-publishes the SAME draft → same key → the
  platform deduplicates → nothing double-posts.
- **Read-back (phantom-publish contract)**: success is confirmed
  against the platform, never assumed from the tool response.
  Two-tier: page/chat history first; where the platform forbids
  history reads (Telegram channels), fall back to the provider's
  delivery receipt, with the degradation reason recorded.
- **A2A grounding contract**: reply-without-data-part = clarifying
  question (`no_draft`), typed error codes map onto the breaker
  taxonomy; `rate_limited` and input-shape problems are distinct
  failure classes.

## Error handling & failure recovery

| Layer | Mechanism |
|---|---|
| Call | bounded retry with backoff (LLM, MCP, planner) |
| Dependency | circuit breaker per external service (cross-session) |
| Publish | output contract → structured failure → **escalation gate** |
| Escalation | human retry/drop; retry re-Sends only failed platforms, idempotent |
| Degradation | planner outage → "estimated" marking; vector memory absent → stub similarity; judge failure → None |
| Cost | per-task budget breaker: over-cap tasks route to the gate; retries over cap force a deterministic drop |
| Panic | kill switch (`pause`): fail-closed stop of all publish workers; a pause is not an incident |
| Runaway | explicit `recursion_limit` (24; worst legal path ~15 supersteps) |

## Safety controls

- Hard rules in code, never prompt-only: reddit never sees the image
  tool (visibility gating) and never carries a link (stripped
  in-code).
- Public-community gating: reddit has no gateway action configured by
  default — auto-posting marketing into public subreddits is a ban
  risk deliberately not automated; the platform stays in a visible
  `skipped` state until a human opts in.
- Dry-run is the default publish mode; live requires explicit config.
- All irreversible actions sit behind interrupt gates whose state
  survives process death.
