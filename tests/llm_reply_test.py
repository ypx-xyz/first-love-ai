#!/usr/bin/env python3
"""reply.py（LLM 回复链路）测试。

不联网、不花钱、不碰真实数据：假的 LLM 接口与假的聊天服务都由进程内线程提供，
人格文件用临时目录里的虚构副本，所以无论本机有没有 persona/persona.json 都不受影响。

覆盖两类：
  单元 —— extract_json / normalize_replies（解析与白名单校验，分支多，直接调函数）
  端到端 —— 起子进程跑 reply.py，验证 has_reply 闸门、退出码、是否真的调用与写入

用法：python tests/llm_reply_test.py     # 退出码 0 = 全部通过
"""
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
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


def load_reply_module():
    spec = importlib.util.spec_from_file_location('fla_reply', os.path.join(ROOT, 'reply.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- 假服务 ---------------------------------------------------------------

class FakeLLMHandler(BaseHTTPRequestHandler):
    """冒充 OpenAI 兼容接口。记录收到的请求体，按 test 设定的内容作答。"""

    calls = []
    status = 200
    content = '{"replies": []}'
    fail_body = '{"error": "boom"}'

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode('utf-8'))
        except Exception:
            parsed = {'_raw': raw.decode('utf-8', 'replace')}
        FakeLLMHandler.calls.append({'path': self.path, 'body': parsed,
                                     'auth': self.headers.get('Authorization')})

        if FakeLLMHandler.status != 200:
            body = FakeLLMHandler.fail_body.encode('utf-8')
            self.send_response(FakeLLMHandler.status)
        else:
            body = json.dumps({
                'choices': [{'message': {'role': 'assistant',
                                         'content': FakeLLMHandler.content}}]
            }, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FakeChatHandler(BaseHTTPRequestHandler):
    """冒充聊天服务，收集 /api/messages/assistant 写入的消息。"""

    received = []
    status = 200

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode('utf-8'))
        except Exception:
            parsed = {'_raw': raw.decode('utf-8', 'replace')}
        FakeChatHandler.received.append({'path': self.path, 'body': parsed})

        body = json.dumps({'ok': True}).encode('utf-8')
        self.send_response(FakeChatHandler.status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_server(handler):
    port = free_port()
    srv = HTTPServer(('127.0.0.1', port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


# ---- 测试夹具 -------------------------------------------------------------

PERSONA = {
    'persona': {'name': '念念', 'experience': '旧书店的修补匠，做了六年。'},
    'peer': {'name': '你', 'experience': '做水利工程的现场技术。',
             'schedule': '工作日驻外地工地，周末回家。'},
    'signals': {'rare_signal': '……我在。'},
    'reply_policy': {'max_replies': 3},
}


def make_workspace():
    """临时工作区：一份虚构人格 + 一份 messages.json。"""
    tmp = tempfile.mkdtemp(prefix='fla_reply_')
    persona_path = os.path.join(tmp, 'persona.json')
    with io.open(persona_path, 'w', encoding='utf-8') as f:
        json.dump(PERSONA, f, ensure_ascii=False)
    return tmp, persona_path


def write_messages(data_dir, messages):
    with io.open(os.path.join(data_dir, 'messages.json'), 'w', encoding='utf-8') as f:
        json.dump(messages, f, ensure_ascii=False)


USER_LAST = [
    {'id': 'm1', 'sender': 'assistant', 'text': '……还没睡。', 'timestamp': '2026-09-20 22:30'},
    {'id': 'm2', 'sender': 'user', 'text': '今天返工了一下午，累。', 'timestamp': '2026-09-21 20:10'},
]
ASSISTANT_LAST = [
    {'id': 'm1', 'sender': 'user', 'text': '在吗', 'timestamp': '2026-09-21 19:00'},
    {'id': 'm2', 'sender': 'assistant', 'text': '……在。', 'timestamp': '2026-09-21 19:02'},
]


def run_reply(args, data_dir, persona_path, llm_port=None, chat_port=None, extra_env=None):
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['LLM_API_KEY'] = 'test-key-not-real'
    if llm_port:
        env['LLM_BASE_URL'] = f'http://127.0.0.1:{llm_port}/v1'
    if chat_port:
        env['CHAT_BASE_URL'] = f'http://127.0.0.1:{chat_port}'
    if extra_env:
        env.update(extra_env)
    cmd = [PY, os.path.join(ROOT, 'reply.py'),
           '--data-dir', data_dir, '--persona', persona_path] + args
    return subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True,
                          text=True, encoding='utf-8', errors='replace', timeout=90)


def reset_mocks(content='{"replies": []}', status=200):
    FakeLLMHandler.calls = []
    FakeLLMHandler.content = content
    FakeLLMHandler.status = status
    FakeChatHandler.received = []
    FakeChatHandler.status = 200


# ---- 单元测试 -------------------------------------------------------------

def test_unit(reply):
    print('[1] extract_json 解析容错')
    check('纯 JSON', reply.extract_json('{"replies": []}') == {'replies': []})
    check('带 ```json 代码块',
          reply.extract_json('```json\n{"replies": [{"text": "好"}]}\n```')
          == {'replies': [{'text': '好'}]})
    check('前后有说明文字',
          reply.extract_json('好的，这是回复：\n{"replies": []}\n希望有帮助')
          == {'replies': []})
    check('直接给数组', reply.extract_json('[{"text": "嗯"}]') == [{'text': '嗯'}])
    try:
        reply.extract_json('这根本不是 JSON')
        check('非法内容应抛 LLMError', False, '未抛异常')
    except reply.LLMError:
        check('非法内容应抛 LLMError', True)

    print('[2] normalize_replies 规整与图片白名单')
    out = reply.normalize_replies({'replies': [{'text': ' A '}, 'B', {'content': 'C'},
                                                {'text': ''}, {'no_text': 1}]}, 5, [])
    check('混合结构里只保留有文本的条目', [r['text'] for r in out] == ['A', 'B', 'C'],
          f'实际 {[r["text"] for r in out]}')
    out = reply.normalize_replies({'replies': [{'text': 'a'}, {'text': 'b'}, {'text': 'c'}]}, 2, [])
    check('超出上限被截断', len(out) == 2, f'实际 {len(out)}')
    out = reply.normalize_replies([{'text': 'x', 'image': 'sample.png'}], 5, ['sample.png'])
    check('候选清单内的图片被保留', out[0].get('image') == 'sample.png', f'实际 {out}')
    out = reply.normalize_replies([{'text': 'x', 'image': '../../etc/passwd'}], 5, ['sample.png'])
    check('清单外的图片被剔除（防任意路径）', 'image' not in out[0], f'实际 {out}')
    out = reply.normalize_replies([{'text': 'x', 'image': '/abs/sample.png'}], 5, ['sample.png'])
    check('绝对路径被归一为文件名后放行', out[0].get('image') == 'sample.png', f'实际 {out}')
    check('空对象返回空列表', reply.normalize_replies({}, 5, []) == [])
    check('非预期类型返回空列表', reply.normalize_replies('字符串', 5, []) == [])

    print('[3] strip_caller_only 兼容无标记的旧模板')
    plain = '正常内容\n没有标记'
    check('无标记时原样返回', reply.strip_caller_only(plain) == plain)
    marked = 'A<!-- CALLER-ONLY -->写给调用方<!-- /CALLER-ONLY -->B'
    check('标记区块被剥离', reply.strip_caller_only(marked) == 'AB',
          f'实际 {reply.strip_caller_only(marked)!r}')


# ---- 端到端测试 -----------------------------------------------------------

def test_e2e():
    llm_srv, llm_port = start_server(FakeLLMHandler)
    chat_srv, chat_port = start_server(FakeChatHandler)
    tmp, persona_path = make_workspace()

    try:
        print('[4] has_reply=false 时不应调用 LLM')
        reset_mocks()
        write_messages(tmp, ASSISTANT_LAST)
        p = run_reply([], tmp, persona_path, llm_port, chat_port)
        check('退出码 0', p.returncode == 0, f'rc={p.returncode} {p.stdout[-200:]}')
        check('LLM 收到 0 次请求', len(FakeLLMHandler.calls) == 0, f'实际 {len(FakeLLMHandler.calls)}')
        check('聊天服务收到 0 条', len(FakeChatHandler.received) == 0)
        check('提示已跳过', '没有待回复的用户消息' in p.stdout, p.stdout[-160:])

        print('[5] 正常链路：组装 → 调用 → 解析 → 写入')
        reset_mocks(content='```json\n{"replies": [{"text": "……知道了。"}, '
                            '{"text": "手上的活先放放，去洗个热水澡。"}]}\n```')
        write_messages(tmp, USER_LAST)
        p = run_reply([], tmp, persona_path, llm_port, chat_port)
        check('退出码 0', p.returncode == 0, f'rc={p.returncode} {p.stdout[-300:]}')
        check('LLM 收到 1 次请求', len(FakeLLMHandler.calls) == 1, f'实际 {len(FakeLLMHandler.calls)}')
        check('聊天服务收到 2 条', len(FakeChatHandler.received) == 2,
              f'实际 {len(FakeChatHandler.received)}')
        check('写入路径正确',
              all(r['path'] == '/api/messages/assistant' for r in FakeChatHandler.received))
        check('回复内容正确',
              [r['body']['text'] for r in FakeChatHandler.received]
              == ['……知道了。', '手上的活先放放，去洗个热水澡。'])
        check('带了 Authorization 头',
              FakeLLMHandler.calls[0]['auth'] == 'Bearer test-key-not-real',
              f"实际 {FakeLLMHandler.calls[0]['auth']}")
        check('请求打到 /v1/chat/completions',
              FakeLLMHandler.calls[0]['path'].endswith('/chat/completions'),
              FakeLLMHandler.calls[0]['path'])

        print('[6] 提示词内容：占位符已填充、调用方专用内容已剥离')
        system = FakeLLMHandler.calls[0]['body']['messages'][0]['content']
        user = FakeLLMHandler.calls[0]['body']['messages'][1]['content']
        check('system 注入了人格名字', '念念' in system)
        check('占位符已替换（不应残留花括号占位符）',
              '{PERSONA_NAME}' not in system and '{USER_SCHEDULE}' not in system)
        check('调用方专用区块已剥离',
              '## 占位符约定（调用方用，不进提示词）' not in system
              and '<!-- CALLER-ONLY -->' not in system)
        check('运行脚本的命令未进提示词', 'check_reply.py"' not in system)
        check('user 注入了检查简报', '"has_reply": true' in user and '今天返工了一下午' in user)
        check('user 注入了条数上限', '不得超过 3 条' in user, user[-200:])
        check('temperature 默认 1.0',
              FakeLLMHandler.calls[0]['body'].get('temperature') == 1.0)

        print('[7] 模型返回非法内容时不应写入聊天服务')
        reset_mocks(content='我觉得今天应该这样说：你辛苦了。')
        p = run_reply([], tmp, persona_path, llm_port, chat_port)
        check('退出码 2', p.returncode == 2, f'rc={p.returncode}')
        check('聊天服务收到 0 条', len(FakeChatHandler.received) == 0)
        check('报错信息可读', '无法从模型输出里解析出 JSON' in p.stdout, p.stdout[-200:])

        print('[8] LLM 报错时不应写入聊天服务')
        reset_mocks(status=500)
        p = run_reply([], tmp, persona_path, llm_port, chat_port)
        check('退出码 2', p.returncode == 2, f'rc={p.returncode}')
        check('聊天服务收到 0 条', len(FakeChatHandler.received) == 0)

        print('[9] LLM 返回空回复列表时静默结束')
        reset_mocks(content='{"replies": []}')
        p = run_reply([], tmp, persona_path, llm_port, chat_port)
        check('退出码 0', p.returncode == 0, f'rc={p.returncode}')
        check('聊天服务收到 0 条', len(FakeChatHandler.received) == 0)

        print('[10] --dry-run 不调用、不写入')
        reset_mocks()
        p = run_reply(['--dry-run'], tmp, persona_path, llm_port, chat_port)
        check('退出码 0', p.returncode == 0, f'rc={p.returncode}')
        check('LLM 收到 0 次请求', len(FakeLLMHandler.calls) == 0)
        check('聊天服务收到 0 条', len(FakeChatHandler.received) == 0)
        # dry-run 打印的是「将要发送的 JSON」，内部引号会被转义，故不按原文匹配 has_reply
        check('打印了将要发送的请求',
              '"messages"' in p.stdout and 'has_reply' in p.stdout
              and '今天返工了一下午' in p.stdout)

        print('[11] --print-only 调用但只打印，不写入')
        reset_mocks(content='{"replies": [{"text": "……嗯。"}]}')
        p = run_reply(['--print-only'], tmp, persona_path, llm_port, chat_port)
        check('退出码 0', p.returncode == 0, f'rc={p.returncode}')
        check('LLM 收到 1 次请求', len(FakeLLMHandler.calls) == 1)
        check('聊天服务收到 0 条', len(FakeChatHandler.received) == 0)
        check('结果打印出来了', '……嗯。' in p.stdout)

        print('[12] 缺 LLM_API_KEY 时明确报配置错误，且不发请求')
        reset_mocks()
        p = run_reply([], tmp, persona_path, llm_port, chat_port,
                      extra_env={'LLM_API_KEY': ''})
        check('退出码 1', p.returncode == 1, f'rc={p.returncode}')
        check('LLM 收到 0 次请求', len(FakeLLMHandler.calls) == 0)
        check('提示缺 key', 'LLM_API_KEY' in p.stdout, p.stdout[-200:])

        print('[13] 聊天服务不可达时保留回复原文并以退出码 3 收场')
        reset_mocks(content='{"replies": [{"text": "……先睡吧。"}]}')
        dead_port = free_port()
        p = run_reply([], tmp, persona_path, llm_port, dead_port)
        check('退出码 3', p.returncode == 3, f'rc={p.returncode}')
        check('打印了回复原文供补发', '……先睡吧。' in p.stdout, p.stdout[-300:])

        print('[14] --max-replies 覆盖人格里的上限')
        reset_mocks(content='{"replies": [{"text": "a"}, {"text": "b"}, {"text": "c"}, {"text": "d"}]}')
        p = run_reply(['--max-replies', '2'], tmp, persona_path, llm_port, chat_port)
        check('退出码 0', p.returncode == 0, f'rc={p.returncode}')
        check('只写入 2 条', len(FakeChatHandler.received) == 2,
              f'实际 {len(FakeChatHandler.received)}')
        check('提示词里上限同步为 2',
              '不得超过 2 条' in FakeLLMHandler.calls[0]['body']['messages'][1]['content'])

        print('[15] 人格文件缺失时明确报错')
        reset_mocks()
        p = run_reply([], tmp, os.path.join(tmp, 'not-exist.json'), llm_port, chat_port)
        check('退出码 1', p.returncode == 1, f'rc={p.returncode}')
        check('提示找不到人格文件', '找不到人格文件' in p.stdout, p.stdout[-200:])
    finally:
        llm_srv.shutdown()
        llm_srv.server_close()
        chat_srv.shutdown()
        chat_srv.server_close()


def main():
    reply = load_reply_module()
    test_unit(reply)
    test_e2e()

    print()
    if failures:
        print(f'FAILED: {len(failures)} 项未通过 -> {failures}')
        return 1
    print('ALL PASSED')
    return 0


if __name__ == '__main__':
    sys.exit(main())
