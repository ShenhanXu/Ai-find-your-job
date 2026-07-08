# 项目改造 Recap（面试准备用）

> 覆盖 2026-07-04 ~ 2026-07-07 对两个项目的全部改动：
> **Ai find your job**（主站：AI 求职 copilot，被测系统）+ **JobTrace AI**（可观测 + 评测平台，测试系统）。
> 按"问题 → 决策 → 实现 → 验证"组织，方便按 STAR 结构复述。
> 成绩单数字均有 JSON 存档：`JobTrace AI/data/eval/results/2026-07-07-*.json`。
> 本文档经 5 个独立核查 agent 对照代码库逐条验证（120 条声明，10 处修正已合入）。

---

## 0. 一句话总结（Elevator Pitch）

> 我给自己的 AI 求职 copilot 做了三件事：把 LLM 调用层重构成带重试的全异步架构；用"多层防御"修掉了意图路由的澄清死循环；然后把配套的监控平台升级成**能判卷的评测系统**（47 条标注意图用例 + 10 条黄金检索用例 + SLO 门禁），第一次运行就抓到了真实 bug，并量化证明了新版本对线上旧版的碾压：意图准确率 55%→89%，禁止意图命中（含死循环回归）2 处→0，检索通过率 0/10→10/10。

---

## 1. 系统架构背景

**主站 Ai find your job**（`找工/Ai find your job/`）
- Next.js + FastAPI + PostgreSQL(pgvector) + Redis
- RAG 链路：问题 embedding（Jina）→ pgvector 相似度检索 → DeepSeek 生成回答（SSE 流式）
- 混合意图路由：规则优先（免费、确定性），规则不可信时升级 LLM 路由（DeepSeek JSON mode），14 种 intent
- 生成式 UI：按 intent 输出对比卡片 / 技能矩阵 / 简历清单 / 操作按钮
- MCP stdio server 暴露 search_jobs / get_job_details / prepare_application_action

**JobTrace AI**（`找工/JobTrace AI/`）
- 主站的 fork，改造成可观测平台：请求级 trace（PG 持久化）、Prometheus/Grafana、负载测试、live-target 代理（可打线上部署）、replay、React 工作台
- 两端配合：主站支持 `x-jobtrace-monitoring: internal` 请求头，把内部 workflow trace（intent_router → jina_embedding → pgvector_search → deepseek_llm_call 每步耗时）随响应返回给 JobTrace

---

## 2. 阶段一：LLM 调用层重构（主站）

### 问题（审计发现）
1. **裸 urllib 同步调用散落 7 处**（同步 chat + 流式 chat / router / eval / 3 个 embedding provider），阻塞 FastAPI 线程池——40 个并发用户等 LLM 时，第 41 个连 /health 都要排队
2. **零重试**：DeepSeek/Jina 一次网络抖动 = 用户直接看到降级回答
3. **流式路径丢 instructions 的真 bug**：`call_chat_llm_stream` 不接受 system prompt 参数，且两处 system prompt 文案已漂移
4. 无 max_tokens 上限、无 token 用量统计、无连接复用

### 实现
- 新建 [apps/api/app/llm_client.py](../apps/api/app/llm_client.py)：httpx 同步+异步双客户端（连接池）、指数退避重试（尊重 `Retry-After` 上限 30s；重试 429/500/502/503/504 与网络传输错误，其余 4xx 不重试）、SSE 流式辅助（**只在首字节前重试**，避免内容重放）
- 关键技巧：`LLMRequestError` 故意做成 **OSError 子类**——现有所有 `except (OSError, ...)` 降级逻辑零改动兼容
- `chat_with_rag` / `chat_with_rag_stream` / `route_intent` 转 **async**；embedding 和 pgvector 查询（同步库）走 `asyncio.to_thread`，避免卡事件循环
- 统一 `DEFAULT_CHAT_INSTRUCTIONS` 常量修复流式丢 instructions；`max_tokens` 经 `LLM_MAX_OUTPUT_TOKENS`（默认 1200）；DeepSeek 流式加 `stream_options.include_usage`
- token 用量作为 `llm_usage` 事件写入 workflow trace（`workflow_trace.record_trace_event`）
- 意外收获：async 生成器被 uvicorn 托管后，**客户端断开自动取消流式生成**——"用户关页面还在烧 token"的问题顺带解决

### 边界取舍（面试常问）
- 为什么 embedding/DB 不也转异步？psycopg 同步驱动 + embedding 调用只有几百 ms，`to_thread` 借线程即还；LLM 等待 5-60s 才是必须真异步的大头
- 为什么不用 OpenAI SDK？依赖最小化 + 多供应商（DeepSeek/Jina/Gemini）统一一层更薄

---

## 3. 阶段二：澄清死循环修复（主站）

### 问题（用户截图复现）
用户问"你看看我找 intern 好还是全职好"→ 被追问"请补充 **desired_action**"（内部字段名直接漏给用户）→ 用户回答后**又被原样追问一遍**，永远出不去。

### 根因（三层）
1. 澄清无状态：第二轮消息被当孤立输入重新路由
2. `normalize_route` 把低置信一律降级成 `clarification_needed`——检索很便宜，追问才是用户流失
3. 澄清文案直接拼内部 entity key

### 多层防御（intent_router.py + ai_chat.py + 前端）
| 层 | 机制 | 性质 |
|---|---|---|
| 1 | `escalate_repeated_clarification`：上一轮是澄清、这轮还想澄清 → 强制升级 `job_search`，用**两轮合并文本**做检索 query | 代码硬保证（防第二轮）|
| 2 | 低置信降级目标改为 `job_search` 带检索（只读安全集 `RETRIEVAL_SAFE_INTENTS`）；澄清只留给非检索安全的 intent（写操作如 application_action 缺岗位，以及 application_status_query / platform_help 等需精确上下文的 intent）| 代码硬保证 |
| 3 | `prefer_retrieval_over_generic_clarification`：LLM 返回的澄清若**只缺泛泛字段**（desired_action/query）且问题明显是求职域 → 直接检索；缺**具体**字段（job_id 等）的澄清保留 | 代码硬保证（防第一轮，后补,见阶段五）|
| 4 | `MISSING_FIELD_PROMPTS` 字段名→人话映射 + 3 个 `suggest_reply` 快捷按钮（前端 `sendChatQuestion` 回调接通）| UX |
| 5 | 路由 prompt 加规则："用户在回答上一轮澄清时合并两轮判断，禁止连续澄清" | LLM 软约束 |

- 澄清消息用稳定前缀 `CLARIFICATION_MARKERS`（"我需要再确认一下"/"I need one more detail"）做检测,注释要求两文件同步
- 回归测试照截图对话写：`test_screenshot_flow_clarification_then_answer` 断言第二轮**永不**是澄清

---

## 4. 阶段三：把 JobTrace 从监控平台升级成评测系统

### 判断
JobTrace 原本只回答"快不快、挂没挂"（SLO/体检），回答不了"答得对不对"（eval/考试）。死循环 bug 在它所有指标里都是绿的。**AI 系统特有的失败方式是"健康地、飞快地给出错误答案"**，必须补判卷层。

### 交付（JobTrace 仓库）
- **数据集** `data/eval/`：
  - `intent_cases.json`：47 条标注意图用例（中英双语、多轮带 messages、`expected` 接受集 + `forbid` 禁止集——4 条 forbid 用例含死循环回归 loop-01..03,命中禁止意图则**整场 FAIL**）
  - `retrieval_cases.json`：10 条黄金查询→期望岗位 ID（从 seed 数据程序化生成再人工校准；`min_hits` 判定适配主站对重复岗位的去重）
- **黑盒 runner** `apps/api/app/evals.py`：**零依赖 fork 的业务代码**（仅复用 live_target 的 HTTP 辅助），只走 HTTP 打目标 `/chat`；输出准确率、混淆矩阵、失败明细；识别"目标降级"（embedding 限流等）计为基础设施错误而非质量分；`POST /evals/run` + CLI（失败退出码 1,可做部署门禁）
- **SLO 门禁**：负载测试 `thresholds`（max_p95_ms / max_error_rate / min_cache_hit_rate），报告带 `passed` + `threshold_violations`
- **Replay diff**：重放返回 `deterministic` + `differences`，抓路由抖动/检索漂移
- **真实 token 消费**：`monitoring_level=internal` 时解析主站 `llm_usage` trace 事件，聚合真实 prompt/completion tokens（`token_source: "reported"` 取代硬编码估算）
- **CI**：两仓库 pytest + 前端 tsc typecheck；JobTrace 另有每日定时 Live Eval Gate（cron 打线上部署,红灯即报警）
- **工作台 UI**：Eval gate 面板（一键跑评测、红绿横幅、失败明细）、SLO 输入框、真实 token 横幅、replay diff 结论
- **fork 漂移治理**：依赖分析确认无死代码可删；README 正式警告"本仓库的 ai_chat/intent_router 是主站的漂移 fork,严禁据此推断主站行为"——评测代码全部黑盒正是为此

---

## 5. 阶段四+五：评测驱动的修复闭环（最有故事性的部分）

评测系统连续抓到真问题,形成"评测发现 → 修复 → 重跑验证"的闭环：

1. **CJK 长度门槛 bug ×2**："我想找实习"（5 个字,完整请求）被 `meaningful_question` 的拉丁 8 字符门槛当垃圾拒掉;修完主站的又在 `intent_router.meaningful_text` 发现同族第二处——同类 bug 散在两处,黑盒评测比逐文件排查兜得住
2. **误报排查**：检索首跑"全零"——手动 curl 单发正常,批量并发才挂 → 定位为**并发打爆 Jina embedding 限流**导致主站降级;改进 runner 把降级显式标为 `target degraded` 而非无声记零
3. **黄金集校准**：seed 数据每岗位有 3-4 个重复副本、主站检索去重,满分物理不可达 → 引入 `min_hits`（"对的岗位出现了没"）替代全量 recall
4. **关键词陷阱**："帮**我保存**这个岗位"被状态词表的"我保存"误命中成状态查询;查申请进度（"have i applied"/"申请进度"）反而无词可匹配 → 词表重构为 query-shaped tokens,一组修复救三题（aa-04/as-01/as-02）
5. **flakiness 治理（阶段五）**：存档重跑暴露两个稳定性问题——
   - `jc-04` 首轮澄清随 DeepSeek 情绪波动出现 → 补第 3 层防御 `prefer_retrieval_over_generic_clarification`（泛泛澄清+求职域文本=直接检索,具体字段澄清保留）
   - 意图套件 47 用例把 Jina 配额烧光,检索套件全程降级 → runner 改为**检索先跑** + 降级用例冷却 20s 后串行重试一轮
6. **对线上旧部署跑同一套评测**：55% 准确率 + 禁止命中 2 处（含死循环 loop-02 实锤）+ 检索 0/10（旧代码无重试,embedding 链路瘫痪）——**为重新部署提供最硬证据**

### 最终成绩单（2026-07-07,JSON 存档见 `JobTrace AI/data/eval/results/`）

| 指标 | 线上旧版（prod-old.json）| 本地新版（local.json）| 门禁线 |
|---|---|---|---|
| 意图准确率（full LLM）| 55.3% (26/47) | **89.4% (42/47) ✅** | ≥85% |
| 禁止意图命中 | 🔴 2（jc-04、loop-02 死循环）| **0 ✅** | =0 |
| 检索通过率 | 0/10（embedding 链路瘫痪）| **10/10 ✅** | ≥60% |
| 测试 | — | 主站 36 过 / JobTrace 34 过 | CI 门禁 |

> 注：LLM 路由有 ±1 题（~2%）的运行间波动,门禁看趋势不看单次;单次数字以存档 JSON 为准。

剩余 5 个意图失败集中在"专业 intent 塌缩成 job_search"（skill_gap/resume_tailoring 类,答案质量没问题但丢专属 UI）+ DeepSeek 偶发 router_unavailable——已知、有边界,是下轮优化清单。

---

## 6. 面试 Talking Points（English）

**Elevator pitch**: *I built an AI job-search copilot (RAG over pgvector + hybrid intent routing + generative UI), then built a second system to test it: an observability platform with a black-box evaluation gate. The eval suite — 47 labeled intent cases and 10 golden retrieval cases with hard-fail "forbidden intent" regressions — caught real production bugs on its first runs and quantified my fixes: intent accuracy 55%→89%, a clarification dead-loop eliminated, retrieval pass rate 0/10→10/10. Every number is backed by archived run artifacts.*

**STAR: the clarification dead-loop**
- S: Users answering the bot's clarifying question got the identical question again — an infinite loop; all monitoring dashboards were green.
- T: Fix the loop AND make this class of failure permanently detectable.
- A: Layered defenses — a code-level anti-repeat guard that retrieves over merged two-turn context, a retrieval-first downgrade policy for read-only intents, a "generic clarification on a job-domain question becomes retrieval" guard for first turns, humanized copy with quick-reply buttons, and router prompt rules. Encoded the exact conversation as `forbid`-tagged eval cases that fail the whole suite on recurrence.
- R: Loop impossible at code level; forbidden hits went from 2 on deployed prod to 0 locally, archived in run artifacts.

**STAR: eval-driven bug discovery**
- The Latin-calibrated 8-char "meaningful question" gate rejected complete 5-char Chinese requests ("我想找实习"). Found by the eval suite in minutes; the same bug family existed in two files — black-box evaluation caught what code review missed.

**STAR: debugging a false alarm in my own eval**
- Retrieval suddenly scored 0/10. Single manual requests worked; only batches failed. Root cause: the intent suite's 47 LLM-mode cases exhausted the embedding API's rate limit before the retrieval suite ran. Fixes on both sides: the runner now reports degraded targets as infrastructure errors (not quality misses), runs retrieval first, and retries degraded cases after a cooldown. Lesson: an eval system needs the same reliability engineering as the system it judges.

**Likely follow-ups**
- *Why not function calling instead of an intent router?* Deterministic writes, cost control (rules answer for free), and generative-UI selection need intents; MCP tools already cover the agent-style path — migrating is a v2 option, and the eval suite makes that migration safe to attempt.
- *How do you handle LLM nondeterminism in evals?* Acceptance sets (`expected` lists), forbidden sets for hard constraints, degraded-target detection kept separate from quality scores, retry-after-cooldown for transient infra, and I treat ±1 case as noise at n=47 — the gate matters on trends.
- *Why is the eval black-box?* The test repo is a fork of the target and has drifted; only HTTP behavior is truth. It also lets the same suite score prod vs local identically — that is exactly how I produced the 55%-vs-89% comparison with artifacts.

---

## 7. 已知短板与下一步

1. 专业意图塌缩（skill_gap/resume_tailoring → job_search）：需要更强的 few-shot 路由 prompt 或迁移 function calling
2. DeepSeek 偶发 router_unavailable（并发下重试仍失败）：查限流 vs JSON 解析,考虑 fallback 到 OpenAI 路由
3. Applications 仍存内存 dict,重启即丢（演示硬伤,待落库）
4. **两个仓库大量改动未 commit**;AWS 线上仍是旧代码,commit 后跑 `infra/aws/deploy-ecs-api.sh` 重新部署,再用 eval gate 打 CloudFront 验收（预期从 55% 涨到 ~89%）
5. JobTrace 的 `MAIN_SITE_TIMEOUT_SECONDS=30` 对重型 LLM 工作流太紧（实测 37s）,`.env.local` 待调
6. 重型工作流回答顶到 1200 max_tokens 被截断,`LLM_MAX_OUTPUT_TOKENS` 需按场景放宽

---

## 附录：待 commit 的 diff 中、不在本 recap 时间窗内的工作（6/18–6/25）

面试官如果看仓库 diff 会看到这些,须能解释——它们与上文改动在同一批未提交变更里：

- **账号系统**：`apps/api/app/auth.py`（PBKDF2 密码哈希 + HMAC access token,14 天 TTL）、`/auth/register|login|me` 端点、`users`/`resumes` 表 + 每用户唯一 active 简历索引（database.py + init.sql）
- **MCP server**：`apps/api/app/mcp_server.py` + `tests/test_mcp_server.py`（stdio JSON-RPC,3 个 tools）
- **生成式 UI 构建器**：`apps/api/app/copilot_workflows.py`（对比卡片/技能矩阵/简历清单的 workflow 拼装）
- **部署脚本 6 份**：`infra/aws/`（deploy-ecs-api / create-api-cloudfront / run-ecs-embedding-backfill / update-ecs-ai-secrets / update-ecs-frontend-origin）+ `infra/vercel/deploy-web.sh`
