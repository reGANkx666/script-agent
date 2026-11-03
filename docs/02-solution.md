# 02 · 方案与实现

## 1. 总体结构

```text
Browser Control Room
        │ HTTP / SSE
        ▼
FastAPI ──► RunManager ──► LangGraph StateGraph
        │                         │
        │                         ├─ Decision Model (DeepSeek)
        │                         ├─ Writer Model (DeepSeek)
        │                         └─ Tools
        │                              ├─ BM25 + pgvector + RRF + rerank
        │                              ├─ duration
        │                              ├─ rule engine
        │                              └─ fact ledger / output validator
        ▼
PostgreSQL: runs / events / checks / documents / chunks / materials
```

关键原则：

- 模型只负责“选择动作”和“写脚本”；
- 运行时负责状态、预算、工具执行、暂停恢复和终态；
- 所有外部事实必须经过 `materials → Claim → Shot` 链路；
- 失败是显式状态，不是异常日志。

## 2. LangGraph 运行时

### 节点

```text
START
  → validate_input
      ├─ 缺字段 → ask_user → agent
      └─ 完整   → agent
  → agent
      ├─ search_materials
      ├─ draft_script
      ├─ estimate_duration
      ├─ check_rules
      ├─ revise_script
      ├─ ask_user
      ├─ finalize → END
      └─ fail → END
```

工具节点完成后都会回到 `agent`。  
`agent` 每次只接收一个 `ActionDecision`，由条件边路由，不把整条流程包进 `AgentExecutor`。

### 调用流程：一次运行实际发生什么

```text
POST /v1/runs
→ 写 runs(QUEUED)
→ RunManager 启动后台图
→ validate_input
→ agent 读取最新观察
→ 执行一个工具
→ tool.result 写入 state / events
→ agent 再读最新观察
→ ...有限步...
→ finalize / fail
→ runs 落终态
→ SSE 客户端收到事件
```

关键点：

- 模型每次只看到**当前投影状态**，不看到全量历史；
- 工具执行由节点完成，不交给模型直接调用；
- 每次动作都进入 `events`，可按 `seq` 回放；
- `RunManager` 不替模型决定下一步，只执行和记录。

### 动态决策：模型如何根据工具结果改动作

这里的“动态”不是写死 if-else，而是模型在每一轮读取 `last_observation` 后重新选择。

| 观察 | 模型可能动作 | 代码护栏 |
|---|---|---|
| 缺 `platform` / `target_duration_sec` | `ask_user` | 状态 `WAITING_INPUT`，不猜值 |
| `search=EMPTY` 且重写次数未耗尽 | 换 query 再检索 | 最多 2 次重写 |
| 连续 `EMPTY` | `ask_user` 或 `fail(NO_MATERIAL)` | 不生成事实 |
| `search=ERROR` | `fail(TOOL_ERROR)` | 不降级伪造结果 |
| 首稿 38s，目标 30s | `revise_script` | 时长差 > ±2s |
| 修订后 28s | `estimate_duration` → `check_rules` | 重新估算，不复用旧时长 |
| 15s 目标仍 18s | `fail(UNSATISFIABLE_DURATION)` | 修订次数耗尽 |
| 达到步数/工具预算 | `fail(MAX_STEPS)` | 预算硬护栏 |

用 A 场景举例：

```text
search_materials("字幕") → P01
→ draft_script → 首稿
→ estimate_duration → 38s / target 30s
→ revise_script → 压缩
→ estimate_duration → 28s / passed
→ check_rules → pass
→ finalize
```

注意第 3 步不是“继续写”，而是模型读到 `passed=false` 后选择 `revise_script`；  
第 5 步也不是复用旧结果，而是重新估算。

用 E 场景举例：

```text
search → draft → 22s
→ revise → estimate → 20s
→ revise → estimate → 18s
→ fail(UNSATISFIABLE_DURATION)
```

这就是“改变时长或检索结果，下一步动作随之调整”。

### 预算与未完成状态

```text
MAX_STEPS=12
MAX_TOOL_CALLS=12
```

达到任一上限时，`agent` 不再调用模型，直接进入 `fail(MAX_STEPS)`。  
示例 F 使用 `MAX_STEPS=4` 验证：

```text
search + draft + draft + draft → step=4
→ MAX_STEPS
```

结果见：

- [examples/results/f_max_steps.json](../examples/results/f_max_steps.json)
- [examples/traces/f_max_steps.jsonl](../examples/traces/f_max_steps.jsonl)

### 状态与预算

状态包含：

```text
brief / materials / claims / contexts
draft / duration / check / risks
step / tool_calls / rewrite_count / revision_count
pending_action / last_observation / status
```

默认预算：

```text
MAX_STEPS=12
MAX_TOOL_CALLS=12
MAX_REVISIONS=2
MAX_REWRITES=2
TOOL_TIMEOUT=10s
DURATION_TOLERANCE=±2s
```

### 暂停与恢复

`ask_user` 调用 LangGraph `interrupt()`：

```text
缺 platform / target_duration_sec
→ 状态 WAITING_INPUT
→ POST /resume 补答案
→ Command(resume=answer)
→ 回到 agent
```

checkpoint 存 PostgreSQL，不把未完成状态只放在内存。

## 3. 模型接入

### DeepSeek

使用 LangChain `ChatOpenAI`，指向 DeepSeek OpenAI 兼容端点：

```text
base_url = https://api.deepseek.com
model    = deepseek-chat / 由 DEEPSEEK_MODEL 指定
```

Decision 使用 `bind_tools(tool_choice="auto")`，每次必须返回一个合法动作。

Writer 同样使用 `tool_choice="auto"`，但必须返回 `submit_script`。  
原因是当前 DeepSeek thinking mode 不支持强制指定 function tool_choice。

### DashScope embedding 与 rerank

```text
embedding: text-embedding-v3, 1024 维
rerank:    gte-rerank-v2
```

当前工作区端点只支持 DashScope 原生路径，因此使用：

```text
/api/v1/services/embeddings/text-embedding/text-embedding
/api/v1/services/rerank/text-rerank/text-rerank
```

不使用 OpenAI 兼容的 `/embeddings`。

## 4. 知识库入库

```text
上传
→ 扩展名/大小校验
→ SHA-256 去重
→ 保存原文件到 uploads/
→ 创建 document + ingestion job
→ 后台解析
→ Parent/Child 切片
→ Child embedding
→ 事务写入 documents / chunks / materials
→ document READY + active
```

解析器：

| 格式 | 实现 |
|---|---|
| PDF | pypdf，只解析文本层 |
| DOCX | python-docx，标题 + 段落 + 表格行 |
| TXT | 空行分段 |
| MD | 标题层级 + 段落 |
| CSV | 表头作为上下文，一行一个 Child |
| JSON | 数组/records 逐条扁平化 |

切片：

```text
Child:  20–120 字，一句 / 一行 / 一条记录
Parent: 300–800 字，3–8 个 Child
```

Child 是检索单元和事实单元；Parent 只作为 Writer 上下文，不作为引用来源。

自动发布：

- 解析、切片、embedding 全部成功后 `READY`；
- 任一步失败 `FAILED`，不留下可检索半成品；
- 无人工审核，但支持下架与永久删除。

## 5. RAG 检索

```text
query
→ BM25 Top 30
→ pgvector Top 30
→ RRF 融合 Top 30
→ reranker 精排
→ Top 8 Child
→ 按 parent 聚合
→ Top 4 Parent 上下文
```

RRF：

```text
score(d) = Σ 1 / (60 + rank_i(d))
```

返回结果同时包含：

- 命中的 Child 事实；
- 所属 Parent 上下文；
- BM25 / 向量排名；
- rerank 分数；
- 检索 debug 信息。

### reranker 降级

```text
第一次失败 → 重试
仍失败 → 使用 RRF 原排序
→ result.risks 记录 RERANK_DEGRADED
→ event 记录 rerank_status=degraded
```

embedding 本身失败仍然是 `TOOL_ERROR`，不会伪装成正常结果。

## 6. 事实与规则

### Fact Ledger

```text
materials (child fact)
  → Claim (claim_id, text, source_id)
  → Shot.claim_ids
  → narration 必须包含 claim 原文
  → source_payload 输出 sources[]
```

### 规则检查

- 时长目标 / 平台上限；
- claim 来源白名单；
- 口播包含 claim 原文；
- 无效果数据时禁止效果结论；
- 品牌禁语；
- 字幕密度 warn；
- 最终输出不变量。

block 违规会阻止 `SUCCESS`。

## 7. 数据模型

```text
knowledge_documents
  id, filename, mime, size_bytes, content_hash,
  status, error, storage_path, active

knowledge_chunks
  id, document_id, parent_id, chunk_type,
  ordinal, heading_path, text, token_count,
  embedding, metadata, active

knowledge_ingestion_jobs
  id, document_id, status, stage, progress, error

materials
  id, text, source, source_type, scope, tags,
  embedding, document_id, chunk_id, parent_id

runs / events / brand_rules / platform_rules
```

同一个 PostgreSQL 数据库。`materials` 仍承担“可引用事实 + embedding”的角色，`knowledge_chunks` 保存来源层级和 Parent 上下文。

## 8. API

```text
POST   /v1/runs
GET    /v1/runs/{id}
GET    /v1/runs/{id}/result
GET    /v1/runs/{id}/events
GET    /v1/runs/{id}/trace
POST   /v1/runs/{id}/resume
POST   /v1/runs/{id}/cancel

POST   /v1/knowledge/documents
GET    /v1/knowledge/documents
GET    /v1/knowledge/documents/{id}
GET    /v1/knowledge/documents/{id}/chunks
GET    /v1/knowledge/jobs/{id}
POST   /v1/knowledge/documents/{id}/deactivate
DELETE /v1/knowledge/documents/{id}
GET    /v1/knowledge/evaluation
POST   /v1/knowledge/search
```

## 9. 核心取舍

1. **不用 AgentExecutor**：需要 checkpoint、interrupt、预算和可回放事件。
2. **短块召回、长块返回**：Child 提高检索精度，Parent 补上下文。
3. **确定性规则在模型外**：模型不能自己决定“合规通过”。
4. **不做静默降级**：检索失败、rerank 失败、无素材都必须可见。
5. **rerank 可评估**：固定 golden set 对比 RRF 与 rerank，不假设一定提升。
6. **单实例优先**：本轮用 asyncio 后台任务，生产化再引入持久化 worker/队列。

## 10. 已知限制

- PDF 无 OCR；
- 任务队列是进程内 asyncio，不是持久化队列；
- 原文件存本地 `uploads/`，不是对象存储；
- reranker 在当前离线 golden set 上没有正增益，面板如实展示；
- 81 条 seed 素材仍以 `materials` 形式存在，没有包装成可展示的 seed document。
