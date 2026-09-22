#!/usr/bin/env python3
"""回复质量审计 · 漂移诊断脚本（Drift Audit）。

**生成后**的质量审计：对比「最近 N 天陪伴者的虚拟回复」与「同期目标人格的真实语料」
的风格统计，输出 JSON 简报，供 LLM 采样解读是否存在漂移
（模板套话复用、语气偏离、长度分布异常、出戏）。

定位（重要）：
- 本脚本**不自行判定漂移**，只提供客观统计与对照，判定交给 LLM。
  避免脚本用固定阈值制造假阳性 / 假阴性，也避免「自证清白」。
- 对照基准必须是**真实人格语料**，不是历史虚拟回复。
  拿虚拟回复自己做基准会陷入回声室（echo chamber），越校准越失真。

与 `check_reply.py` 的分工：

| 脚本 | 时机 | 作用 |
|---|---|---|
| `check_reply.py` | 生成**前** | 给上下文简报（只读） |
| `drift_audit.py` | 生成**后** | 审计回复质量、发现漂移线索 |

报告字段：

| 字段 | 含义 |
|---|---|
| `now` / `window_days` | 审计时间 / 对照窗口天数 |
| `virtual_reply` | 虚拟回复的统计块（条数/均长/省略号率/表情率/高频收尾/高频开头） |
| `reference_corpus` | 同期真实语料的同结构统计块 |
| `template_drift_suspects` | 漂移线索：虚拟回复中出现 ≥2 次、真实语料同期为 0 的收尾/套话 |
| `virtual_samples` / `reference_samples` | 各取最近 12 条文本样本，供 LLM 对照真实语气 |

用法：
    python persona/drift_audit.py --reference persona/reference_corpus.json
    python persona/drift_audit.py --data-dir /path --days 7

仅依赖标准库、只读、不联网、不调用任何 LLM。
"""
import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DAYS = 7
ASSISTANT_SENDER = 'assistant'

# 收尾 / 套话模式（示例；实际使用时按你的语料库定制）
# 每一类代表一种「容易退化成模板」的表达，用于统计其复用频次
CLOSING_PATTERNS = {
    '回家/陪家人': ['回家', '陪陪家里人', '陪家里人', '陪家人'],
    '别想那么多': ['别想那么多', '别想太多', '想那么多'],
    '照顾好自己': ['照顾好自己', '照顾好'],
    '好好休息/快去睡': ['好好休息', '早点休息', '快去睡', '快去休息'],
    '吃饭/喝水': ['吃饭去吧', '吃点饭', '吃饭'],
    '谢什么呀': ['谢什么'],
    '往事意象': ['往事', '从前', '那年'],
    '好吧/行吧/算了/不说了': ['好吧', '行吧', '算了', '就这样吧', '不说了'],
    '别光顾/别老': ['别光顾', '别老', '别光'],
    '加油/坚持/你真棒': ['加油', '坚持', '你真棒', '你可以的'],
}

EMOJI_RE = re.compile(r'\[[^\]]{1,4}\]|[\U0001F300-\U0001FAFF\u2600-\u27BF]')


def parse_ts(value):
    """解析时间戳。支持 'YYYY-MM-DD HH:MM'、'YYYY-MM-DD HH:MM:SS' 与 ISO 8601。"""
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(str(value).strip(), fmt)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(str(value).strip().replace('Z', '+00:00')).replace(tzinfo=None)
    except Exception:
        return None


def within_days(value, days, now):
    """是否在窗口内；解析失败时保守纳入（宁可多统计，不漏）。"""
    ts = parse_ts(value)
    if ts is None:
        return True
    return ts >= now - timedelta(days=days)


def load_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f'[drift_audit] 读取 {path} 失败：{e}', file=sys.stderr)
        return None


def stat_block(texts):
    """对一组文本产出风格统计块。"""
    n = len(texts)
    if n == 0:
        return {'count': 0}
    lens = [len(t) for t in texts]
    ellipsis = sum(1 for t in texts if '……' in t or '...' in t)
    emoji = sum(1 for t in texts if EMOJI_RE.search(t))
    closings = Counter()
    for t in texts:
        for name, pats in CLOSING_PATTERNS.items():
            if any(p in t for p in pats):
                closings[name] += 1
    openers = Counter(t[:6] for t in texts if len(t) >= 4)
    return {
        'count': n,
        'avg_len': round(sum(lens) / n, 1),
        'ellipsis_rate': round(ellipsis / n, 2),
        'emoji_rate': round(emoji / n, 2),
        'top_closings': closings.most_common(8),
        'top_openers': openers.most_common(5),
    }


def build_report(data_dir, reference_path, days):
    now = datetime.now()
    msgs = load_json(os.path.join(data_dir, 'messages.json')) or []
    reference = load_json(reference_path) or []
    if not isinstance(reference, list):
        reference = []

    virtual = [m.get('text', '') for m in msgs
               if m.get('sender') == ASSISTANT_SENDER and m.get('text')
               and within_days(m.get('timestamp', ''), days, now)]
    real = [str(m.get('text', '')).strip() for m in reference
            if isinstance(m, dict) and m.get('text')
            and within_days(m.get('time', ''), days, now)]

    report = {
        'now': now.strftime('%Y-%m-%d %H:%M'),
        'window_days': days,
        'virtual_reply': stat_block(virtual),
        'reference_corpus': stat_block(real),
    }

    # 漂移线索：虚拟回复中出现频次高、但真实语料同期几乎不出现的收尾/套话
    vir_close = dict(report['virtual_reply'].get('top_closings', []))
    ref_close = dict(report['reference_corpus'].get('top_closings', []))
    report['template_drift_suspects'] = [
        {'phrase': k, 'virtual': v, 'reference': ref_close.get(k, 0)}
        for k, v in vir_close.items() if v >= 2 and ref_close.get(k, 0) == 0
    ]

    # 采样：各取最近 12 条，供 LLM 对照真实语气
    report['virtual_samples'] = [t[:100] for t in virtual[-12:]]
    report['reference_samples'] = [t[:100] for t in real[-12:]]
    return report


def main():
    ap = argparse.ArgumentParser(description='回复质量审计 · 漂移诊断')
    ap.add_argument('--data-dir', default=os.environ.get('CHAT_DATA_DIR', APP_DIR),
                    help='数据目录，内含 messages.json（默认取 CHAT_DATA_DIR 或仓库目录）')
    ap.add_argument('--reference', default=None,
                    help='真实语料 JSON（默认 <data-dir>/reference_corpus.json）')
    ap.add_argument('--days', type=int, default=DEFAULT_DAYS, help='对照窗口天数（默认 7）')
    ap.add_argument('--compact', action='store_true', help='单行 JSON，便于管道处理')
    args = ap.parse_args()

    reference = args.reference or os.path.join(args.data_dir, 'reference_corpus.json')
    report = build_report(args.data_dir, reference, args.days)

    if args.compact:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
