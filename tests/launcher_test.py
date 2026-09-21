#!/usr/bin/env python3
"""launcher.py 端口选择逻辑测试。

背景（为什么要专门测这个）：
    Windows 允许多个进程同时监听同一端口，Flask 也不会报错，请求会被路由到
    先启动的那个进程。旧版启动器只判断「端口有没有在监听」，于是当 5000 上
    跑着别的程序时，它会以为「本应用已经在跑」，直接打开浏览器指向别人的页面。
    这里用「假装成别的服务」的方式锁住这个行为，防止回归。

不启动浏览器、不改任何真实文件；假的 HTTP 服务由进程内线程提供。
用法：python tests/launcher_test.py     # 退出码 0 = 全部通过
"""
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import URLError
from urllib.request import urlopen

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
failures = []


def check(name, cond, detail=''):
    if cond:
        print(f'  ✓ {name}')
    else:
        print(f'  ✗ {name} {detail}')
        failures.append(name)


def load_launcher():
    spec = importlib.util.spec_from_file_location('fla_launcher', os.path.join(ROOT, 'launcher.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class OtherAppHandler(BaseHTTPRequestHandler):
    """假装成「别的程序」：返回合法 JSON，但不是本应用的 /api/stats 结构。"""

    def do_GET(self):
        body = json.dumps({'hello': 'i am some other app'}).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_other_app(port):
    srv = HTTPServer(('127.0.0.1', port), OtherAppHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def wait_port(port, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.4)
        with socket.socket() as s:
            s.settimeout(1)
            try:
                s.connect(('127.0.0.1', port))
                return True
            except OSError:
                pass
    return False


def main():
    launcher = load_launcher()

    print('[1] 空闲端口不应被误判')
    idle = free_port()
    check('port_listening 空闲端口返回 False', launcher.port_listening(idle) is False)
    check('is_our_app 空闲端口返回 False', launcher.is_our_app(idle) is False)

    print('[2] 端口上是「别的服务」时不被认作本应用')
    p_other = free_port()
    srv = start_other_app(p_other)
    try:
        check('port_listening 检测到假服务', launcher.port_listening(p_other) is True)
        check('is_our_app 不把别的服务当自己', launcher.is_our_app(p_other) is False)

        print('[3] pick_port 遇到别的服务必须顺延，不能复用')
        launcher.PORT_START = p_other
        launcher.PORT_TRIES = 10
        picked, running = launcher.pick_port()
        check('顺延到了其他端口', picked is not None and picked != p_other, f'实际 {picked}')
        check('未被误判为「本应用已在运行」', running is False, f'实际 {running}')
    finally:
        srv.shutdown()
        srv.server_close()

    print('[4] 端口上是本应用时，认定「已在运行」')
    p_ours = free_port()
    tmp = tempfile.mkdtemp(prefix='fla_launcher_')
    env = dict(os.environ, PORT=str(p_ours), CHAT_DATA_DIR=tmp)
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, 'app.py')],
                            cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding='utf-8', errors='replace')
    try:
        if not wait_port(p_ours):
            print('  服务未能就绪，跳过第 4 组')
        else:
            check('is_our_app 识别出本应用', launcher.is_our_app(p_ours) is True)
            launcher.PORT_START = p_ours
            launcher.PORT_TRIES = 10
            picked, running = launcher.pick_port()
            check('pick_port 返回该端口', picked == p_ours, f'实际 {picked}')
            check('pick_port 标记为已在运行', running is True, f'实际 {running}')
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
