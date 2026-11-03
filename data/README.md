# 数据说明

## materials.json

当前共 81 条：

- `P01`：产品事实，来源为 `产品说明 v1.2`；
- `K01`–`K80`：短视频字幕、脚本、拍摄、发布与合规的通用编辑规范。

`K` 系列来源统一标记为：

```text
Synthetic short-video knowledge base v1.0
source_type = synthetic_kb
```

它们是为 P0 检索、事实引用和演示构造的**合成知识库**，不是小影科技官方产品文档，也不是平台官方规则原文。不要把它们当作真实产品能力或真实平台政策对外宣传。

`K` 系列分为 8 类，每类 10 条：

| tag | 内容 |
| --- | --- |
| `subtitle` | 字幕可读性、断句、字号、对比度 |
| `structure` | 开头、正文、结尾和分镜结构 |
| `hook` | 开头三秒、问题切入、承诺一致性 |
| `delivery` | 口播语速、停顿、短句 |
| `production` | 竖屏构图、录屏、剪辑、音画同步 |
| `compliance` | 虚假承诺、授权、价格、个人信息 |
| `rights` | 音乐、影视、字体、图片和素材授权 |
| `publishing` | 发布前检查、复盘和版本对比 |

## rules.json

规则只保留当前规则引擎能够执行的类型：

- `fact_source_required`
- `forbid_effect_claim_without_data`
- `banned_terms`
- `subtitle_clearance`（warn）
- 平台时长上限

没有为了凑数量加入“只存不执行”的规则。若要把平台官方规则加入，应先核对原文，再在 `src/tools/rules.py` 增加对应检查。

## knowledge_eval.json

固定 8 条 RAG 检索评估 query，每条包含 `query` 和 `expected` source_id。  
`GET /v1/knowledge/evaluation` 会用同一批 query 分别跑 RRF-only 和 rerank，计算 Recall@K、MRR、Precision@K。它不是模型质量评测，只衡量检索排序。

## 替换为真实资料

如果拿到真实产品文档：

1. 保留 `P01` 或替换为真实事实；
2. 删除不需要的 `K` 条目；
3. 把 `source` 改为真实文档名和版本；
4. 重新执行 `python scripts/seed_data.py`；
5. 重启 API，让 BM25 索引重建。

真实资料进入仓库前，应确认可公开使用，不包含用户隐私或未授权内容。
