# 漂移诊断机制（Drift Audit）

配套脚本：[`drift_audit.py`](drift_audit.py)

## 问题：人格模拟为什么会「漂移」

陪伴者长期运行后，回复会**渐进地**偏离目标人格，典型表现：

- **模板套话复用**：反复用同一句收尾关心（「照顾好自己」「早点休息」「别想那么多」）
- **语气偏离**：越来越客气、越来越「客服腔」，偏离原人格的说话方式
- **长度分布异常**：回复越来越长（说教）或越来越短（敷衍）
- **出戏漂移**：开始探讨「我是不是 AI / 会不会消失」之类的元问题

这类退化不剧烈，但会持续累积，**靠肉眼很难及时发现**——需要可量化的对照。

## 在链路中的位置

```
check_reply.py（生成前：上下文简报）
        │
        ▼
   回复生成（reply_prompt.md）
        │
        ▼
   drift_audit.py（生成后：质量审计）
        │
        ▼
   LLM 解读 → 回写 reply_prompt 的约束
```

`check_reply.py` 管「生成前拿到什么」，`drift_audit.py` 管「生成后像不像」。

## 核心设计：以真实语料为基准的客观对照

三条关键原则：

1. **不自行判定漂移**
   脚本只产出客观统计，**不做主观的「是否漂移」判断**。判定交给 LLM 采样解读。
   这样避免脚本用固定阈值制造假阳性 / 假阴性，也避免「自证清白」。

2. **基准是真实人格语料，不是历史虚拟回复**
   若拿虚拟回复自己当基准，会陷入**回声室（echo chamber）**——越校准越失真。
   唯一可靠的基准，是目标人格的**真实语料**。对照组错了，指标全错。

3. **只看相对差异**
   有意义的不是绝对值（如「省略号率 0.8」），而是
   **虚拟回复 vs 同期真实语料** 的差异。差异显著才说明偏离。

## 实现流程（`drift_audit.py`）

1. 读取最近 N 天（默认 7）的**虚拟回复**（`messages.json` 中 `sender=assistant`），
   与目标人格**同期真实语料**（`reference_corpus.json`）
2. 各产出一个统计块：
   - `count` 条数、`avg_len` 平均长度
   - `ellipsis_rate` 省略号率、`emoji_rate` 表情率
   - `top_closings` 高频收尾 / 套话、`top_openers` 高频开头
3. 计算**漂移线索**：虚拟回复中出现 ≥2 次、但真实语料同期为 0 的收尾 / 套话
4. 各取最近 12 条文本样本（`virtual_samples` / `reference_samples`），供 LLM 对照真实语气
5. 输出 JSON 简报到 stdout

## 如何解读输出

| 观察点 | 含义与动作 |
|---|---|
| `template_drift_suspects` 非空 | 优先排查这些套话是否被过度复用，考虑在回复 prompt 中加防复读约束 |
| `virtual_reply` vs `reference_corpus` 的 `ellipsis_rate` / `avg_len` / `emoji_rate` | 差异显著 → 语气节奏偏离，需校准 |
| `virtual_samples` vs `reference_samples` | LLM 直接对照阅读，判断「像不像」 |
| 结论 | 落回回复 prompt 的约束项，更新规则 |

## 与回复 prompt 的闭环

```
回复生成（reply_prompt）──产出──> 聊天记录
      ▲                              │
      │                              ▼
   约束更新 <──解读── 漂移诊断（drift_audit）
```

即：**drift_audit 定期扫描 → LLM 解读发现漂移 → 回写 reply_prompt 的约束 → 回复质量回升**。

这正是 [`reply_prompt.md`](reply_prompt.md) 中「约束 A / B / C / D」这类条款的由来——
每一条约束，都是对一次真实漂移的修正。

## 运行

```bash
python persona/drift_audit.py --reference persona/reference_corpus.json
python persona/drift_audit.py --data-dir /path --days 7
```

参数：`--data-dir`（同 `CHAT_DATA_DIR`）、`--reference`（默认 `<data-dir>/reference_corpus.json`）、
`--days`（默认 7）、`--compact`（单行 JSON）。

参考语料 `reference_corpus.json` 的结构为 `[{"text": "...", "time": "YYYY-MM-DD HH:MM"}, ...]`。

## 隐私

脚本只做统计与采样，**不输出可识别个人的信息**；本仓库不含任何真实语料。
实际使用时，请自行评估参考语料的合规性与授权。
