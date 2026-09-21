#!/usr/bin/env python3
"""聊天服务端到端冒烟测试（仅标准库）。

在一个随机空闲端口上真实拉起 app.py（数据目录指向临时目录，不污染仓库），
然后走一遍完整链路：

    用户发消息 → 陪伴者注入回复 → 全量拉取 → 增量拉取 → 统计 → 备份 → 图片

用法：
    python tests/e2e_test.py       # 退出码 0 = 全部通过
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
failures = []


def check(name, cond, detail=''):
    if cond:
        print(f'  ✓ {name}')
    else:
        print(f'  ✗ {name} {detail}')
        failures.append(name)


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def req(url, method='GET', payload=None):
    """返回 (status_code, parsed_body)。HTTP 错误不抛异常，返回码交给用例判断。"""
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode('utf-8'))
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode('utf-8'))
        except Exception:
            return e.code, None


def wait_ready(base, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            code, _ = req(base + '/api/stats')
            if code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def main():
    port = free_port()
    base = f'http://127.0.0.1:{port}'

    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, PORT=str(port), CHAT_DATA_DIR=tmp)
        proc = subprocess.Popen([sys.executable, os.path.join(ROOT, 'app.py')],
                                cwd=ROOT, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding='utf-8', errors='replace')
        try:
            if not wait_ready(base):
                out = proc.stdout.read() if proc.stdout else ''
                print('服务未能在 25 秒内就绪，输出：\n', out[:1000])
                return 1

            print(f'[服务已就绪] {base}')

            print('[1] 初始状态')
            code, body = req(base + '/api/stats')
            check('GET /api/stats 返回 200', code == 200, f'实际 {code}')
            check('初始消息数为 0', body.get('total') == 0, f"实际 {body.get('total')}")

            print('[2] 用户发送消息')
            code, user_msg = req(base + '/api/messages', 'POST', {'text': '你好'})
            check('POST /api/messages 返回 200', code == 200, f'实际 {code}')
            check('发送者为 user', user_msg.get('sender') == 'user')
            check('返回了消息 id', bool(user_msg.get('id')))

            print('[3] 空文本应被拒绝')
            code, _ = req(base + '/api/messages', 'POST', {'text': '   '})
            check('空文本返回 400', code == 400, f'实际 {code}')

            print('[4] 注入陪伴者回复')
            code, a1 = req(base + '/api/messages/assistant', 'POST', {'text': '在'})
            check('POST /api/messages/assistant 返回 200', code == 200, f'实际 {code}')
            check('发送者为 assistant', a1.get('sender') == 'assistant')
            code, a2 = req(base + '/api/messages/assistant', 'POST',
                           {'text': '给你看张图', 'image': 'sample.png'})
            check('带图片的回复保留 image 字段', a2.get('image') == 'sample.png')

            print('[5] 全量与增量拉取')
            code, msgs = req(base + '/api/messages')
            check('全量返回 3 条', isinstance(msgs, list) and len(msgs) == 3, f'实际 {len(msgs)}')
            code, inc = req(base + '/api/messages?since_id=' + urllib.parse.quote(str(user_msg['id'])))
            check('增量只返回 2 条', isinstance(inc, list) and len(inc) == 2, f'实际 {inc}')
            code, reset = req(base + '/api/messages?since_id=' + urllib.parse.quote('不存在的id'))
            check('since_id 失效时返回 needReset', isinstance(reset, dict) and reset.get('needReset') is True)

            print('[6] 统计与备份')
            code, st = req(base + '/api/stats')
            check('统计：user=1', st.get('user') == 1, f"实际 {st.get('user')}")
            check('统计：assistant=2', st.get('assistant') == 2, f"实际 {st.get('assistant')}")
            check('统计：total=3', st.get('total') == 3)
            code, bk = req(base + '/api/backups')
            check('写前已产生备份', isinstance(bk.get('backups'), list) and len(bk['backups']) > 0,
                  f"实际 {bk}")

            print('[7] 图片服务')
            code, _ = req(base + '/api/images/sample.png')
            check('GET /api/images/sample.png 返回 200', code == 200, f'实际 {code}')

            print('[8] 数据落盘')
            path = os.path.join(tmp, 'messages.json')
            check('临时数据目录生成 messages.json', os.path.exists(path))
            if os.path.exists(path):
                with open(path, encoding='utf-8') as f:
                    check('落盘消息数为 3', len(json.load(f)) == 3)

        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    print()
    if failures:
        print(f'FAILED: {len(failures)} 项未通过 -> {failures}')
        return 1
    print('ALL PASSED')
    return 0


if __name__ == '__main__':
    sys.exit(main())
