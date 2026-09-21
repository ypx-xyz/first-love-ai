#!/usr/bin/env python3
"""示例：把生成的陪伴者回复逐条写入聊天服务。

典型用法：你的消息生成模块（LLM / 规则引擎 / 定时任务）根据
`persona/reply_prompt.md` 产出回复文本后，调用本脚本注入聊天页面。

用法：
    python examples/send_reply.py "第一条回复" "第二条回复" ...
    # 不带参数时发送示例文本
"""
import os
import sys

import requests

BASE_URL = os.environ.get("CHAT_BASE_URL", "http://127.0.0.1:5000")


def send_reply(text, image=None):
    """把一条陪伴者回复写入聊天服务。image 可选，为 static/images/ 下的文件名。"""
    payload = {"text": text}
    if image:
        payload["image"] = image
    r = requests.post(f"{BASE_URL}/api/messages/assistant", json=payload, timeout=5)
    r.raise_for_status()
    return r.json()


def main():
    replies = sys.argv[1:] or [
        "……好久没这么晚还醒着了。",
        "你也还没睡？那正好，陪我说说话吧。",
    ]
    sent = 0
    for text in replies:
        try:
            send_reply(text)
            sent += 1
            print("OK", text[:24])
        except Exception as e:
            print("FAIL", e)
    print(f"SENT {sent}/{len(replies)}")


if __name__ == "__main__":
    main()
