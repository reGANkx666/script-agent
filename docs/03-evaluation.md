# 03 · 测试、实际结果与失败边界

## 1. 运行方式

```bash
cd scriptAgent
python -m pytest -q
python scripts/reproduce_examples.py
```

`reproduce_examples.py` 会重新生成 6 组场景的：

```text
examples/results/<name>.json
examples/traces/<name>.jsonl
```

当前结果：

```text
42 passed
OK: 6 scenarios reproduced
```

原始测试输出：[examples/test-results.txt](../examples/test-results.txt)。

## 2. 六组输入、实际结果与 Trace

每组都有“输入场景 → 实际输出 → 运行记录”三份证据。

| # | 场景 | 输入 | 实际结果 | Trace | 关键实际值 |
|---|---|---|---|---|---|
| A | 正常生成 | [a_normal_generation.json](../examples/scenarios/a_normal_generation.json) | [result](../examples/results/a_normal_generation.json) | [trace](../examples/traces/a_normal_generation.jsonl) | `SUCCESS`，28s，P01，7 步 / 6 次工具 |
| B | 缺关键信息 | [b_missing_information.json](../examples/scenarios/b_missing_information.json) | [result](../examples/results/b_missing_information.json) | [trace](../examples/traces/b_missing_information.jsonl) | `WAITING_INPUT`，missing_fields 明确，result=null |
| C | 无素材 | [c_no_material.json](../examples/scenarios/c_no_material.json) | [result](../examples/results/c_no_material.json) | [trace](../examples/traces/c_no_material.jsonl) | `NO_MATERIAL`，两次 EMPTY，3 步 / 2 次工具 |
| D | 工具失败 | [d_tool_error.json](../examples/scenarios/d_tool_error.json) | [result](../examples/results/d_tool_error.json) | [trace](../examples/traces/d_tool_error.jsonl) | `TOOL_ERROR`，dashscope timeout，2 步 / 1 次工具 |
| E | 超时长且无法满足 | [e_unsatisfiable_duration.json](../examples/scenarios/e_unsatisfiable_duration.json) | [result](../examples/results/e_unsatisfiable_duration.json) | [trace](../examples/traces/e_unsatisfiable_duration.jsonl) | `UNSATISFIABLE_DURATION`，22→20→18，8 步 / 2 次修订 |
| F | 预算耗尽 | [f_max_steps.json](../examples/scenarios/f_max_steps.json) | [result](../examples/results/f_max_steps.json) | [trace](../examples/traces/f_max_steps.jsonl) | `MAX_STEPS`，step=4 / tool_calls=4 |

A、C、E 已覆盖“正常、资料不足、工具结果改变后续动作”三类路径；B、D、F 覆盖暂停、失败和预算边界。

## 3. 两条实际执行路径

### 路径一：正常生成中的时长修正

来自 A 的实际 Trace：

| 步 | 动作 | 工具参数 | 返回摘要 | 下一步 | 依据 |
|---:|---|---|---|---|---|
| 1 | `search_materials` | `query=字幕, top_k=5` | `P01`，status=OK | `draft_script` | 先找字幕功能素材 |
| 2 | `draft_script` | 无 | draft_01，claim=`C-P01` | `estimate_duration` | 生成首稿 |
| 3 | `estimate_duration` | 无 | total=38s，target=30s，passed=false | `revise_script` | 超出目标 8 秒 |
| 4 | `revise_script` | 无 | draft_02，revision=1 | `estimate_duration` | 压缩到目标附近 |
| 5 | `estimate_duration` | 无 | total=28s，passed=true | `check_rules` | 时长通过 |
| 6 | `check_rules` | 无 | passed=true，block_count=0 | `finalize` | 时长和规则均通过 |
| 7 | `finalize` | 无 | `SUCCESS`，sources=[P01] | END | 输出可交付 |

这条路径能直接回答审核要点：

- 首稿 38 秒 → 估算失败 → 模型选择修订；
- 修订后 28 秒 → 重新估算；
- 最终 P01、分镜、28 秒和来源一一对应。

### 路径二：超时长修订耗尽后明确失败

来自 E 的实际 Trace：

| 步 | 动作 | 观察 | 下一步 |
|---:|---|---|---|
| 1 | `search_materials` | P01 命中 | `draft_script` |
| 2 | `draft_script` | draft_01 | `estimate_duration` |
| 3 | `estimate_duration` | 22s / target 15s，passed=false | `revise_script` |
| 4 | `revise_script` | draft_02，revision=1 | `estimate_duration` |
| 5 | `estimate_duration` | 20s，仍失败 | `revise_script` |
| 6 | `revise_script` | draft_03，revision=2 | `estimate_duration` |
| 7 | `estimate_duration` | 18s，仍失败 | `fail` |
| 8 | `fail` | `UNSATISFIABLE_DURATION` | END |

这条路径证明系统没有把“压缩过一次”当成成功，而是在修订预算耗尽后给出明确终态。

### 路径三：无素材时改写查询

C 的实际 Trace：

```text
search_materials("字幕") → EMPTY
search_materials("重写字幕") → EMPTY
→ fail(NO_MATERIAL)
```

模型没有继续编造 P01，而是在两次空召回后停止。

## 4. 测试预期 vs 实际结果

| 场景 | 预期 | 实际 | 断言 |
|---|---|---|---|
| A | `SUCCESS`，P01，28s，无效果 80% | `SUCCESS`，P01，28s | 全部通过 |
| B | `WAITING_INPUT`，不生成草案 | `WAITING_INPUT`，result=null | 全部通过 |
| C | `NO_MATERIAL`，无 P01 | `NO_MATERIAL`，script=null | 全部通过 |
| D | `TOOL_ERROR`，不伪造结果 | `TOOL_ERROR`，result=null | 全部通过 |
| E | `UNSATISFIABLE_DURATION`，保留 18s | `UNSATISFIABLE_DURATION`，18s，P01 | 全部通过 |
| F | `MAX_STEPS` | `MAX_STEPS`，step=4 | 全部通过 |

## 5. Trace 字段

每个 JSONL 行至少包含：

```text
seq / step / node / event_type
action / args / basis
observation
next_action
```

示例：

```json
{
  "seq": 23,
  "step": 3,
  "node": "estimate_duration",
  "event_type": "tool.result",
  "action": "estimate_duration",
  "observation": {"total_sec": 38, "target_sec": 30, "passed": false},
  "next_action": "revise_script"
}
```

`estimate_duration` 和 `revise_script` 的 `args` 为空是设计结果：

- 它们没有模型可传的 `target_sec` 或 `script` 参数；
- 输入来自当前 state 的 `draft`、`brief.target_duration_sec`、`duration` 和 `violations`；
- `tool.called.observation` 记录 `script_id`、`target_sec`、`revision`、`violation_codes`。

这样模型不能通过工具参数绕过目标时长或篡改待检查脚本。

不包含：

- 模型隐藏推理；
- API Key / 数据库密码；
- 全量 Prompt。

## 6. 失败与限制

### 已覆盖的失败

- 缺字段 → `WAITING_INPUT`；
- 空检索 → `NO_MATERIAL`；
- embedding / 数据库失败 → `TOOL_ERROR`；
- 时长修订耗尽 → `UNSATISFIABLE_DURATION`；
- 步数 / 工具预算 → `MAX_STEPS`；
- reranker 失败 → 降级 RRF + `RERANK_DEGRADED`（知识库链路）。

### 尚未解决

- 扫描版 PDF 没有 OCR；
- 入库任务是进程内 `asyncio`，重启不会自动恢复；
- 原文件存本地 `uploads/`，没有对象存储；
- 81 条 seed 素材没有包装成可展示的 seed document；
- reranker 在当前 golden set 上未表现出正增益；
- 没做多实例、鉴权和生产级并发。

## 7. 真实环境证据

真实依赖联调见 [README 第 9 节](../README.md#9-真实环境联调记录)：

- PostgreSQL 18 + pgvector 0.8.7；
- 六种格式上传 READY；
- BM25 + pgvector + RRF + `gte-rerank-v2`；
- 真实 API 生成 `SUCCESS`；
- [real_normal_30s.json](../examples/results/real_normal_30s.json)；
- [real_knowledge_30s.json](../examples/results/real_knowledge_30s.json)。

## 8. 复现边界

`examples/` 里的 A–F 是 **Mock 场景**，用于验证流程、状态和预算；  
真实 API 的 `real_normal_30s` / `real_knowledge_30s` 才代表真实模型与真实知识库结果。

两类证据不能混写：

```text
Mock  → 证明流程和断言
Real  → 证明外部依赖可用
```
