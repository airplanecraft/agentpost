# Agent Flow

One task = one thread = one trace. The graph suspends twice at human
gates and resumes in a different process.

## Full flow

```mermaid
flowchart TD
    S([brief: run --platform telegram,facebook,reddit]) --> RC[retrieve_context<br/>A2A grounding + breaker check<br/>+ lessons recall]
    RC --> RD{{route_drafts<br/>SUPERVISOR ①<br/>budget guard}}
    RD -->|Send ×N, parallel| DW1[platform_draft · telegram<br/>text+image+link]
    RD -->|Send ×N| DW2[platform_draft · facebook<br/>text+image+link]
    RD -->|Send ×N| DW3[platform_draft · reddit<br/>text-only · no link · no image tool]
    DW1 & DW2 & DW3 --> APP{{⏸ APPROVE gate<br/>one human decision<br/>covers ALL drafts}}
    APP -->|rejected| E1([END])
    APP -->|approved| AA{{after_approve<br/>SUPERVISOR ②}}
    AA -->|Send ×N, parallel| PW1[platform_publish · telegram<br/>strip → MCP → contract<br/>→ read-back → trust]
    AA -->|Send| PW2[platform_publish · facebook<br/>page post + history verify]
    AA -->|Send| PW3[platform_publish · reddit<br/>SKIPPED ~1ms<br/>gateway-gated]
    PW1 & PW2 & PW3 --> REC[record_all<br/>ledger + vector archive]
    REC --> AH{{after_harvest<br/>SUPERVISOR ③<br/>current-attempt failures?}}
    AH -->|clean| E2([END])
    AH -->|failures| ESC{{⏸ ESCALATION gate<br/>batch retry / drop}}
    ESC -->|retry ≤2<br/>failed platforms only,<br/>same idempotency keys| PW1
    ESC -->|drop| DIST[record incidents<br/>LLM distills rule proposal<br/>⏸ rule gate] --> E3([END])
```

## The two invoke halves (durable human gate)

`interrupt()` is node-level suspension, not a blocked thread: the run
process exits, state lives in the checkpoint, and the approve process
replays the `approve` node from its top.

```mermaid
sequenceDiagram
    participant R as run process
    participant C as checkpointer (sqlite)
    participant H as human (minutes…days)
    participant A as approve process
    R->>R: retrieve → drafts ×3 (parallel)
    R->>R: approve node: INSERT pending_reviews
    R->>C: interrupt() → GraphInterrupt
    R-->>R: invoke returns, process exits
    Note over C: "next task = approve, waiting"
    H->>A: python -m agentposthub approve <id>
    A->>C: Command(resume=approved)
    A->>A: approve node REPLAYS from top<br/>(INSERT OR REPLACE = replay-safe)
    A->>A: publish workers ×3 → verify → record
```

The approve node appears TWICE in the trace — one span per execution:
the suspension pass and the resume replay. The wall-clock gap between
them is the measurable human-gate latency.

## Send fan-out and the barrier

`route_drafts` returns a list of `Send(node, state_slice)` — the loop
only *declares* tasks; the engine executes them in the same superstep
(parallel), and the next node (`approve` / `record_all`) starts only
after ALL of them finish (superstep barrier). Total wall time is
`max(worker)` — the straggler sets the join time.

## Trace anatomy (true call order)

```bash
python -m agentposthub trace <task_id>
```

```
LangGraph                        ← invoke #1 root
 ├─ retrieve_context
 │    └─ site_planner        🔧 TOOL   (A2A grounding; failures visible)
 ├─ platform_draft ×3        (parallel, same-millisecond start)
 │    └─ deepseek_chat       🤖 GENERATION (model, tokens, tool_calls)
 ├─ dedup / after_dedup
 └─ approve ⏸                 ← graph suspends
        ╳ human-gate latency (directly measurable)
 LangGraph                        ← invoke #2 root (resume process)
 ├─ approve (replay) → after_approve   SUPERVISOR ②
 ├─ platform_publish ×3
 │    ├─ telegram   → composio_publish 🔧 → composio_read_back 🔧
 │    ├─ facebook   → composio_publish 🔧 (page-history verify)
 │    └─ reddit     → skipped ~1ms (gateway-gated, visible reason)
 └─ record_all → after_harvest         SUPERVISOR ③ → END
```

Span nesting is real OTel context propagation: manual spans adopt
their node (a span created inside `generate` becomes its child).
