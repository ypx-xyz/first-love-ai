#!/usr/bin/env python3
"""回复前置检查脚本（Check Script）。

给「陪伴者回复生成」用的**只读**前置检查器：读一遍聊天记录与素材目录，
输出一份 JSON 简报，让生成方（LLM / 规则引擎 / 定时任务）在动手之前
就拿到全部必要上下文，避免"凭印象写回复"。

它对应 `persona/reply_prompt.md` 第一步的 `{CHECK_SCRIPT}`，
报告以下关键字段：

| 字段 | 含义 |
|---|---|
| `now` / `now_phase` | 当前时间 / 时段（清晨…凌晨），写问候、提休息前必看 |
| `has_reply` | 是否存在**待回复**的用户消息；false 时生成方应直接结束任务 |
| `pending` | 待回复的用户消息列表（has_reply=true 时有值） |
| `context` | 最近若干条对话脉络（u=用户 / x=陪伴者，含文本与图片标记） |
| `recent_replies` | 最近 N 天陪伴者的回复清单（**防复读**依据） |
| `candidate_images` | 可发图片（已排除近 N 天发过的） |
| `img_roll` | 0~1 随机数；生成方按 `< 0.30` 决定是否带图（脚本只给骰子，不替它决策） |
| `learning` | 可选。若配置了学习进度文件则读取，否则为 null |

本脚本**只读不写**、不联网、不调用任何 LLM，可安全地高频运行。
仅依赖标准库。

用法：
    python check_reply.py                      # 读仓库内 messages.json
    python check_reply.py --data-dir /path     # 指定数据目录（同 CHAT_DATA_DIR）
    python check_reply.py --compact            # 单行 JSON，便于管道处理
"""
import argparse
import json
import os
import random
import re
import sys
from datetime import datetime, timedelta

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# ---- 可调参数 -------------------------------------------------------------
CONTEXT_N = 40          # context 中保留的最近消息条数
RECENT_DAYS = 3         # recent_replies 回看天数（防复读窗口）
IMAGE_ROLL = 0.30       # 带图概率阈值：img_roll < 该值才建议带图
IMAGE_DIR = os.path.join(APP_DIR, 'static', 'images')
LEARNING_FILE = os.path.join(APP_DIR, 'learning.json')  # 可选，不存在则为 null


def phase_of(hour):
    """把小时映射为时段名——写问候/提休息前先看它。"""
    if 5 <= hour < 8:
        return '清晨'
    if 8 <= hour < 11:
        return '上午'
    if 11 <= hour < 13:
        return '中午'
    if 13 <= hour < 17:
        return '下午'
    if 17 <= hour < 19:
        return '傍晚'
    if 19 <= hour < 23:
        return '晚上'
    if 23 <= hour <= 23:
        return '深夜'
    return '凌晨'  # 0-4 点


def parse_ts(value):
    """解析 messages.json 里的时间戳。支持 'YYYY-MM-DD HH:MM' 与 ISO 8601，
    解析失败返回 None（不抛异常——脏数据不应阻断检查）。"""
    if not value:
        return None
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).replace(tzinfo=None)
    except Exception:
        return None


def load_messages(data_dir):
    path = os.path.join(data_dir, 'messages.json')
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f'[check_reply] 读取 messages.json 失败：{e}', file=sys.stderr)
        return []


def render(msg):
    """把一条消息压成一行简报：'u: 文本' / 'x: 文本 [图:xxx.png]'。"""
    tag = 'u' if msg.get('sender') == 'user' else 'x'
    text = (msg.get('text') or '').replace('\n', ' ').strip()
    if len(text) > 200:
        text = text[:200] + '…'
    suffix = f" [图:{msg['image']}]" if msg.get('image') else ''
    return f'{tag}: {text}{suffix}'


def used_images(messages, since):
    """近 N 天陪伴者发过的图片文件名——同一张图近期不重复发。"""
    used = set()
    for m in messages:
        if m.get('sender') != 'assistant' or not m.get('image'):
            continue
        ts = parse_ts(m.get('timestamp'))
        if ts is None or ts >= since:
            used.add(os.path.basename(str(m['image'])))
    return used


def list_images():
    if not os.path.isdir(IMAGE_DIR):
        return []
    exts = ('.png', '.jpg', '.jpeg', '.gif', '.webp')
    return sorted(f for f in os.listdir(IMAGE_DIR) if f.lower().endswith(exts))


def load_learning():
    if not os.path.exists(LEARNING_FILE):
        return None
    try:
        with open(LEARNING_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def build_report(data_dir):
    now = datetime.now()
    messages = load_messages(data_dir)
    since = now - timedelta(days=RECENT_DAYS)

    # 待回复 = 末尾连续的用户消息（最后一条是用户发的，说明还没人接话）
    pending = []
    for m in reversed(messages):
        if m.get('sender') == 'user':
            pending.append(m)
        else:
            break
    pending.reverse()

    recent_replies = [render(m) for m in messages
                      if m.get('sender') == 'assistant']
    # 只留最近 N 天的回复（无时间戳的保守保留）
    recent = []
    for m in messages:
        if m.get('sender') != 'assistant':
            continue
        ts = parse_ts(m.get('timestamp'))
        if ts is None or ts >= since:
            recent.append(render(m))

    images = list_images()
    used = used_images(messages, since)
    candidates = [f for f in images if f not in used][:3]

    return {
        'now': now.strftime('%Y-%m-%d %H:%M'),
        'now_weekday': '周' + '一二三四五六日'[now.weekday()],
        'now_phase': phase_of(now.hour),
        'has_reply': bool(pending),
        'pending_count': len(pending),
        'pending': [render(m) for m in pending],
        'context': [render(m) for m in messages[-CONTEXT_N:]],
        'total_messages': len(messages),
        'recent_replies': recent[-20:],
        'recent_days': RECENT_DAYS,
        'candidate_images': candidates,
        'img_roll': round(random.random(), 3),
        'image_roll_threshold': IMAGE_ROLL,
        'learning': load_learning(),
    }


def main():
    ap = argparse.ArgumentParser(description='回复前置检查：输出对话上下文简报（JSON）')
    ap.add_argument('--data-dir', default=os.environ.get('CHAT_DATA_DIR', APP_DIR),
                    help='数据目录，内含 messages.json（默认取 CHAT_DATA_DIR 或仓库目录）')
    ap.add_argument('--compact', action='store_true', help='输出单行 JSON')
    args = ap.parse_args()

    report = build_report(args.data_dir)
    if args.compact:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    # 退出码恒为 0：是否回复由生成方根据 has_reply 判断，不靠退出码传递


if __name__ == '__main__':
    main()
