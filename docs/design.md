# AgentPostHub — LangGraph/LangChain 重构设计（Refactor Design）

> 目标：将 AgentPostHub 的自研编排运行时移植到 LangGraph，保留既有领域数据与规则，
> 使系统成为"真·LangGraph 应用"。分阶段绞杀式迁移，Phase 1 即可作为 A2B2 技术对话的实证。
> 本文档只写设计，不含代码。

---

## 0. 迁移总原则（Strangler Pattern）

```
不推倒重来。SQLite 领域数据/策略规则/审批控制台全部保留，
只把"编排内核"从自研状态机换成 LangGraph StateGraph。

Phase 1（今天）: 审批门控的发布子图 —— gen → approve → publish → verify 全链上 LangGraph
Phase 2       : discover + strategy 门控子图
Phase 3       : scheduler 改为 graph 触发器（tick → graph.invoke）
```

---

## 1. 图拓扑（对应 note 第 1 节：工作流与自主决策）

### 主图 `publish_flow`

```
                    ┌─────────────────────────────────────────┐
                    │  StateGraph(publish_flow)               │
                    │                                         │
 entry(scheduler)   ▼                                         │
 ──► [discover] ──► [strategy_gate] ──► [generate] ──► ⏸[approve]
      │                │    (条件边: 规则不过 → reschedule)      │
      │                │                        │              │
      │ (发现失败→降级)  │              (条件边: 人工决策)          │
      ▼                ▼                        ▼              ▼
  [cache_targets]  [reschedule]        rejected: [archive]   approved:
                                                            [publish]（Send 并行扇出
                                                             到 5 个平台子图）
                                                                  │
                                                                  ▼
                                                            [verify]
                                                           /        \
                                                    confirmed     mismatch(有界重试)
                                                          │            │
                                                          ▼            ▼
                                                      [record]     [escalate]⏸
                                                          │            │
                                                          ▼            ▼
                                                     (END)       policy_update → 回写 strategy_gate
```

### 自主决策点（无人工挑选）全部落在**条件边**上

| 决策 | LangGraph 机制 |
|---|---|
| 哪些到期任务进入图 | scheduler 触发 `graph.invoke(thread_id=task_id)`，无人工 |
| 候选目标取舍 | `discover` 节点产出 → `strategy_gate` 条件边按规则分流 |
| 账号是否配外链 | `strategy_gate` 条件边（信任分级读取自 long-term store） |
| 发什么/何时发 | `generate` 节点模型主导 + `pacing` 条件边（时间状态+随机抖动，不到点 → reschedule 循环） |
| 每平台并行发布 | **`Send` API**：publish 节点按 targets 扇出，每平台一个子图实例，互不阻塞 |

### 图状态（TypedDict + Pydantic 双层）

```
GraphState:
  task / route_brief          任务与路线上下文
  targets[]                   候选平台目标
  draft / draft_version       生成稿与版本
  approval: pending|approved|rejected|edited   审批状态
  platform_results[]          Send 扇出回收的结果（含 tool_call_id、延迟、成本）
  verification[]              回读验证结果
  failures[]                  结构化失败记录（节点、原因、类别）
  budget_snapshot             预算余量（进 publish 前快照）
```

---

## 2. 状态、记忆、工具契约、故障恢复（note 第 2 节）

### 2.1 三层记忆

| 层 | 内容 | LangGraph 机制 |
|---|---|---|
| 图内短期状态 | GraphState（一次任务全生命周期） | StateGraph 状态通道 |
| 跨任务持久 | 审批中断点、长任务恢复点 | **Checkpointer: SqliteSaver**（thread_id=task_id；进程崩溃 → 重新 invoke 同 thread_id 从最后检查点续跑 —— 替代自研 checkpoint 表） |
| 长期记忆 | 账号信任分级、campaign 上下文、事故沉淀的策略 | **LangGraph Store**（跨 thread 读写），与 SQLite 领域表双向同步（SQLite 仍是事实源，Store 是图侧缓存/投影） |

### 2.2 工具契约（LangChain 严格化）

| 契约环节 | 实现 |
|---|---|
| 输入 | 每个 MCP 工具包一层 `@tool(args_schema=PydanticModel)` —— 结构化入参在派发前被 Pydantic 拒绝 |
| MCP 接入 | `langchain-mcp-adapters` 把 Composio 工具转成 LangChain Tool |
| 输出 | ToolMessage 强制 schema 校验；**不可逆工具（发布类）附带 read-back 二次确认调用** —— 幽灵发布契约保留并升级为工具层的 post-condition |
| 幂等 | 每个工具调用携带 `client_req_id`（task_id+platform+attempt），平台侧重复执行可检测 |

### 2.3 故障恢复矩阵（每个环节的 error handling —— 见 §5 全表）

核心迁移：自研失败状态机 → **节点级重试策略 + 条件错误边 + Checkpointer 断点续跑** 三件套。

---

## 3. 人工在环与安全控制（note 第 3 节）

### 3.1 双人工门（LangGraph 原生化）

```
门① 审批队列:
    approve 节点调用 interrupt() → 图挂起，状态持久在 checkpointer
    审批控制台（保留现有 web 页）改为 resume 端点:
      approved → Command(resume={"decision":"approved"})
      rejected → Command(resume={"decision":"rejected"})
      edited   → Command(resume={"decision":"edited", draft:...}) → 回 generate 重生
    ★ propose/execute 分离由 interrupt 机制结构性保证 —— 不可绕过

门② 升级处理:
    escalate 节点 interrupt(kind="escalation")
    人工决策结果经 Command 回填,并作为 policy_update 写回策略规则
```

### 3.2 代码级策略执行（不依赖提示词）

```
[policy_validator] 前置节点（publish 之前,Send 扇出之前最后一道）:
  - 养号期 → 剥离全部出站链接（角色拒绝规则,硬代码）
  - 节奏检查 → 未到 pacing 窗口 → 打回 reschedule
  - 预算检查 → budget_snapshot 不足 → interrupt(escalation) 或终止
  - 平台红线词表 → 拦截
任何模型输出过不了这四个断言,绝不进入工具调用。
```

---

## 4. 追踪、评估、运营结果（note 第 4 节）

| 能力 | 重构后实现 |
|---|---|
| 全链 trace | **LangGraph 回调 → Langfuse**（自托管）：每个节点/每次工具调用一个 span，任务→工具→验证→产物端到端串联 |
| 成本/延迟 | callback 事件流提取 per-call token/latency → 每日 rollup（替代自研 usage events） |
| 变更门控 | **LangSmith 风格 eval 套件**：golden set（历史已审内容）+ 确定性断言（链接规则/词表/格式）+ LLM-as-judge（品牌契合度）；prompt/模型版本变更 → 跑评估 → 不过 = 不发布（接线到图版本的配置库） |
| 版本化 | 图结构变更走 git；prompt 变更走版本库；两者都可回滚 |

---

## 5. 分环节 Error Handling 全表（note 第 2/3 节的实证核心）

| 环节 | 失败模式 | 检测 | LangGraph 恢复机制 |
|---|---|---|---|
| scheduler 触发 | DB 锁/空任务 | tick 返回空 | 静默跳过，日志留痕 |
| discover 节点 | 平台 API 429/超时 | 工具异常捕获 | 节点内 tenacity 退避重试（≤3）→ 降级到缓存候选 → 标记 target stale 下轮再试 |
| strategy_gate | 规则求值异常 | 断言失败 | **fail-safe 默认拒绝**（宁可不发）→ 条件边进 reschedule |
| generate 节点 | LLM 超时/输出不合 schema | 超时+结构校验 | 降上下文重试 → 备用模型（模型路由 fallback 链）→ 仍败 → interrupt(escalation) |
| interrupt 挂起 | resume payload 非法 | Pydantic 校验 | 保持挂起 + 告警；不产生副作用 |
| publish 子图（Send 并行） | 单平台失败/静默成功 | 工具异常 / **verify 回读不一致（幽灵发布）** | 平台级隔离（一个 Send 失败不污染其他）；错误边 → 有界重试（≤2,冷却）→ 超界 → escalate⏸ |
| verify 节点 | 回读状态≠预期 | post-condition 断言 | 触发有界重试；重试超界 → 升级人工 + 该平台冷却标记 |
| record 节点 | DB 写失败 | 异常 | Checkpointer 保证 at-least-once 到达；**按 task_id+platform 幂等 upsert**（重复执行无害） |
| 全图崩溃 | 进程死/OOM | 外部 liveness 探针（保留 1h/6h/24h/72h） | 同 thread_id 重新 invoke → 从最后检查点续跑，不重做已完成节点 |
| 预算熔断 | 单位时间成本超限 | budget_snapshot 对账 | policy_validator 直接拦截 → escalate⏸ |

---

## 6. RAG 子系统（回答"要不要设计 RAG"）

**要，但克制 —— 定位是"知识接地的生成"，不是独立产品。**

```
[retriever] 节点（generate 之前接入）:
  语料: 路线事实/品牌口径/历史高表现内容（自家的,量小: 千级 chunk）
  切分: 结构感知（按路线/段落）, 512 token, 10% overlap
  嵌入: BGE-M3（中文主语料）
  存储: pgvector
  检索: hybrid（向量 + BM25）, metadata 过滤（campaign/route）
  注入: top-3 → generate 的 grounding 上下文
  评估: 检索层 recall@5 + 生成层 faithfulness（答案论断必须有 chunk 支撑）
增量: content_hash 差量重嵌（沿用自研管道设计）
```

存在理由：生成内容的事实一致性（预算数字/天数/口径）从"靠提示词自觉"升级为"检索接地 + eval 校验"。规模小、闭环完整 —— **在 note 里它是 grounding 组件，不喧宾夺主**。

---

## 7. Ownership 表述（note 第 5 节重构后版本）

> 原系统为自研运行时（2025 年初起生产运行）。**在收到贵司邮件后，我将编排内核重构于 LangGraph**：StateGraph 图拓扑、SqliteSaver 检查点、interrupt 式审批门、Send 并行发布、Langfuse 全链追踪、LangSmith 式评估门控 —— 领域数据与策略规则保持自持。设计的每一处取舍（哪里用框架、哪里保留自有实现）都由我做出并可逐条讲解。

—— 每个字将在移植完成后为真。

---

## 8.5 节奏层与双触发设计（2026-10-14 增补：pacer / 双图拆分 / 平台通用）

### 8.5.1 图拆分 —— 审批图与发布图分离

```
━━━ approval_graph(每目标一条 thread) ━━━
  read_thread → retrieve → generate → dedup_guard
  → ⏸approve(interrupt) → policy_validator
  → [approved] 写 posts.status='approved'
  → 发 Kafka topic=posts.ready {task_id, platform}
  → END(账本干净收工)

━━━ pacer 节奏层(平台级全局视角,见 8.5.2) ━━━

━━━ publish_graph(thread_id = task_id#pub) ━━━
  policy_validator'(再验一次——排队期间规则可能已变)
  → publish(MCP 工具调用) → verify(回读)
  → verify_ext(延迟复查,定时再入 —— automod 删除检测)
  → record(★配额计数在此刻+1,唯一真相源)
  → 发 Kafka topic=posts.published {task_id, comment_id, verified}
```

### 8.5.2 pacer：双触发 + 单一判决入口

```
入口A【事件触发】审批动作本身 = 第一扳机
  approve 点击 → posts.ready 消息 → pacer 立即消费判决
  → 窗口开 → 立即 invoke publish_graph(秒级,不等任何人)

入口B【定时器触发】延时件的闹钟
  判决"现在不行" → 写 scheduled{not_before}
  → 定时器(1~5min tick)扫 WHERE not_before<=now
  → 重判(规则可能变) → 通过 → invoke publish_graph

★单一入口原则: 消息/定时器/人工"立即发"按钮 → 全部走 pacer.decide(task_id)
  同一函数,同一规则,任何路径不可绕过。
```

### 8.5.3 规则与配额（数据驱动,改规则不改代码）

```sql
-- strategy_rules(每平台一行)
platform | max_per_day | min_interval_min | quiet_hours    | 特有规则
reddit   | 2           | 180               | 00:00-07:00    | 养号期禁外链(role deny)
facebook | 2           | 480               | -              | 外链降权→链接放首条评论
telegram | 2           | 480(8-12h摊开)    | -              | bot 频道推送,无发现环节

-- 判决SQL(真相源 = publish_history,只数 verified=1;失败/被删不占配额)
今日已发 = COUNT(*) WHERE platform=? AND date=today AND verified=1
上次发布 = MAX(published_at) WHERE platform=?
判决: 超日配额→not_before=明天+随机抖动
     间隔不足→not_before=上次+min_interval
     否则    →立即发布
```

竞态防护: 每平台串行化(单 pacer 实例 / DB lease;SQLite 单写者天然串行)。

### 8.5.4 平台兼容(Facebook task / Telegram bot 通用)

三平台共用同一对图 + pacer,差异全部落在**策略包与挂载点**:

| 挂载点 | reddit(REPLY族) | facebook(POST族) | telegram(POST族) |
|---|---|---|---|
| approval_graph 前段 | scan_rising+select_target+read_thread | 无(直接 brief→generate) | 无 |
| policy_validator 参数 | 剥链接(养号)/单tick单评 | **链接迁移到首条评论**(发布图内: publish 后自动跟一条 comment 放链接) | 纯文本/频道格式校验 |
| pacer 规则 | 2/日,3h | 2/日,8h | 2/日,8-12h 摊开 |
| verify_ext | automod 删除延迟复查 | 帖子可见性+首评存在性 | message_id 存在性 |
| 工具调用 | Composio reddit comment | Composio FB page post + comment | Composio TG send_message |

发布图的 policy_validator'/verify 按平台参数化(读 strategy_rules + platform 字段路由) —— 图结构零分叉,行为全数据驱动。

### 8.5.5 scheduled 表 + 发布队列页

```sql
CREATE TABLE scheduled(
  task_id TEXT, platform TEXT, not_before TIMESTAMPTZ,
  status TEXT DEFAULT 'pending',  -- pending|firing|done|cancelled
  PRIMARY KEY(task_id));
```

| 页面 | 数据源 | 内容 |
|---|---|---|
| 审批页(已有) | pending_reviews + get_state | 待审草稿卡 |
| 发布队列页(新增) | scheduled 表 SELECT | task_id/平台/预定时间/状态;[立即发布]按钮=调 pacer.decide(加速重判,不破规则) |

### 8.5.6 Kafka 消息全景

```
posts.ready     审批通过 → pacer(即时判决)      {task_id, platform}
posts.published record 后 → 指标/通知           {task_id, ref_id, verified}
延时不用 Kafka 延迟队列 —— 延时件落 scheduled 表 + 定时器扫(简单可查)
```

---

## 9. 三层规则引擎（站点 / 子社区 / 自定）—— 2026-10-14 增补

### 9.1 规则分层

```
Tier 1 站点规则(platform): 平台官方,违反=封号
      例: Reddit 反spam政策、API ToS、内容红线;来源=平台政策,低频变更
Tier 2 子社区规则(subcommunity): 各sub自治,违反=删帖/禁言
      例: r/ChinaTravel 禁广告/禁链接/karma门槛/话题限制
      来源=sub 规则页(半自动抓取+人工确认),中频
Tier 3 自定规则(own): 我们给自己定的
      例: 每sub每天≤2帖、间隔≥3h、全局日配额、静默时段、去重阈值
      来源=strategy_rules,运营随时改,高频
优先级铁律: Tier1 > Tier2 > Tier3 —— 自定规则再宽不能松过平台红线
```

### 9.2 数据模型

```sql
CREATE TABLE rules(
  id INTEGER PK,
  tier CHECK(platform|subcommunity|own),
  scope TEXT,          -- platform='reddit' / sub='r/ChinaTravel' / 'global'
  rule_key TEXT,       -- max_posts_per_day|min_interval_min|no_links|no_ads|
                       -- image_policy|karma_floor|quiet_hours
  rule_value TEXT,
  enforcement CHECK(pacer|validator|discover_filter|post_check),
  violation_action CHECK(block|strip|skip|escalate|reschedule),
  status DEFAULT active,             -- 软删
  source TEXT,        -- 'platform_tos_v2026.3'/'sub_sidebar_2026-10'/'operator'
  updated_at TIMESTAMP);
```

### 9.3 执行点矩阵

| 执行点 | 时机 | 规则例 | 违例动作 |
|---|---|---|---|
| discover_filter | 选目标 | Tier2 禁广告→不选;karma门槛 | skip 换目标 |
| pacer | 放行前 | Tier3 2/日、3h、静默时段 | reschedule |
| policy_validator | 发布前最后闸 | Tier1 图片政策;Tier2 禁链;Tier3 词表 | strip/block |
| verify_ext | 发布后复查 | Tier2 版主删帖检测 | escalate⏸→教训入库 |

Tier2 半自动维护: discover 顺带抓 sub 规则页 → 与 rules diff → 变更生成"规则变更审批"(人确认后入库) —— 子社区改规则,系统自发现,人只确认。

---

## 10. 记忆层(三层记忆,与 graph state 区分)

### 10.1 Store 存储(namespace 分抽屉,key 分文件夹)

```
namespace(抽屉,按"记忆属于谁")     key(文件夹,按"记忆是什么")   value(JSON,自由schema)
account.reddit                │ trust          → {"tier":"building","incidents":1}
account.reddit                │ profile        → {"best_hours":[..],"survived_tone":[..]}
account.reddit                │ cooldown_until → {"resume_at":"..."}(带TTL临时记忆)
account.facebook              │ trust          → {...}
sub.r.ChinaTravel             │ profile        → {...}
campaign.cj_007               │ angles         → {"covered":[...],"top":[...]}

落库形态(store表): 主键(prefix,key),同地址再写=原地upsert;expires_at/ttl可过期。
value schema 自定义 —— 粗结论可随蒸馏升级为细结论(同地址覆盖)。
```

账本(publish_history)与记忆(Store)的关系: 账本记真相(追加,审计),
Store 是蒸馏出的决策面(upsert,可重建) —— 丢 Store 只损失一次重建,丢账本损失真相。

### 10.2 蒸馏管道两档

| 档 | 触发 | 处理 | 示例 |
|---|---|---|---|
| ① 事件时即时 | verify/探针检出当下 | 单事件→粗结论(止损) | removal → incidents+1 → tier 降级 → 冷却启动 |
| ② 事后批量 | 定时作业扫账本 | 跨事件模式→细结论(学乖) | "该sub连续3帖6h内被删" → 更新profile/策略 |

记忆写入只来自确定性事件或人工裁决 —— 模型永不自改记忆(demo已演示①档)。

### 10.3 信任阶梯(Tier Ladder,4级)

| Tier | 进入条件 | 解锁权利 | 生产link_policy对应 |
|---|---|---|---|
| new 新号 | karma<10 | 纯文字回复/日1条/不主动 | deny |
| building 养号 | karma10-50 或 降级进入 | 纯文字/日≤2 | deny |
| established 成熟 | karma≥50且30天无事故 | 相关链接(AI判相关性+人审) | allow_if_relevant |
| quarantined 隔离 | 短期≥2事故 | 全停,唯人工可解 | 冻结 |

转移规则(信任状态机):
```
new ──karma≥10──► building ──karma≥50+30天无事故──► established
                    ▲ 1次事故(即时降级)               │
                    └───────────────────────────────┤
                    ▲ 人工确认恢复                     │ 短期2次事故
                  quarantined ◄──────────────────────┘
```
原则: 升级靠时间与表现(慢,自动规则),降级靠事故(快,自动事件),
恢复隔离唯人工 —— "earned slowly, lost quickly, restored by humans"。
demo 实现 = established/building 两级 + 一条降级边(最小可演示子集)。



```
会话记忆(graph state)     单次执行        checkpoints
活动记忆(campaign)        单campaign周期  Store ns=campaign_id
   选题历史/已覆盖角度/top帖 → 喂generate防视角重复
长期记忆(long-term)       永久            Store ns=platform/account/sub
   ├ 账号信任: karma轨迹/违规记录 → 信任分级(定链接权限)
   ├ 事故教训: 每次escalate结论 → policy_update回写rules
   └ 子社区画像: 时段效果/措辞存活率
```

写入安全边界: 记忆写入路径全部经人工审批(escalate)或确定性事件 —— **模型不能自改记忆**。
核心闭环: escalate⏸(人结论) → 事故记忆 → 生成/更新 rule → discover/validator 下次生效。

---

## 11. 可测量结果体系(指标定义 → 生产库挖真数)

| 指标 | 定义 | 数据源 |
|---|---|---|
| 发布成功率 | verified=1/尝试数 | publish_history |
| 幽灵发布拦截数 | verify 阶段catch的假成功 | verify 结果记录 |
| 事故→规则转化数 | escalate后产生/修改的rules数 | rules.updated_at×escalate日志 |
| 违例拦截数 | validator各tier block/strip计数 | 校验器输出 |
| 节奏合规率 | 实际间隔≥规则间隔比例 | publish_history×rules |
| 恢复事件数 | 崩溃后checkpoint续跑次数 | 重启标记 |
| 账号健康度 | 违规/删帖率账户级时序 | verify_ext 聚合 |

纪律: 每个写进 note 的数字必须从表里 SELECT 出来 —— 不挖出,不写。

---

## 12. 故障矩阵增补(规则与集成层)

| 环节 | 失败 | 处置 |
|---|---|---|
| discover_filter | sub规则抓取失败 | 保守跳过该sub(fail-safe),下轮重试 |
| policy_validator | 规则求值异常 | 按Tier1最严默认拒绝 |
| rules变更审批 | 人未处理 | 旧规则继续生效(新规则不生效比错发强) |
| Composio API | 超时/429 | 退避≤3→失败状态机冷却 |
| 账号断连 | OAuth过期 | sync检测→status='disconnected'(软删)→escalate⏸人工重连 |
| 工具假成功 | 返回ok回读无物 | verify拦截→有界重试→escalate |
| 平台结构变更 | ToolMessage校验失败 | 告警+该平台暂停降级(他平台不受累) |

---

## 13. Composio 集成层(三平台统一出口)

```
图节点(publish/discover/verify)
   │ @tool(langchain-mcp-adapters 包装)
   ▼
Composio 集成层 —— 所有平台动作唯一出口,不自建平台SDK
   facebook: publish post/comment/read_back
   reddit:   search(enriched:真实karma)/comment/read_back
   telegram: send_message/read_back
   │ MCP
   ▼
Composio 云端(平台账号经 Composio Connect OAuth)
```

原则: 换/加平台=配置不改代码;限流重试公共逻辑收在一处。

⚠️ 生产实证陷阱(幽灵发布根因,写入工具契约出生证明):
Composio MCP 只暴露 COMPOSIO_* 元工具 —— 平台动作必须经 COMPOSIO_MULTI_EXECUTE_TOOL
路由;直调工具名会**静默失败但返回成功**。
对策(三重): 工具包装层强制正确路由 + 不可逆调用后接 verify 回读 + 集成层单测覆盖该路径。

---

## 8. 交付节奏

| 时间 | 交付物 |
|---|---|
| 今天 | Phase 1：`publish_flow` 主图（gen→approve⏸→publish(Send)→verify→record）+ SqliteSaver + 错误边 + policy_validator —— 可运行 |
| 明天 | Langfuse tracing 接线 + 迷你 RAG grounding（retriever 节点）+ eval 套件雏形 |
| 技术轮前 | 端到端演示脚本：崩溃恢复（kill 进程→续跑）、interrupt 审批流、幽灵发布验证各演示一遍 |
