# 短视频脚本 Agent — 实施级方案

> 用途：交给实现 Agent 直接照着开发。  
> 关系：`README.md` 是架构说明；本文是**施工图**，规定模块、接口、数据、顺序和验收。  
> 范围：P0 核心闭环 + 5 个验收场景。前端 `web/` 不在本轮改造范围。

---

## 0. 不可变约束

实现时优先遵守这些约束；需要改接口、状态码或数据表时，先停下来询问，不要自行改动。

1. 只实现后端和测试接口，**不修改** `web/` 现有静态前端。
2. 使用 LangGraph **自定义 StateGraph**，不使用 `AgentExecutor` 包住整个流程。
3. 使用 DeepSeek 的 OpenAI 兼容接口；模型名默认 `deepseek-chat`。
4. 使用 DashScope 的 OpenAI 兼容 embedding 接口；向量维度固定 `1024`。
5. 使用已有外部 PostgreSQL，必须启用 `pgvector`；不自动建生产数据库。
6. P0 只做确定性的 5 个验收场景；真实模型只做冒烟，不作为 CI 断言。
7. 不把 API Key、真实产品资料、用户隐私写入仓库、日志或 Trace。
8. 任何“无素材”“工具失败”“无法满足”都必须显式返回，不能编造成功。

---

## 1. 已确认的技术选型

| 领域 | 选型 | 用途 / 理由 |
| --- | --- | --- |
| 语言 | Python 3.11 | LangGraph/LangChain 生态完整 |
| Web | FastAPI + Uvicorn | 原生支持 async、SSE、OpenAPI |
| 编排 | LangGraph `StateGraph` | checkpoint、interrupt、流式状态、可控路由 |
| 模型接入 | LangChain `ChatOpenAI` + DeepSeek | DeepSeek 兼容 OpenAI 协议，改 `base_url/model/api_key` 即可 |
| 模型 | `deepseek-chat` | 用于动作选择和脚本生成；不要默认用 reasoner 做工具调用 |
| Embedding | DashScope `text-embedding-v3` | OpenAI 兼容模式，1024 维 |
| 数据库 | 外部 PostgreSQL + pgvector | 运行状态、事件、素材和向量 |
| Checkpointer | `AsyncPostgresSaver` | LangGraph 暂停/恢复 |
| 检索 | BM25 + pgvector + RRF | 词面精确 + 语义召回，结果可解释 |
| 中文分词 | `jieba` | BM25 查询/素材分词 |
| 测试 | pytest + pytest-asyncio + httpx | 单元、图、API 三层 |
| 包管理 | uv + `pyproject.toml` + `uv.lock` | 可复现安装 |
| 前端 | 保持现状 | 本轮不接实时 API |

### 关于“LangChain 的 OpenAI Agent”

DeepSeek 的接入确实只需要改模型名、Base URL 和 Key：

```python
ChatOpenAI(
    model="deepseek-chat",
    base_url="https://api.deepseek.com",
    api_key=DEEPSEEK_API_KEY,
    temperature=0.2,
    timeout=60,
    max_retries=1,
)
```

但**不建议**把 `create_openai_tools_agent + AgentExecutor` 作为整条流程的容器。原因是它不能自然提供：

- LangGraph checkpoint；
- `interrupt()` 暂停/恢复；
- 当前节点、步数、工具调用次数等状态；
- 可回放的 SSE 事件。

因此采用：

> LangChain `ChatOpenAI` + `bind_tools()` 负责模型动作选择；  
> LangGraph 自定义图负责状态、路由、预算、检查和中断。

这不是绕开 LangChain，而是把 Agent 的“模型能力”和“运行时控制”分开。

---

## 2. 依赖与目录

### 2.1 依赖

在 `pyproject.toml` 中声明，M0 生成 `uv.lock` 后提交：

```text
fastapi
uvicorn[standard]
pydantic
pydantic-settings
langgraph
langgraph-checkpoint-postgres
langchain-core
langchain-openai
psycopg[binary,pool]
pgvector
rank-bm25
jieba
httpx
pytest
pytest-asyncio
```

说明：

- `langchain-openai` 同时用于 DeepSeek Chat 和 DashScope Embedding（OpenAI 兼容模式）。
- 不引入 SQLAlchemy；P0 用 `psycopg` + 原生 SQL，减少迁移成本。
- 不引入向量数据库；pgvector 足够。
- 如果后续要上任务队列，再增加 Redis/Celery，不在 P0。

### 2.2 目录

```text
src/
  main.py
  config.py
  api/
    app.py
    deps.py
    schemas.py
    routes_runs.py
    routes_test.py
    sse.py
  runtime/
    run_manager.py
    run_store.py
    event_store.py
    checkpointer.py
    assembler.py
    graph_factory.py
  graph/
    state.py
    build.py
    routing.py
    nodes/
      validate.py
      agent.py
      search.py
      draft.py
      duration.py
      rules.py
      revise.py
      ask.py
      finalize.py
      fail.py
  domain/
    models.py
    fact_ledger.py
    status.py
    duration_policy.py
    validators.py
  llm/
    base.py
    deepseek.py
    mock.py
    prompts/
      decide.md
      write.md
      revise.md
  tools/
    retrieval.py
    bm25.py
    embeddings.py
    duration.py
    rules.py
    registry.py
  observability/
    events.py
    trace.py
scripts/
  migrate.py
  seed_data.py
  smoke_model.py
data/
  materials.json
  rules.json
tests/
  unit/
  graph/
  api/
  fixtures/
docs/
  implementation-plan.md
```

---

## 3. 环境变量

新增/更新 `.env.example`：

```dotenv
# DeepSeek / OpenAI-compatible chat
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat

# DashScope / OpenAI-compatible embedding
DASHSCOPE_API_KEY=
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DASHSCOPE_EMBEDDING_MODEL=text-embedding-v3
EMBEDDING_DIM=1024

# Existing PostgreSQL with pgvector
DATABASE_URL=postgresql://user:password@host:5432/script_agent

# Runtime
APP_ENV=local
ENABLE_TEST_API=true
MAX_STEPS=8
MAX_TOOL_CALLS=12
MAX_REVISIONS=2
MAX_REWRITES=2
TOOL_TIMEOUT_SECONDS=10
```

启动时必须检查：

- `DATABASE_URL` 可连接；
- `SELECT extversion FROM pg_extension WHERE extname='vector'` 有结果；
- `embedding_dim == 1024`；
- 缺少 `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` 时，`/healthz` 报配置错误；只有测试 Mock 模式可以绕过。

> 不要自动执行 `CREATE DATABASE` 或修改共享库。迁移只负责当前库内的表、扩展检查、索引。

---

## 4. 数据库设计

### 4.1 运行前检查

`scripts/migrate.py`：

1. 连接 `DATABASE_URL`。
2. 执行 `CREATE EXTENSION IF NOT EXISTS vector;`；无权限则明确报错，要求 DBA 开启。
3. 建表、索引。
4. 检查 `vector` 维度和 `materials.embedding` 是否一致。

### 4.2 DDL

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS materials (
  id            text PRIMARY KEY,
  text          text NOT NULL,
  source        text NOT NULL,
  source_type   text NOT NULL DEFAULT 'product_brief',
  scope         text,
  tags          text[] NOT NULL DEFAULT '{}',
  active        boolean NOT NULL DEFAULT true,
  embedding     vector(1024),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS materials_active_idx
  ON materials (active);

CREATE INDEX IF NOT EXISTS materials_embedding_hnsw
  ON materials USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS brand_rules (
  id          text PRIMARY KEY,
  rule        jsonb NOT NULL,
  severity    text NOT NULL,
  active      boolean NOT NULL DEFAULT true,
  version     text NOT NULL DEFAULT '1',
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS platform_rules (
  platform    text NOT NULL,
  code        text NOT NULL,
  rule        jsonb NOT NULL,
  active      boolean NOT NULL DEFAULT true,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (platform, code)
);

CREATE TABLE IF NOT EXISTS runs (
  run_id        uuid PRIMARY KEY,
  status        text NOT NULL,
  request       jsonb NOT NULL,
  result        jsonb,
  error         jsonb,
  current_node  text,
  model         text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS events (
  id          bigserial PRIMARY KEY,
  run_id      uuid NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  seq         integer NOT NULL,
  event_type  text NOT NULL,
  node        text,
  payload     jsonb NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (run_id, seq)
);

CREATE INDEX IF NOT EXISTS events_run_seq_idx
  ON events (run_id, seq);
```

LangGraph checkpointer 会创建自己的表，不要手写或删除。

### 4.3 种子数据

`data/materials.json`：

```json
[
  {
    "id": "P01",
    "text": "产品支持字幕功能",
    "source": "产品说明 v1.2",
    "source_type": "product_brief",
    "scope": "中国大陆版",
    "tags": ["feature"],
    "active": true
  }
]
```

`data/rules.json`：

```json
{
  "brand": [
    {"id": "BR01", "severity": "block", "rule": {"type": "fact_source_required"}},
    {"id": "BR02", "severity": "block", "rule": {"type": "forbid_effect_claim_without_data"}},
    {"id": "BR03", "severity": "block", "rule": {"type": "banned_terms", "terms": ["最", "第一", "绝对"]}},
    {"id": "BR04", "severity": "warn", "rule": {"type": "subtitle_clearance"}}
  ],
  "platform": {
    "抖音": [
      {"code": "PLATFORM_DURATION", "severity": "block", "rule": {"max_sec": 180}}
    ]
  }
}
```

`scripts/seed_data.py` 负责：

- upsert 素材；
- 调用 DashScope embedding；
- 写入 `brand_rules` / `platform_rules`；
- 打印写入条数与向量维度；
- 重复执行幂等。

真实素材和 Mock 素材使用同一 schema；敏感数据使用独立数据库或外部存储，不提交。

---

## 5. 领域模型与校验

### 5.1 Pydantic 模型

`src/domain/models.py`：

```python
class Brief(BaseModel):
    topic: str
    audience: str | None = None
    platform: str | None = None
    target_duration_sec: int | None = None
    style: str | None = None

class Material(BaseModel):
    id: str
    text: str
    source: str
    source_type: str
    scope: str | None = None
    tags: list[str] = []

class Claim(BaseModel):
    claim_id: str
    text: str
    source_id: str
    approved: bool = True

class Shot(BaseModel):
    shot_id: str
    visual: str
    narration: str
    subtitle: str
    duration_sec: int
    visual_hold_sec: int = 0
    claim_ids: list[str] = []

class Script(BaseModel):
    script_id: str
    title: str
    shots: list[Shot]

class DurationReport(BaseModel):
    total_sec: int
    target_sec: int
    tolerance_sec: int
    passed: bool
    per_shot: list[dict]
    basis: str

class Violation(BaseModel):
    code: str
    severity: Literal["block", "warn", "info"]
    shot_id: str | None = None
    message: str
    evidence: dict = {}
    suggested_fix: str | None = None

class CheckReport(BaseModel):
    passed: bool
    violations: list[Violation]
```

`TaskStatus` 用 `StrEnum` 或 `Literal`：

```text
QUEUED
RUNNING
WAITING_INPUT
SUCCESS
NEEDS_INPUT
NO_MATERIAL
TOOL_ERROR
UNSATISFIABLE_DURATION
UNSATISFIABLE_RULES
MAX_STEPS
MODEL_ERROR
CANCELLED
INTERRUPTED
```

### 5.2 必填和默认值

- `topic` 必填。
- `platform`、`target_duration_sec` 是业务必填；缺失进入 `ask_user`，不能猜。
- `audience`、`style` 可默认，但必须写入 `risks`，例如 `DEFAULTED_AUDIENCE`。

### 5.3 输出不变量

`OutputValidator` 必须检查：

1. 每个 `shot.claim_ids` 都存在；
2. 每个 claim 的 `source_id` 都存在于 materials；
3. `total_sec == sum(shot.duration_sec)`；
4. `target ± tolerance` 判断一致；
5. block violation 存在时，`status != SUCCESS`；
6. 没有 source 的效果数据结论不能通过；
7. 素材 ID 不能被模型自由生成。

---

## 6. 工具实现

### 6.1 检索：BM25 + pgvector

`src/tools/retrieval.py` 暴露：

```python
async def search_materials(query: str, top_k: int = 5) -> SearchResult: ...
```

Embedding 适配：

```python
from langchain_openai import OpenAIEmbeddings

embedder = OpenAIEmbeddings(
    model="text-embedding-v3",
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    api_key=DASHSCOPE_API_KEY,
    dimensions=1024,
    check_embedding_ctx_length=False,
)
```

启动时实际调用 `embed_query("ping")`，断言返回长度为 1024；失败或维度不一致直接阻止 `/readyz`。

步骤：

1. **分词**：`jieba.lcut_for_search(query.lower())`，过滤空白和纯标点；英文保留 token。
2. **BM25**：
   - 启动时从 Postgres 读取 `active=true` 的 materials；
   - 用相同分词构建 `BM25Okapi`；
   - 取 Top 20，记录 `matched_terms`。
3. **向量**：
   - 用 DashScope 嵌入 query；
   - SQL：
     ```sql
     SELECT id, text, source,
            1 - (embedding <=> %s::vector) AS score
     FROM materials
     WHERE active = true AND embedding IS NOT NULL
     ORDER BY embedding <=> %s::vector
     LIMIT 20;
     ```
   - 相似度低于 `0.25` 的候选不进入融合。
4. **RRF 融合**：
   ```text
   score = sum(1 / (60 + rank))   # rank 从 1 开始
   ```
   BM25 和向量各占一份；按融合分取 Top K。
5. **返回**：
   ```json
   {
     "status": "OK",
     "query": "字幕",
     "results": [
       {"source_id": "P01", "text": "产品支持字幕功能", "score": 0.031, "bm25_rank": 1, "vector_rank": 1, "matched_terms": ["字幕"]}
     ],
     "debug": {"bm25_count": 1, "vector_count": 1, "fused_count": 1}
   }
   ```

状态：

- `OK`：至少一条结果；
- `EMPTY`：两路都没有结果；
- `LOW_SCORE`：只有低相关结果；
- `ERROR`：embedding/数据库失败，经过一次重试仍失败。

错误处理：

- DashScope 超时或 5xx：重试一次；
- 仍失败：返回 `ERROR`，Agent 最终进入 `TOOL_ERROR`；
- **不要**在 P0 静默降级为纯 BM25。降级必须显式记录风险并另行开关。

### 6.2 时长估算

`src/tools/duration.py`：

```python
def estimate_duration(script: Script, target_sec: int, tolerance_sec: int = 2) -> DurationReport: ...
```

公式：

```text
speech_sec = ceil(chinese_char_count / 4.5)
shot_sec   = max(speech_sec, visual_hold_sec) + transition_sec
total_sec  = sum(shot_sec)
passed     = abs(total_sec - target_sec) <= tolerance_sec
```

要求：

- 纯函数，不调用模型；
- 所有输入分镜都要保留 `shot_id`；
- 返回 `basis`，便于 Trace 复算；
- 测试必须覆盖 38/28/18 边界。

### 6.3 规则检查

`src/tools/rules.py`：

```python
def check_rules(script, brief, materials, brand_rules, platform_rules) -> CheckReport: ...
```

P0 规则：

| code | 级别 | 检查 |
| --- | --- | --- |
| `DURATION_MISSING` | block | 没有时长报告 |
| `DURATION_OVER` | block | 总时长超出 `target ± tolerance` |
| `UNSUPPORTED_CLAIM` | block | claim 没有来源，或 narration 不包含 claim 文本 |
| `EFFECT_WITHOUT_DATA` | block | 出现效果百分比/提升结论，但素材没有效果数据 |
| `BANNED_TERM` | block | 命中品牌禁语 |
| `SUBTITLE_TOO_LONG` | warn | 每秒字幕字符数过高 |
| `PLATFORM_LIMIT` | block | 平台时长限制 |
| `DEFAULTED_FIELD` | info | 使用了默认受众/风格 |

`UNSUPPORTED_CLAIM` 的 P0 实现：

- 规范化后检查 `claim.text` 是否为 `shot.narration` 的子串；
- 不满足则阻断，要求 revise；
- 不引入 LLM 事实判断，避免不可复现。

---

## 7. 模型适配

### 7.1 接口

`src/llm/base.py`：

```python
class DecisionModel(Protocol):
    async def decide(self, context: DecisionContext) -> ActionDecision: ...

class WriterModel(Protocol):
    async def write(self, context: WriterContext) -> ScriptDraft: ...
```

`ActionDecision`：

```json
{
  "action": "search_materials",
  "args": {"query": "字幕", "top_k": 5},
  "basis": "先找 P01 支撑字幕功能"
}
```

### 7.2 DeepSeek 实现

`src/llm/deepseek.py`：

```python
model = ChatOpenAI(
    model=settings.deepseek_model,
    base_url=settings.deepseek_base_url,
    api_key=settings.deepseek_api_key,
    temperature=0.2,
    timeout=60,
    max_retries=1,
)
decision_model = model.bind_tools(ACTION_TOOLS, tool_choice="auto")
```

动作工具：

- `search_materials`
- `draft_script`
- `estimate_duration`
- `check_rules`
- `revise_script`
- `ask_user`
- `finalize`
- `fail`

每个工具的 args schema 都带 `reason: str`；`reason` 就是 Trace 里的 `decision_basis`。实际执行由 LangGraph 节点完成，不使用 `ToolNode`。

模型输出校验：

1. 必须恰好一个 `tool_call`；
2. name 必须是允许动作；
3. 参数通过 Pydantic 校验；
4. 不合规则重试一次，仍失败返回 `MODEL_ERROR`。

DeepSeek 如果出现工具调用不稳定：

- 保留同一 `DecisionModel` 接口；
- 增加 `DECISION_MODE=json` 的适配器，用 `with_structured_output(ActionDecision)`；
- 不改图路由和状态。

### 7.3 Mock 实现

`src/llm/mock.py`：

```python
class MockDecisionModel:
    def __init__(self, decisions: list[ActionDecision]):
        self._queue = decisions
    async def decide(self, context):
        if not self._queue:
            raise ModelError("mock action queue exhausted")
        return self._queue.pop(0)
```

测试 API 使用 Mock，不让真实模型决定 CI 结果。

### 7.4 Prompt 和上下文投影

`prompts/decide.md` 只接收：

- Brief；
- 已有 materials 的 ID + 文本；
- claim 列表；
- draft 摘要；
- duration/check；
- 计数器和剩余预算；
- 明确约束：一次只选一个动作；不得编造素材；没有数据不能写效果结论。

`prompts/write.md` 只接收：

- Brief；
- approved claims；
- 约束；
- 上一版摘要（revise 时）；
- violations（revise 时）；
- `submit_script` 工具 schema。

不要把完整 Trace、全部历史消息、API Key 注入 Writer。

---

## 8. LangGraph 设计

### 8.1 State

`src/graph/state.py`：

```python
class AgentState(TypedDict, total=False):
    run_id: str
    brief: Brief
    materials: list[Material]
    claims: list[Claim]
    draft: Script | None
    duration: DurationReport | None
    check: CheckReport | None
    risks: list[dict]
    status: str
    current_node: str
    step: int
    tool_calls: int
    rewrite_count: int
    revision_count: int
    pending_question: dict | None
    pending_action: ActionDecision | None
    last_observation: dict | None
    result: dict | None
```

不把全量 events 放进 State。

### 8.2 节点

| 节点 | 输入 | 输出 | 关键约束 |
| --- | --- | --- | --- |
| `validate_input` | brief | brief/missing | 缺 platform/时长 → ask |
| `agent` | 投影状态 | pending_action | 唯一模型决策点；step+1 |
| `search_materials` | query | materials/claims | 最多一次内部重试 |
| `draft_script` | brief/claims | draft | 调用 Writer，记录 source |
| `estimate_duration` | draft | duration | 纯函数 |
| `check_rules` | draft/duration/rules | check/risks | 阻断违规 |
| `revise_script` | draft/violations | draft v2 | revision+1，最多 2 |
| `ask_user` | missing | pending_question | `interrupt()` |
| `finalize` | 全部状态 | result/status | 输出校验 |
| `fail` | reason | status/error | 终止 |

### 8.3 图和路由

```text
START
  → validate_input
      ├─ 缺信息 → ask_user → agent
      └─ 完整   → agent
  → agent
      ├─ search_materials  → agent
      ├─ draft_script      → agent
      ├─ estimate_duration → agent
      ├─ check_rules       → agent
      ├─ revise_script     → agent
      ├─ ask_user          → agent
      ├─ finalize          → END
      └─ fail              → END
```

`agent` 条件边只读 `pending_action.action`；不要用写死的 if-else 取代模型选择。

决策策略（写进 Prompt，并在测试中固定）：

| 观察 | 首选动作 | 保护条件 |
| --- | --- | --- |
| 缺必填字段 | `ask_user` | brief 仍缺少字段 |
| `search.status=EMPTY` 且 `rewrite_count<2` | `search_materials`，换查询词 | 不能重复同一个 query |
| `search.status=EMPTY` 且 `rewrite_count>=2` | `ask_user` 或 `fail(NO_MATERIAL)` | 不生成产品事实 |
| `search.status=ERROR` | `fail(TOOL_ERROR)` | 不伪造结果 |
| 无 draft | `draft_script` | 至少有可用 claim |
| 无 duration | `estimate_duration` | draft 已存在 |
| `duration.passed=false` 且 `revision_count<2` | `revise_script` | 保留 claim 引用 |
| `check` 有 block violation | `revise_script` | 最多 2 轮 |
| 全部通过 | `finalize` | 输出校验通过 |
| `step>=8` | `fail(MAX_STEPS)` | 由编排器强制 |

预算：

```text
MAX_STEPS = 8
MAX_TOOL_CALLS = 12
MAX_REVISIONS = 2
MAX_REWRITES = 2
```

`agent` 在调用模型前检查步数；达到上限路由 `fail(MAX_STEPS)`。`recursion_limit=24` 作为硬保险。

### 8.4 Checkpoint 和 interrupt

```python
# FastAPI lifespan 中进入并保持上下文；不要每次请求重新 setup
async with AsyncPostgresSaver.from_conn_string(settings.database_url) as checkpointer:
    await checkpointer.setup()
    app.state.graph = builder.compile(checkpointer=checkpointer)
    yield

config = {"configurable": {"thread_id": run_id}, "recursion_limit": 24}
```

`AsyncPostgresSaver` 使用独立的连接，不能和业务连接池共用同一个 `AsyncConnection`；连接池参数按 `langgraph-checkpoint-postgres` 官方示例配置。若 checkpointer 初始化失败，`/readyz` 返回 503，不允许开始接收 run。

`ask_user`：

```python
answer = interrupt({
    "type": "NEEDS_INPUT",
    "missing": ["target_duration_sec", "platform"],
    "question": "..."
})
# 恢复后执行：合并 answer 到 brief，返回新 brief
```

要求：

- `ask_user` 在 `interrupt` 之前不能写数据库或发不可逆副作用；
- 恢复使用 `Command(resume=answer)`；
- `POST /resume` 只接受 `WAITING_INPUT` 状态；
- 恢复后重新进入 `agent`，不重置已有素材。

### 8.5 事件

每个节点通过 `config["configurable"]["emit"]` 或 `get_stream_writer()` 发送：

```text
node.started
action.selected
tool.called
tool.result
node.completed
run.waiting_input
run.completed
run.failed
```

事件字段：

```json
{
  "run_id": "...",
  "seq": 4,
  "event_type": "tool.result",
  "node": "search_materials",
  "payload": {"status": "OK", "source_ids": ["P01"]},
  "created_at": "..."
}
```

不记录模型隐藏推理。

---

## 9. API 与运行管理

### 9.1 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/runs` | 创建运行，202 |
| GET | `/v1/runs/{id}` | 状态 + 当前节点 + 摘要 |
| GET | `/v1/runs/{id}/result` | 最终结果 |
| GET | `/v1/runs/{id}/events` | SSE |
| GET | `/v1/runs/{id}/trace` | Trace JSONL / JSON |
| POST | `/v1/runs/{id}/resume` | 补充信息并恢复 |
| POST | `/v1/runs/{id}/cancel` | 取消 |
| POST | `/v1/test/runs` | 测试场景，同步返回结果 |
| GET | `/healthz` | 进程健康 |
| GET | `/readyz` | DB、pgvector、配置检查 |

### 9.2 创建运行

```json
POST /v1/runs
{
  "brief": {
    "topic": "新手做短视频，字幕怎么加",
    "audience": "20-30 岁短视频新手",
    "platform": "抖音",
    "target_duration_sec": 30,
    "style": "轻快、口语化"
  }
}
```

返回：

```json
{
  "run_id": "uuid",
  "status": "QUEUED",
  "links": {
    "self": "/v1/runs/uuid",
    "events": "/v1/runs/uuid/events",
    "result": "/v1/runs/uuid/result"
  }
}
```

处理：

1. 写入 `runs(QUEUED)`；
2. `asyncio.create_task(run_manager.execute(run_id))`；
3. 立即返回 202；
4. 客户端轮询或订阅 SSE。

### 9.3 状态与恢复

- `GET /v1/runs/{id}` 返回 `status/current_node/step/missing_fields`。
- `WAITING_INPUT` 时，`POST /resume`：

```json
{"answer": {"platform": "抖音", "target_duration_sec": 30}}
```

- 非 `WAITING_INPUT` 返回 409。
- `/cancel` 设置取消标记；节点边界检查，已取消则结束为 `CANCELLED`。

### 9.4 SSE

```text
id: 12
event: tool.result
data: {"run_id":"...","node":"search_materials","status":"OK","source_ids":["P01"],"seq":12}
```

- 支持 `Last-Event-ID`；
- 事件从 Postgres 读，追加后推送；
- 15 秒心跳；
- 断线重连从 `last_event_id+1` 继续。

### 9.5 测试接口

`POST /v1/test/runs`：

```json
{
  "mode": "mock",
  "brief": {...},
  "scenario": {
    "decisions": [
      {"action": "search_materials", "args": {"query": "字幕"}, "basis": "找 P01"},
      {"action": "draft_script", "args": {}, "basis": "生成首稿"},
      {"action": "estimate_duration", "args": {}, "basis": "计算时长"}
    ],
    "tool_overrides": {
      "search_materials": [{"status": "OK", "results": [{"source_id": "P01", "text": "产品支持字幕功能"}]}],
      "duration_total_sec": [38, 28]
    }
  },
  "assertions": {
    "final_status": "SUCCESS",
    "must_include_source": "P01",
    "max_steps": 8
  }
}
```

测试模式同步执行到终态，返回：

```json
{
  "run_id": "...",
  "passed": true,
  "status": "SUCCESS",
  "trace": [...],
  "result": {...}
}
```

实现方式：

- 为测试构建独立 `FakeServices`；
- `MockDecisionModel` 按队列返回动作；
- 图用 `MemorySaver`，不写生产库；
- 测试 API 默认关闭，生产环境 `ENABLE_TEST_API=false`。

---

## 10. 运行、事件和 Trace

`RunManager`：

- 每个 run 一个 `asyncio.Lock`；
- 启动时把遗留 `RUNNING` 标记为 `INTERRUPTED`；
- 执行 `graph.astream`；
- 每次节点更新写 `runs.current_node` + `events`；
- 中断时写 `WAITING_INPUT` + pending question；
- 成功后写 `SUCCESS` + result；
- 失败写对应状态 + 具体错误。

`Trace` 字段：

```json
{
  "run_id": "...",
  "step": 4,
  "node": "estimate_duration",
  "action": "estimate_duration",
  "args": {"script_id": "draft_01"},
  "observation": {"total_sec": 38, "target_sec": 30},
  "next_action": "revise_script",
  "basis": "超出目标 8 秒",
  "latency_ms": 42
}
```

不得记录模型隐藏推理和 Key。

---

## 11. P0 验收场景

测试数据全部为合成数据，真实 Key 只用于可选 smoke。

### A. 正常生成

- 目标 30 秒，P01；
- 首稿 38 秒；
- 修正后 28 秒；
- 规则通过；
- 状态 `SUCCESS`；
- 结果包含 P01，且不包含“效率提升 80%”。

### B. 缺关键信息

- 缺 `target_duration_sec` 或 `platform`；
- 任务 `WAITING_INPUT`；
- `missing_fields` 明确；
- resume 后继续；
- 不猜测缺失值。

### C. 无素材

- 检索连续两次 `EMPTY`；
- 模型第二次后选择 `ask_user` 或 `fail`；
- 最终 `NO_MATERIAL`；
- 不生成带 P01 的脚本。

### D. 工具失败

- DashScope 超时/数据库异常；
- 工具内部重试一次仍失败；
- 最终 `TOOL_ERROR`；
- 不伪造搜索结果。

### E. 超时长且无法满足

- 目标 15 秒；
- 首稿 22 秒；
- 修正后 18 秒；
- 最终 `UNSATISFIABLE_DURATION`；
- 返回卡点、尝试次数、可选方案。

---

## 12. 测试计划

### 12.1 单元

- 时长公式：38、28、18；
- 容差判断；
- 规则：时长、禁用词、事实来源、效果数据；
- RRF：两路排序融合；
- 输出校验：缺 source 必须失败。

### 12.2 图测试

- 使用 `MemorySaver` + `FakeServices`；
- 正常、缺信息、无素材、工具失败、超时长；
- 断言节点序列、状态、步数、最终结果。

### 12.3 API 测试

- `httpx.AsyncClient(transport=ASGITransport(app=app))`；
- 202、状态查询、409 resume、SSE 首事件、result；
- 测试接口同步跑 5 个场景；
- Postgres 集成测试用 `TEST_DATABASE_URL`，无该变量则 skip。

### 12.4 命令

```bash
uv sync
uv run python scripts/migrate.py
uv run python scripts/seed_data.py
uv run pytest -q
uv run uvicorn src.api.app:app --reload
```

真实模型/embedding 冒烟：

```bash
uv run python scripts/smoke_model.py
```

该脚本只验证：

- DeepSeek 能返回一个工具调用；
- DashScope 返回 1024 维向量；
- 不写入验收断言，不替代 Mock 测试。

---

## 13. 开发顺序与完成标准

### M0 脚手架

产物：`pyproject.toml`、`src/config.py`、`src/api/app.py`、`.env.example`、`/healthz`。  
完成：`uv run uvicorn` 启动，配置缺失时错误清晰。

### M1 数据库

产物：`scripts/migrate.py`、DDL、`scripts/seed_data.py`、`/readyz`。  
完成：外部 Postgres 可连接，pgvector 存在，P01 写入并可查询。

### M2 领域与确定性工具

产物：models、duration、rules、validators、单元测试。  
完成：38/28/18 公式通过；无 source 的事实被阻断。

### M3 混合检索

产物：BM25、DashScope embedding、RRF、`search_materials`、单元测试。  
完成：P01 可被“字幕”召回；embedding 失败返回 `ERROR`。

### M4 模型适配

产物：`DecisionModel`、`WriterModel`、DeepSeek adapter、Mock、prompt。  
完成：`smoke_model.py` 成功；Mock 可固定输出动作。

### M5 LangGraph

产物：State、节点、路由、checkpointer、interrupt、5 个图集成测试。  
完成：5 个场景状态与 Trace 正确；`WAITING_INPUT` 可 resume。

### M6 API 与 SSE

产物：routes、RunManager、EventStore、Trace、测试接口。  
完成：`POST /runs` → SSE → result；resume 409 规则正确。

### M7 交付材料

产物：`tests/`、`examples/traces/`、`docs/03-evaluation.md`、`README` 更新。  
完成：`uv run pytest -q` 全绿；5 条 Trace 可从文件复现。

---

## 14. 交给实现 Agent 的边界说明

实现时请：

1. 先读 `README.md` 的架构约束和本文的接口契约。
2. 不要改 `web/`；P0 只做 API。
3. 不要改状态枚举、API 路径、表名和字段名，除非先提出变更。
4. 不要在日志、错误、Trace 中输出 Key、数据库密码或真实素材全文。
5. 遇到以下情况停止并报告，而不是自行绕过：
   - Postgres 没有 pgvector；
   - DashScope embedding 维度不是 1024；
   - DeepSeek 工具调用不可用；
   - 外部数据库无建表权限。
6. 先完成 M0–M3，再写图和 API；不要先做 UI。
7. 每个里程碑结束运行对应测试，不要等最后一次性调试。
8. Mock 测试必须通过；真实模型 smoke 失败时，记录失败，不得把 Mock 结果伪装成真实模型结果。

### Definition of Done

- `uv run pytest -q` 通过；
- 5 个验收场景都有测试和 Trace；
- `POST /v1/runs`、`/resume`、`/events`、`/result` 可用；
- `SUCCESS` 结果包含 `sources`、`estimated_duration_sec`、`risks`；
- 无来源事实不能通过；
- 无效果数据不出现“效率提升 80%”；
- 超时长或工具失败有明确状态和原因；
- README / docs / examples 与实际实现一致。
