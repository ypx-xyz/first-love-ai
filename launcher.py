# -*- coding: utf-8 -*-
"""聊天应用启动器 - 启动 Flask 服务器并打开浏览器（Windows）。"""
import subprocess
import sys
import os
import time
import webbrowser
import socket
import ctypes

APP_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 5000
LOG_FILE = os.path.join(APP_DIR, 'server.log')

def is_port_in_use(port):
    """检查端口是否在监听"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        try:
            s.connect(('127.0.0.1', port))
            return True
        except (ConnectionRefusedError, OSError):
            return False

def main():
    # 如果服务器已经在跑，直接打开网页
    if is_port_in_use(PORT):
        webbrowser.open(f'http://localhost:{PORT}')
        return

    # 启动 Flask 服务器（完全独立进程）
    app_path = os.path.join(APP_DIR, 'app.py')
    log_f = open(LOG_FILE, 'w', encoding='utf-8')

    # 用 CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS 让 Flask 完全脱离本进程
    proc = subprocess.Popen(
        [sys.executable, app_path],
        cwd=APP_DIR,
        stdout=log_f,
        stderr=log_f,
        creationflags=0x00000008 | 0x00000200,  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    )

    # 等待服务器就绪（最多等 15 秒）
    ready = False
    for i in range(30):
        time.sleep(0.5)
        if is_port_in_use(PORT):
            ready = True
            break

    log_f.close()

    if not ready:
        # 启动失败，读取日志显示错误
        try:
            with open(LOG_FILE, 'r', encoding='utf-8') as f:
                error_log = f.read()[:500]
        except Exception:
            error_log = '（无日志输出）'
        print('服务器启动失败。错误日志：\n', error_log)
        return

    # 打开浏览器
    webbrowser.open(f'http://localhost:{PORT}')

if __name__ == '__main__':
    main()
