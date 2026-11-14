# 合成 fixture 说明

本目录的所有文件均为离线合成数据，不包含真实用户、真实组织、真实域名或真实业务数据。

## 文件

| 文件 | 内容 |
| --- | --- |
| `experiment.yaml` | 实验定义、指标、护栏、分层与声明式决策规则 |
| `assignments.csv` | 1600 条分配记录，含平台与区域分层字段 |
| `observations.csv` | 1600 条曝光观测，含激活、会话深度、崩溃字段 |

生成方式：固定种子 `20260301` 的本地脚本离线生成；脚本不随运行期使用，项目运行时只读取 CSV/YAML。

## 数据字典

### assignments.csv

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `unit_id` | string | `unit-0001` 至 `unit-1600` |
| `experiment_id` | string | 固定为 `exp-onboarding-card-2026` |
| `variant` | string | `control` / `treatment` |
| `assigned_at` | ISO-8601 | 2026-03-01 至 2026-03-11 的 UTC 时间 |
| `platform` | string | `desktop` / `mobile` / `legacy` |
| `region` | string | `north` / `south` / `west` |

### observations.csv

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `unit_id` | string | 与分配一一对应 |
| `experiment_id` | string | 固定实验 id |
| `variant` | string | 与分配一致 |
| `observed_at` | ISO-8601 | 分配后 1–72 小时内 |
| `activated` | 0/1 | 主指标：激活 |
| `session_depth` | float | 次级指标：会话深度 |
| `crashed` | 0/1 | 护栏：崩溃 |

## 预设信号

数据刻意包含以下信号：

1. 分流精确平衡：对照组与实验组各 800 个唯一 unit。
2. 主指标显著正向：激活率 25.0% → 30.0%，绝对提升 5.0pp。
3. 护栏临界：崩溃率 1.0% → 1.75%，劣化 0.75pp，低于 1.0pp 阈值但超过阈值一半。
4. `legacy` 平台方向反转：激活率 33.3% → 10.0%，样本各 120，显著为负。
5. 总体同向分层占比不足 75%，因此主指标稳健性为 false。
6. 没有重复曝光、互斥冲突或结构性数据质量问题。

## 期望结果

运行：

```bash
python -m app demo --out out --db data/ledger.sqlite3
```

得到：

- 决策：`继续迭代 / iterate`
- 命中规则：`iterate-evidence`
- 检查：分层 `fail`，护栏与主指标 `warn`，其余 `pass`
- finding：
  - `stratification.reversal`（high）
  - `guardrail.watch.crash_rate`（medium）
  - `metric.fragile`（medium）

## 为什么刻意不返回 ship

样例的主指标总体为正且显著，但某足量分层显著反方向，护栏也接近阈值。把这些冲突同时展示出来，才能验证工具不是“p<0.05 就上线”的单阈值工具，而是把分流健康、质量、护栏与分层稳健性一起纳入可解释的决策。
