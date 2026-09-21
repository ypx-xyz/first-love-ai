#!/usr/bin/env python3
"""check_reply.py 冒烟测试。

不依赖第三方库、不占端口，用子进程跑真实的 check_reply.py，覆盖：
1. 空数据目录          → has_reply=False、total=0
2. 末条是陪伴者回复    → has_reply=False（无新消息，生成方应结束任务）
3. 末条是用户消息      → has_reply=True、pending 正确
4. 简报字段完整性与取值合法性
5. --compact 与默认输出都能被解析为 JSON

用法：
    python tests/smoke_test.py     # 退出码 0 = 全部通过
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'check_reply.py')
PHASES = {'清晨', '上午', '中午', '下午', '傍晚', '晚上', '深夜', '凌晨'}
REQUIRED = ['now', 'now_phase', 'has_reply', 'pending', 'context',
            'recent_replies', 'candidate_images', 'img_roll', 'learning']

failures = []


def check(name, cond, detail=''):
    if cond:
        print(f'  ✓ {name}')
    else:
        print(f'  ✗ {name} {detail}')
        failures.append(name)


def run_check(data_dir, compact=True):
    cmd = [sys.executable, SCRIPT, '--data-dir', data_dir]
    if compact:
        cmd.append('--compact')
    # encoding 必须显式指定 utf-8：Windows 默认用 GBK 解码子进程输出会乱码/报错
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=60)
    if r.returncode != 0:
        raise AssertionError(f'check_reply.py 退出码 {r.returncode}: {r.stderr[:400]}')
    return json.loads(r.stdout)


def write_messages(data_dir, messages):
    with open(os.path.join(data_dir, 'messages.json'), 'w', encoding='utf-8') as f:
        json.dump(messages, f, ensure_ascii=False)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        print('[1] 空数据目录')
        rep = run_check(tmp)
        check('has_reply 为 False', rep['has_reply'] is False)
        check('total_messages 为 0', rep['total_messages'] == 0)
        check('context 为空列表', rep['context'] == [])

        print('[2] 字段完整性')
        for k in REQUIRED:
            check(f'字段 {k} 存在', k in rep, f'实际键：{sorted(rep)}')
        check('now_phase 取值合法', rep['now_phase'] in PHASES, f"实际：{rep['now_phase']}")
        check('img_roll 在 0~1 之间', 0.0 <= rep['img_roll'] <= 1.0)

        print('[3] 末条为陪伴者回复 → 无需回复')
        write_messages(tmp, [
            {'id': 'm1', 'sender': 'user', 'text': '在吗', 'timestamp': '2026-01-01 10:00'},
            {'id': 'm2', 'sender': 'assistant', 'text': '在', 'timestamp': '2026-01-01 10:01'},
        ])
        rep = run_check(tmp)
        check('has_reply 为 False', rep['has_reply'] is False)
        check('pending 为空', rep['pending'] == [])
        check('context 已渲染为 u:/x: 前缀',
              rep['context'][0].startswith('u: ') and rep['context'][1].startswith('x: '))

        print('[4] 末条为用户消息 → 需要回复')
        write_messages(tmp, [
            {'id': 'm1', 'sender': 'assistant', 'text': '在', 'timestamp': '2026-01-01 10:01'},
            {'id': 'm2', 'sender': 'user', 'text': '今天过得怎么样', 'timestamp': '2026-01-01 10:02'},
            {'id': 'm3', 'sender': 'user', 'text': '还在吗', 'timestamp': '2026-01-01 10:03'},
        ])
        rep = run_check(tmp)
        check('has_reply 为 True', rep['has_reply'] is True)
        check('pending_count 为 2', rep['pending_count'] == 2, f"实际：{rep['pending_count']}")

        print('[5] 非 compact 输出同样可解析')
        rep2 = run_check(tmp, compact=False)
        check('两种输出等价', rep2['has_reply'] == rep['has_reply'])

        print('[6] 脏数据不炸')
        with open(os.path.join(tmp, 'messages.json'), 'w', encoding='utf-8') as f:
            f.write('{ 这不是合法 JSON')
        r = subprocess.run([sys.executable, SCRIPT, '--data-dir', tmp, '--compact'],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=60)
        check('坏 JSON 不导致崩溃', r.returncode == 0, f'退出码 {r.returncode}')
        check('坏 JSON 仍输出可用简报', json.loads(r.stdout)['has_reply'] is False)

    print()
    if failures:
        print(f'FAILED: {len(failures)} 项未通过 -> {failures}')
        return 1
    print('ALL PASSED')
    return 0


if __name__ == '__main__':
    sys.exit(main())
