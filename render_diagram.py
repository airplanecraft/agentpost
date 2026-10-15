"""Render the AgentPostHub workflow diagram to PNG (email attachment)."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

fig, ax = plt.subplots(figsize=(11, 14), dpi=150)
ax.set_xlim(0, 10); ax.set_ylim(0, 14); ax.axis("off")

C_NODE   = "#dae8fc"; E_NODE   = "#6c8ebf"   # agent nodes: blue
C_HUMAN  = "#ffe6cc"; E_HUMAN  = "#d79b00"   # human gates: orange
C_STORE  = "#d5e8d4"; E_STORE  = "#82b366"   # storage: green
C_RULE   = "#fff2cc"; E_RULE   = "#d6b656"   # rules/eval: yellow
C_FAIL   = "#f8cecc"; E_FAIL   = "#b85450"   # failure: red
C_OBS    = "#f5f5f5"; E_OBS    = "#666666"   # observability: gray

def box(x, y, w, h, text, fc=C_NODE, ec=C_NODE, fs=8.5, lw=1.4, dashed=False, weight="normal"):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.12",
                 fc=fc, ec=ec, lw=lw, linestyle="--" if dashed else "-"))
    ax.text(x+w/2, y+h/2, text, ha="center", va="center", fontsize=fs,
            fontweight=weight, linespacing=1.4)
    return (x, y, w, h)

def arrow(p1, p2, label="", color="#333", dashed=False, lw=1.4, lx=0, ly=0, fs=7.5):
    a = FancyArrowPatch((p1[0], p1[1]), (p2[0], p2[1]),
                        arrowstyle="-|>", mutation_scale=13, color=color,
                        lw=lw, linestyle="--" if dashed else "-",
                        connectionstyle="arc3,rad=0")
    ax.add_patch(a)
    if label:
        mx, my = (p1[0]+p2[0])/2+lx, (p1[1]+p2[1])/2+ly
        ax.text(mx, my, label, fontsize=fs, ha="center", color=color, fontweight="bold")

def right(b):  return (b[0]+b[2], b[1]+b[3]/2)
def left(b):   return (b[0], b[1]+b[3]/2)
def top(b):    return (b[0]+b[2]/2, b[1]+b[3])
def bottom(b): return (b[0]+b[2]/2, b[1])

ax.text(5, 13.7, "AgentPostHub — Agent Workflow (LangGraph port)",
        ha="center", fontsize=14, fontweight="bold", color="#1a1a1a")
ax.text(5, 13.35, "6 autonomous decisions · 2 human gates · 3-tier rules · verification contract",
        ha="center", fontsize=9, color="#555")

# main chain (center column)
sched  = box(3.9, 12.3, 2.2, 0.7, "Scheduler (tick)\npicks due tasks — no manual selection", fs=8)
disc   = box(3.9, 11.1, 2.2, 0.8, "Discover / select_target\nrule-filtered targets\n[D1]", fs=8)
gen    = box(3.9, 9.9, 2.2, 0.8, "Generate (model-led)\nfact-grounded + tool choice\n[D3]", fs=8)
dedup  = box(3.9, 8.8, 2.2, 0.7, "Dedup Guard\nsemantic similarity [D5]\nreject → variant regen ≤2", fs=8)
appr   = box(3.9, 7.6, 2.2, 0.8, "■ APPROVAL QUEUE\nHUMAN GATE ①\npropose / execute separation", fc=C_HUMAN, ec=E_HUMAN, fs=8, weight="bold")
pol    = box(3.9, 6.4, 2.2, 0.8, "Policy Validator\n3-tier rules, 19 live\n[D2] link privilege by trust", fs=8)
pub    = box(3.9, 5.2, 2.2, 0.8, "Publish Agent\nMCP / Composio tool calls\nidempotency key [D4 pacer]", fs=8)
ver    = box(3.9, 4.0, 2.2, 0.8, "Post-Exec Verification\nread-back platform state\n[D6] confirm ≠ assume", fc=C_STORE, ec=E_STORE, fs=8)
rec    = box(3.9, 2.8, 2.2, 0.8, "Record\nidempotent ledger write\n+ index to memory", fc=C_STORE, ec=E_STORE, fs=8)

# right column: rules and pacer
rules  = box(7.2, 10.6, 2.4, 1.0, "3-Tier Rules Engine\nplatform / subreddit / self-imposed\n19 rules · 4 checkpoints", fc=C_RULE, ec=E_RULE, dashed=True, fs=8)
pacer  = box(7.2, 6.4, 2.4, 0.9, "Release Pacer [D4]\n2/day · 3h interval\nplatform-global, outside graphs", fc=C_RULE, ec=E_RULE, dashed=True, fs=8)

# left column: memory and recovery
store  = box(0.4, 7.6, 2.4, 0.9, "Long-term Memory (Store)\ntrust tiers · incident ledger\n4-level ladder, auto cooldown", fc=C_STORE, ec=E_STORE, dashed=True, fs=8)
failsm = box(0.4, 4.6, 2.4, 0.9, "Failure State Machine\nbounded retry · cooldown\n1/6/24/72h liveness probes", fc=C_FAIL, ec=E_FAIL, fs=8)
esc    = box(0.4, 3.0, 2.4, 0.8, "■ ESCALATION\nHUMAN GATE ②\noutcome → new rule", fc=C_HUMAN, ec=E_HUMAN, fs=8, weight="bold")

# bottom: observability band
obs    = box(0.4, 1.4, 9.2, 0.8, "Observability — append-only ledger: task → tool call → verification → probe timeline · 15,800 runs / 1 error · 206 publishes · 14 removals caught",
             fc=C_OBS, ec=E_OBS, dashed=True, fs=8)

# main-chain arrows
arrow(bottom(sched), top(disc))
arrow(bottom(disc), top(gen), "target + brief")
arrow(bottom(gen), top(dedup), "draft")
arrow(bottom(dedup), top(appr), "unique draft")
arrow(bottom(appr), top(pol), "HUMAN approval", color=E_HUMAN, lw=2)
arrow(bottom(pol), top(pub), "validated")
arrow(bottom(pub), top(ver), "tool executed")
arrow(bottom(ver), top(rec), "confirmed")

# rule edges (dashed)
arrow(left(rules), right(disc), "filters", dashed=True, color=E_RULE, ly=0.25)
arrow(left(rules), (6.1, 6.8), "tiers", dashed=True, color=E_RULE, lx=0.1)
arrow(left(pacer), right(pub), "pacing gate", dashed=True, color=E_RULE)

# memory and recovery edges
arrow(right(store), left(appr), "trust tier read", dashed=True, color=E_STORE)
arrow(right(store), left(pol), "tier → strip/allow", dashed=True, color=E_STORE)
arrow((3.9, 4.4), right(failsm), "mismatch", color=E_FAIL, lx=-0.2)
arrow(top(failsm), (3.9, 5.4), "cooldown + retry", color=E_FAIL)
arrow(bottom(failsm), top(esc), "sustained failure", color=E_HUMAN)
arrow(top(esc), (8.4, 10.6), "policy update", dashed=True, color=E_HUMAN)

# dedup bounce-back loop
curve = FancyArrowPatch((3.9, 9.15), (3.9, 9.9), arrowstyle="-|>", mutation_scale=13,
                        color="#333", lw=1.3, connectionstyle="arc3,rad=-1.2")
ax.add_patch(curve)
ax.text(2.9, 9.5, "regen ≤2", fontsize=7.5, ha="center", color="#333", fontweight="bold")

plt.tight_layout()
plt.savefig("a2b2_agentposthub_workflow.png", dpi=150, bbox_inches="tight",
            facecolor="white")
print("done: a2b2_agentposthub_workflow.png")
