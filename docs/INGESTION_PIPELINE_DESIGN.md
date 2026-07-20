# 实时岗位摄取管道 — 设计文档

> **状态：已实现**（2026-07-14）。代码在 `apps/ingestion/`，运行方式见 [apps/ingestion/README.md](../apps/ingestion/README.md)。
> P1–P3 全部落地；P4 的 SQS 适配器与 generic_html 铺量仍为可选后续项。

> 目标：把主站 100 条种子岗位替换为真实岗位流。
> 定时抓取 Greenhouse/Lever 公开 API → Redis Streams 队列 → 指纹去重幂等写入 → 增量 embedding 索引。
> 覆盖限流、幂等、死信队列（DLQ）、schema 演化四个分布式系统主题。

## 1. 现状盘点（本设计的起点）

| 已有资产 | 位置 | 说明 |
|---|---|---|
| ATS 抓取 + 解析 + 指纹 | `apps/api/app/ingestion.py` | Greenhouse/Lever/JSON-LD/HTML 解析器、sha256 指纹、added/updated/unchanged 判定均已实现，但是**同步单进程** |
| 公司源配置 | `data/company_sources.json` | 146 家公司：27 Greenhouse + 9 Lever（API 直连）+ 110 generic_html（需适配器）；schema 已含 `priority`、`crawlIntervalMinutes` 字段；当前全部 `enabled: false` |
| 幂等 upsert | `apps/api/app/database.py::upsert_jobs` | ON CONFLICT 写入 Postgres |
| Embedding | `apps/api/app/backfill_embeddings.py` | 1536 维（text-embedding-3-small），**逐条串行**，429/5xx 重试已有 |
| 基础设施 | `docker-compose.yml` | Redis 7 + pgvector/pg16 已在跑 |

缺口 = 本设计要建的东西：调度器、队列解耦、worker 池、限流、DLQ、增量 embedding、消息 schema 版本化。

## 2. 架构

```
                    ┌─────────────────────────────────────────────────┐
                    │              Redis Streams                       │
 ┌───────────┐      │  crawl.tasks ──┐        jobs.raw ──┐             │
 │ scheduler │─XADD─┼──────────────► │                   │             │
 │ (每60s扫描)│      │                │                   │             │
 └───────────┘      │  crawl.dlq ◄───┤        embed.dlq ◄┤             │
                    └────────┬───────┴─────────┬─────────┴─────────────┘
                             │ XREADGROUP      │ XREADGROUP
                    ┌────────▼────────┐   ┌────▼─────────┐   ┌──────────────┐
                    │ crawl-worker ×N │──►│ upserter     │──►│ embedder     │
                    │ async httpx     │   │ 指纹比对      │   │ 批量64/请求   │
                    │ 令牌桶限流       │   │ 幂等upsert    │   │ 增量索引      │
                    └─────────────────┘   └──────┬───────┘   └──────┬───────┘
                                                 │                  │
                                          ┌──────▼──────────────────▼──────┐
                                          │   Postgres + pgvector          │
                                          │   job_postings (embedding)     │
                                          └────────────────────────────────┘
```

新增服务放在 `apps/ingestion/`（monorepo 内新可部署单元），复用 `apps/api/app` 里的纯函数（解析器、指纹、模型）：

```
apps/ingestion/
  scheduler.py    # 定时器：扫描到期源 → XADD crawl.tasks
  crawl_worker.py # 消费者组 crawlers：拉 ATS API → 逐岗位 XADD jobs.raw
  upserter.py     # 消费者组 upserters：指纹比对 → 幂等写库 → 变更岗位 XADD jobs.embed
  embedder.py     # 消费者组 embedders：批量 embedding → 写 pgvector
  queue.py        # Streams 封装：XADD/XREADGROUP/XACK/XAUTOCLAIM/DLQ，留 SQS 适配接口
  ratelimit.py    # Redis 令牌桶（Lua 脚本），per-host
  envelope.py     # 消息信封 + schema_version 校验
```

## 3. 消息设计

所有消息共用信封（schema 演化的载体）：

```json
{
  "schema_version": 1,
  "event_id": "uuid",
  "occurred_at": "2026-07-08T12:00:00Z",
  "type": "crawl.task | job.raw | job.embed",
  "payload": { ... }
}
```

| Stream | payload | 幂等键 |
|---|---|---|
| `crawl.tasks` | `{source_id, ats_type, url, window}` | `crawl:{source_id}:{window}` — Redis `SET NX EX`，同一调度窗口内不重复入队 |
| `jobs.raw` | 归一化岗位（`JobPosting` 字段 + `raw` 快照） | `job_id`（已有 `make_job_id`）+ 指纹比对 → 重放是 no-op |
| `jobs.embed` | `{job_id, fingerprint, search_text}` | `{job_id}:{fingerprint}` — 库里该指纹已有向量则跳过 |

**Schema 演化规则**：加字段 = 不升版本，消费者忽略未知字段；改语义/删字段 = `schema_version+1`，消费者按版本分发处理函数，认不出的版本进 DLQ 隔离（不 crash、不丢）。第一个演化案例现成的：v1 岗位事件 → v2 增加 `salary_range` 字段。

## 4. 四个分布式主题的具体实现

**限流**：per-host 令牌桶存 Redis（Lua 保证原子），默认 1 req/s/host；429 响应读 `Retry-After` 退避，5xx 指数退避 + 抖动（1s→2s→4s→8s，±20%）。Greenhouse/Lever 是"一个请求返回整个 board"，实际压力很小，限流主要为 110 个 HTML 站点和演示叙事服务。

**幂等 / at-least-once → 恰好一次效果**：Streams 消费者组保证 at-least-once（worker 崩溃 → pending entry 被 `XAUTOCLAIM` 认领重试）；消费侧全部幂等（上表幂等键），重复投递结果不变。这是面试标准答案的完整落地。

**DLQ**：投递次数（`XPENDING` delivery count）> 5 的消息，连同错误信息 XADD 到对应 `.dlq` stream 并 XACK 原消息。`GET /api/ingestion/dlq` 列出死信，`POST /api/ingestion/dlq/{id}/replay` 重放回主 stream。毒消息（解析必炸）最多重试 5 次后隔离，不阻塞消费。

**Job 生命周期**（顺带解决"过期岗位"）：每次源爬取成功后，该源下 `last_seen_at` 落后 3 个爬取周期的岗位标记 `status=closed`，搜索默认过滤。

## 5. 能实现的功能（具体数字）

### 抓取频率（scheduler 每 60s 扫描一次到期源）

| 层级 | 公司 | 间隔 | 说明 |
|---|---|---|---|
| P1 | 重点公司（自选 ~10 家 Greenhouse/Lever） | **每 30 分钟** | 新岗位最快 30 分钟内入库 |
| P2 | 其余 API 直连源（~26 家） | 每 3 小时 | schema 默认值就是 180 分钟 |
| P3 | generic_html（110 家，Phase 4） | 每 6 小时 | 需逐站适配器，暂缓 |

### 单轮爬取耗时

- Greenhouse/Lever：**每公司 1 个 HTTP 请求**返回整个 board（JSON 0.5–10 MB，响应 0.5–3s）。
- 35 个 API 源 × ~2s，4 个 worker 并发 ≈ **一轮全量 sweep < 30 秒**。
- 首轮全量（35 源、按 role/location 关键词过滤后估计 1000–3000 条相关岗位）：抓取+解析+入库 **约 1–2 分钟**。

### Embedding 吞吐

- 由逐条改为**批量 64 条/请求**：首轮 ~2000 条 ≈ 32 个请求 ≈ **1–2 分钟**建完全量索引。
- 稳态只 embed 新增/指纹变化的岗位（预计每天几十条）→ **秒级**。

### 端到端新鲜度（对外可承诺的指标）

> 公司在 Greenhouse 上发布新岗位 → P1 公司 **最迟 ~31 分钟**、P2 公司最迟 ~3 小时，岗位即在主站可搜索（含语义搜索）。
> 链路分解：调度窗口（≤30min）+ 抓取入库（秒级）+ embedding（≤1min）。

### 容错行为（可现场演示）

| 故障注入 | 系统行为 |
|---|---|
| `docker kill` 一个 crawl-worker | pending 消息 5 分钟可见性超时后被其他 worker `XAUTOCLAIM` 接管，零丢失（超时刻意大于最长合法爬取：Retry-After 120s + 限流等待） |
| ATS 返回 429 | 按 `Retry-After` 退避，任务不失败 |
| 消息连续失败 5 次 | 进 DLQ，`/api/ingestion/dlq` 可见，可一键重放 |
| 重放整个 stream | 幂等键保证 jobs 表结果不变（added=0） |
| `--scale crawl-worker=3` | 消费者组自动分摊，sweep 时间近似线性下降 |

### 运维可见性

`GET /api/ingestion/status`：各源最近成功时间/错误、各 stream 深度与 pending 数（XLEN/XPENDING）、最近一轮 `IngestionRunResult`（seen/added/updated/unchanged）、DLQ 计数。后续接入 JobTrace AI 面板。

## 6. 实施阶段

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| **P1 队列骨架** | `queue.py`+`envelope.py`+scheduler+crawl_worker+upserter，启用 35 个 API 源，compose 加服务 | 主站展示真实岗位；杀 worker 不丢消息；重放幂等 |
| **P2 增量索引** | embedder（批量64）+ `jobs.embed` + 岗位生命周期 | 新岗位 ≤1min 可语义搜索；旧岗位自动 closed |
| **P3 可靠性完善** | 令牌桶限流、DLQ + replay 端点、status 端点、schema v2 演化案例 | 五个容错演示全部可复现 |
| **P4（可选）** | SQS 适配器（队列接口切换）、generic_html 适配器铺量、JobTrace 集成 | — |

## 7. 关键取舍记录

- **Redis Streams 而非 SQS**：Redis 已在 compose，本地可完整演示消费者组/DLQ/重放；队列抽象成接口留 SQS 适配器，README 说明生产切换路径。
- **不新建 repo**：管道价值在于喂主站的库和索引；解析/指纹逻辑直接复用，git 历史呈现"单体→事件驱动"演化叙事。
- **fetch 层改 async httpx**：复用上个 commit LLM gateway 的 retry 模式，替换 `urllib`。
- **公司源仍以 JSON 为 seed**：启动时载入 Postgres 表（含 `last_crawled_at`），调度状态入库，配置文件保持可读；`sync_from_json` 会同步禁用 JSON 中已不存在的残留行（JSON 是配置权威）。

## 8. 实测结果（2026-07-17，对真实公开 API 的端到端验证）

| 设计估算 | 实测 |
|---|---|
| 一轮全量 sweep < 30 秒 | **27.1s**（36 源冷缓存首轮）/ 7.6s（重爬轮） |
| 首轮过滤后 1000–3000 条相关岗位 | 首轮发布 1147 条事件，**入库 587 条**；修复 8 个坏 board token 后累计 **~774 条真实岗位** |
| 入库耗时秒级 | 1147 条事件 **3.8s** 幂等写入（added/updated/unchanged 正确分类） |
| Embedding 批量 64/请求 | Jina 免费档实测 **100k tokens/分钟限流**，批 64 会触发 429 → 生产参数调为批 ≤32 + 间隔；429 消息正确留在 pending 重试，无丢失 |
| 启用 36 个 API 源 | **35 个**（rec-room 无公开 board 已禁用；echodyne/terrapower 等 8 个 token 为老 probe 误提取，已逐一对公开 API 验证修正）；35/35 爬取成功 |
| DLQ 演示 | 坏 token 源真实触发 404 → 立即进 DLQ 并记录源错误，管道其余部分不受影响 |

多智能体对抗式审查（4 维度 finder + 每 finding 3 视角验证）确认并修复 5 个缺陷：
embed 任务在"写库提交后、发布前"崩溃窗口永久丢失（已加 `unchanged`+无向量自愈重发，真实数据上 197 条孤儿岗位借此全部自动恢复）；去重键寿命长于向量导致指纹 A→B→A 翻转吞任务（embedder 成功后释放键）；crawl worker 同步 DB/Redis 调用阻塞事件循环（全部 `asyncio.to_thread`）；`CLAIM_IDLE_MS` 60s 小于最长合法爬取导致多副本互抢（提高到 5 分钟）；`dead_letter` 的 XADD+XACK 非原子（改 MULTI）。
