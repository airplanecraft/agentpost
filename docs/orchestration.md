# Orchestration

How control flows: supervisor decisions, idempotency, retries, gates,
and the identity keys that tie state, observability, and the platform
side together.

## Supervisor vs workers

| | Supervisor | Worker |
|---|---|---|
| Output | control flow (node names, `Send` lists, `END`) | business data (drafts, publish results) |
| LLM | none — policy code + human gates | draft workers are model-led |
| Instances | evaluated once per fan-out | one per platform via `Send` |
| Failure semantics | orchestration bug — covered by tests | business failure — routed to the gate, retryable |
| Litmus test | remove it → the graph doesn't know where to go | remove it → that platform misses a post, others unaffected |

Supervisor decisions in the graph (all span-audited):

1. **`route_drafts`** — which platforms get draft workers; budget
   guard routes over-cap tasks straight to the gate.
2. **`after_approve`** — translates the human decision into the second
   fan-out (or END); budget guard again.
3. **`after_harvest`** — clean sweep ends; failures (of the CURRENT
   retry generation only) go to the escalation gate.
4. **`escalate` targeting** — retry re-Sends only the failed
   platforms, with their original drafts.

## Interrupt semantics (what "waiting" really is)

- `interrupt()` raises `GraphInterrupt`; the engine checkpoints
  ("next task = approve, waiting for human") and `invoke()` returns.
  **Nothing waits** — the run process exits.
- A resume (`Command(resume=payload)`) replays the interrupted node
  FROM THE TOP; `interrupt()` then returns the payload. Node code
  before an interrupt must therefore be idempotent (`INSERT OR
  REPLACE`, deletes on both paths).
- One task = one thread; both invoke halves and every worker branch
  share it. Workers never see the thread id — the engine attributes
  their results to the invoking thread's channels (append reducers
  merge without locks).

## Identity keys

| Key | Scope | Purpose |
|---|---|---|
| `task_id` (= thread_id) | whole task lifecycle | checkpoint routing; the one key humans must persist |
| `trace_id` = sha1(task_id), stored in FlowState | whole task's observability tree | spans from both processes aggregate into one trace; persisted in state so resume reads it instead of re-deriving |
| `req_id` = task_id#draft_version | one platform publish | idempotency key — retries reuse it, the platform dedupes |
| platform message/post id | one published item | read-back verification target |

## Retry & escalation

- Call-level: bounded retry with backoff (LLM 5xx, MCP, planner).
- Publish-level: a failed branch records a structured failure (with
  its `attempt` generation — append-only audit channels can't be
  cleared, so only current-generation failures route) and the batch
  gate suspends once for all failed platforms.
- `retry`: re-Send failed platforms only; same drafts, same `req_id` —
  **nothing double-posts**. Bounded at 2; budget burn forces a drop.
- `drop`: incidents are recorded and (at threshold) an LLM distills a
  rule proposal — pending until a human approves. A kill-switch pause
  is excluded from incidents (a human pause is not a system fault).

## Platform rule matrix

| | telegram | facebook | reddit |
|---|---|---|---|
| image tool offered | ✓ (model decides) | ✓ | **✗ structurally absent** |
| link policy | allowed | allowed | **stripped in code** |
| gateway action | TELEGRAM_SEND_MESSAGE | FACEBOOK_CREATE_POST | **none — skipped ~1ms** |
| read-back | history → delivery receipt (channels) | page history (true verify) | — |
| rationale | own channel | own page | public community: auto-posting is a ban risk, gated until a human opts in |

## Model-swap experiments

`DEEPSEEK_MODEL` selects the model; `evalflow --label <name>` records
a labelled baseline; `--diff A B` compares. Findings from the first
swap (chat → reasoner): tool_call_rate 0.667→0 (no function calling),
placeholder_rate 0→1.0 (a real defect in the chase heuristic,
generalized and re-measured to 0.0), number-consistency drift
documented as an adapt-or-accept decision — while system,
orchestration, and safety layers held at 1.0 across the board.
