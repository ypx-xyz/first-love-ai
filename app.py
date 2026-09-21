#!/usr/bin/env python3
"""AI 陪伴聊天应用 - 本地微信风格网页后端。

一个基于 Flask 的轻量聊天服务：负责消息的持久化、并发安全写入、
增量拉取与图片服务。人格侧（如何生成陪伴者回复）与本服务解耦，
可通过外部进程/定时任务调用 `/api/messages/assistant` 注入回复，
也可接入任意 LLM 或规则引擎。

本仓库仅包含通用聊天骨架，不含任何真实语料或隐私数据。
"""
import json
import os
import socket
import sys
import uuid
import shutil
import threading
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_from_directory

app = Flask(__name__, static_folder='static', template_folder='templates')

# 生产部署建议将 DATA_DIR 指向可写目录；默认使用仓库内 messages.json
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('CHAT_DATA_DIR', APP_DIR)
MESSAGES_FILE = os.path.join(DATA_DIR, 'messages.json')
BACKUP_DIR = os.path.join(DATA_DIR, 'backups')
BACKUP_KEEP = 60   # 保留最近 60 份备份（可回滚最近 60 次写入）
IMAGES_DIR = os.path.join(APP_DIR, 'static', 'images')

@app.after_request
def add_no_cache(response):
    """禁用浏览器缓存，确保前端总能加载到最新 JS/HTML（避免缓存旧代码导致发送不显示）。"""
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

# 写锁：保护 messages.json 的读-改-写临界区。
# Flask 开发服务器默认多线程(threaded=True)，多个 POST 并发时会同时 load→append→save，
# 非原子导致竞态丢消息。用全局锁保证一次只处理一个写入。
_messages_lock = threading.Lock()

def _backup_current():
    """写前备份当前 messages.json 到 backups/，只保留最近 BACKUP_KEEP 份。
    用于崩溃后回滚到之前的状态。"""
    if not os.path.exists(MESSAGES_FILE):
        return
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        dst = os.path.join(BACKUP_DIR, f'messages_backup_{stamp}.json')
        if not os.path.exists(dst):
            shutil.copy2(MESSAGES_FILE, dst)
        # 清理：只保留最近 BACKUP_KEEP 份，按文件名（时间戳）排序
        files = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith('messages_backup_'))
        for old in files[:-BACKUP_KEEP]:
            try:
                os.remove(os.path.join(BACKUP_DIR, old))
            except Exception:
                pass
    except Exception:
        pass  # 备份失败不阻断主流程

def load_messages():
    """读消息列表。文件不存在、损坏或结构不对时一律降级为空列表。

    不直接抛异常的原因：数据文件可能被外部工具改坏，此时整个服务 500
    会让人无从下手；降级为空列表后服务仍可用，且写前备份里还留着旧数据。
    """
    if not os.path.exists(MESSAGES_FILE):
        return []
    try:
        with open(MESSAGES_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (ValueError, OSError) as e:
        app.logger.warning('messages.json 读取失败，按空列表处理：%s', e)
        return []
    return data if isinstance(data, list) else []

def extract_text(data):
    """从请求体里安全取出 text：非字符串（数字 / null / 嵌套对象）一律视为空。

    直接写 data.get('text', '').strip() 会在 text 是数字时抛 AttributeError，
    未捕获就变成 500。这里统一返回空串，由调用方回 400。
    """
    if not isinstance(data, dict):
        return ''
    text = data.get('text', '')
    return text.strip() if isinstance(text, str) else ''

def extract_image(data):
    """同理安全取出 image 文件名；非字符串一律视为未提供。"""
    if not isinstance(data, dict):
        return None
    image = data.get('image', '')
    if not isinstance(image, str):
        return None
    return image.strip() or None

def save_messages(messages):
    """原子写：先写临时文件再 os.replace 替换，避免并发读到半写文件；写前自动备份。"""
    _backup_current()
    tmp = MESSAGES_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MESSAGES_FILE)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/messages')
def get_messages():
    """全量返回；支持 ?since_id=<msg_id> 只返回该 id 之后的新消息（增量轮询，减少传输）。
    若 since_id 找不到（消息被清理/重置），返回 {needReset:true, messages:全量}，前端据此全量重载。"""
    messages = load_messages()
    since_id = request.args.get('since_id', '')
    if since_id:
        for i, m in enumerate(messages):
            if m.get('id') == since_id:
                return jsonify(messages[i + 1:])
        # since_id 找不到：消息已被清理/重置，提示前端全量重载
        return jsonify({'needReset': True, 'messages': messages})
    return jsonify(messages)

@app.route('/api/messages', methods=['POST'])
def send_message():
    # silent=True：请求体不是合法 JSON / 缺 Content-Type 时返回 None，
    # 而不是抛 415 —— 统一由下面的类型校验回 400，前端只处理一种错误形态。
    data = request.get_json(silent=True)
    text = extract_text(data)
    if not text:
        return jsonify({'error': '消息不能为空'}), 400

    with _messages_lock:
        messages = load_messages()
        msg = {
            'id': f'msg_{uuid.uuid4().hex[:8]}',
            'sender': 'user',
            'text': text,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M')
        }
        messages.append(msg)
        save_messages(messages)
    return jsonify(msg)

@app.route('/api/messages/assistant', methods=['POST'])
def add_assistant_message():
    """供外部进程/AI 调用：注入陪伴者(assistant)的回复。
    text 必填；image 可选，指向 static/images/ 下的文件名。"""
    data = request.get_json(silent=True)
    text = extract_text(data)
    if not text:
        return jsonify({'error': '消息不能为空'}), 400
    image = extract_image(data)

    with _messages_lock:
        messages = load_messages()
        msg = {
            'id': f'msg_{uuid.uuid4().hex[:8]}',
            'sender': 'assistant',
            'text': text,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M')
        }
        if image:
            msg['image'] = image
        messages.append(msg)
        save_messages(messages)
    return jsonify(msg)

@app.route('/api/images/<filename>')
def serve_image(filename):
    """服务陪伴者分享的图片（static/images 目录），供前端展示。"""
    return send_from_directory(IMAGES_DIR, filename)

@app.route('/api/stats')
def stats():
    messages = load_messages()
    # 用 .get 而非 []：数据文件可能被外部工具改过，缺字段不应导致 500
    assistant_count = sum(1 for m in messages if m.get('sender') == 'assistant')
    user_count = sum(1 for m in messages if m.get('sender') == 'user')
    last_msg = messages[-1] if messages else None
    return jsonify({
        'total': len(messages),
        'assistant': assistant_count,
        'user': user_count,
        'last_message': last_msg
    })

@app.route('/api/backups')
def list_backups():
    """列出可回滚的备份文件（供排查/恢复用）。"""
    if not os.path.isdir(BACKUP_DIR):
        return jsonify({'backups': []})
    files = sorted((f for f in os.listdir(BACKUP_DIR) if f.startswith('messages_backup_')), reverse=True)
    return jsonify({'backups': files})

def port_in_use(port):
    """端口上是否已有进程在监听。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        try:
            s.connect(('127.0.0.1', port))
            return True
        except OSError:
            return False

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    # Windows 下多个进程可以同时监听同一端口，Flask 不会报错，
    # 但请求会被路由到先启动的那个进程——直接起会静默地「帮别人接客」。
    # 这里显式拦一道，并提示换端口。
    if port_in_use(port):
        print(f'[错误] 端口 {port} 已被占用（可能是本应用已在运行，也可能是其他程序）。')
        print(f'       请改用其他端口，例如：set PORT=5100 && python app.py')
        sys.exit(1)
    app.run(host='127.0.0.1', port=port, debug=False)
