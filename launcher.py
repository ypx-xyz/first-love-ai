# -*- coding: utf-8 -*-
"""聊天应用启动器 - 启动 Flask 服务器并打开浏览器（Windows）。

为什么不能只看「端口有没有在监听」：
    Windows 允许多个进程同时监听同一端口，Flask 也不会在这个情况下报错，
    于是请求会被路由到先启动的那个进程。如果 5000 端口上跑着别的程序，
    旧版启动器会以为「服务已经在跑」，直接打开浏览器指向别人的页面。
    所以这里必须确认端口上跑的到底是不是本应用，不是就顺延换端口。
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(APP_DIR, 'server.log')
PORT_START = int(os.environ.get('PORT', 5000))
PORT_TRIES = 20          # 起始端口被其他程序占用时，最多顺延探测多少个端口
STATS_PATH = '/api/stats'


def port_listening(port):
    """端口上是否有进程在监听。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        try:
            s.connect(('127.0.0.1', port))
            return True
        except OSError:
            return False


def is_our_app(port):
    """端口上跑的到底是不是本应用——只认 /api/stats 的返回结构，别的都不算。"""
    url = 'http://127.0.0.1:{}{}'.format(port, STATS_PATH)
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except Exception:
        return False
    return isinstance(data, dict) and 'total' in data and 'assistant' in data


def pick_port():
    """返回 (端口, 是否已有本应用在跑)。

    起始端口空闲        → 用它启动
    被本应用占用        → 判定为已在运行，直接开页面
    被其他程序占用      → 顺延到下一个空闲端口，绝不复用
    """
    for port in range(PORT_START, PORT_START + PORT_TRIES):
        if not port_listening(port):
            return port, False
        if is_our_app(port):
            return port, True
        print('[提示] 端口 {} 已被其他程序占用，换下一个端口'.format(port))
    return None, False


def wait_ready(port, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        if port_listening(port):
            return True
    return False


def main():
    port, running = pick_port()
    if port is None:
        print('从 {} 起的 {} 个端口都被占用，请用环境变量 PORT 指定其他端口。'.format(
            PORT_START, PORT_TRIES))
        return 1

    if running:
        print('检测到本应用已在 {} 端口运行，直接打开页面'.format(port))
        webbrowser.open('http://localhost:{}'.format(port))
        return 0

    app_path = os.path.join(APP_DIR, 'app.py')
    env = dict(os.environ, PORT=str(port))
    log_f = open(LOG_FILE, 'w', encoding='utf-8')

    # 用 CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS 让 Flask 完全脱离本进程
    subprocess.Popen(
        [sys.executable, app_path],
        cwd=APP_DIR,
        env=env,
        stdout=log_f,
        stderr=log_f,
        creationflags=0x00000008 | 0x00000200,  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    )

    if not wait_ready(port):
        log_f.close()
        try:
            with open(LOG_FILE, 'r', encoding='utf-8') as f:
                error_log = f.read()[:500]
        except Exception:
            error_log = '（无日志输出）'
        print('服务器启动失败。错误日志：\n', error_log)
        return 1

    log_f.close()
    webbrowser.open('http://localhost:{}'.format(port))
    return 0


if __name__ == '__main__':
    sys.exit(main())
