#!/usr/bin/env python3
"""LLM 回复链路（Reply Chain）。

把仓库里原本"解耦待接"的那一段补上：一条命令跑完
**检查简报 → 组装提示词 → 调用真实 LLM → 写入聊天页面**。

```
check_reply.build_report()          # 只读，产出 JSON 简报（待回复/上下文/防复读/候选图）
        ↓
persona/reply_prompt.md + 人格画像   # 组装 system / user 两条消息
        ↓
POST {LLM_BASE_URL}/chat/completions  # OpenAI 兼容接口，默认 DeepSeek
        ↓
POST {CHAT_BASE_URL}/api/messages/assistant   # 逐条写回聊天页
```

设计上刻意与聊天服务**解耦**：本脚本是独立进程，可以手动跑，也可以挂定时任务。
不做"服务内自动回复"是有意的——`reply_prompt.md` 的分批节奏、防复读窗口、
时段规则都要求"无新消息直接结束是常态"，用户一发消息就即时回会破坏这套节奏。

用法：
    python reply.py                          # 完整链路（需先配 LLM_API_KEY）
    python reply.py --dry-run                # 只打印将要发送的提示词，不调用、不写入
    python reply.py --print-only             # 调用 LLM 但只打印结果，不写入聊天页
    python reply.py --data-dir /path         # 指定数据目录（同 CHAT_DATA_DIR）
    python reply.py --persona persona/persona.json
    python reply.py --max-replies 3 --temperature 1.0

环境变量：
    LLM_API_KEY     必填。LLM 接口密钥。**不要写进仓库**，用环境变量传
    LLM_BASE_URL    默认 https://api.deepseek.com/v1（OpenAI 兼容即可，如本地 ollama 的 http://127.0.0.1:11434/v1）
    LLM_MODEL       默认 deepseek-chat
    LLM_TIMEOUT     默认 90 秒
    LLM_TEMPERATURE 默认 1.0
    CHAT_BASE_URL   默认 http://127.0.0.1:5000（聊天服务地址）
    CHAT_DATA_DIR   数据目录（内含 messages.json）

退出码：
    0 正常（含"无待回复消息、未调用 LLM"这一常态）
    1 配置问题（缺 API key / 提示词或人格文件缺失损坏）
    2 LLM 调用失败或回复解析失败
    3 回复已生成但写入聊天服务失败（此时会把回复原文打印出来，不浪费这次调用）
"""
import argparse
import json
import os
import re
import sys

import requests

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from check_reply import build_report  # noqa: E402  只读检查脚本，复用其简报产出

# ---- 路径 -----------------------------------------------------------------
REPLY_PROMPT_FILE = os.path.join(APP_DIR, 'persona', 'reply_prompt.md')
PERSONA_FILE = os.path.join(APP_DIR, 'persona', 'persona.json')            # 真实画像，已 gitignore
PERSONA_EXAMPLE_FILE = os.path.join(APP_DIR, 'persona', 'persona.example.json')
IMAGES_DIR = os.path.join(APP_DIR, 'static', 'images')

# ---- 默认值 ---------------------------------------------------------------
DEFAULT_BASE_URL = 'https://api.deepseek.com/v1'
DEFAULT_MODEL = 'deepseek-chat'
DEFAULT_TIMEOUT = 90
DEFAULT_TEMPERATURE = 1.0
DEFAULT_CHAT_BASE = 'http://127.0.0.1:5000'
FALLBACK_MAX_REPLIES = 6

# 回复生成规范里用到的占位符 → 人格画像字段
PLACEHOLDER_FIELDS = {
    '{PERSONA_NAME}': ('persona', 'name'),
    '{PERSONA_EXPERIENCE}': ('persona', 'experience'),
    '{USER}': ('peer', 'name'),
    '{USER_EXPERIENCE}': ('peer', 'experience'),
    '{USER_SCHEDULE}': ('peer', 'schedule'),
    '{RARE_SIGNAL}': ('signals', 'rare_signal'),
}

# 模板里写给「调用方」看的区块（占位符约定表、运行脚本的命令、写回接口示例）。
# 这些内容既要写在文档里给人看，又不能进提示词：占位符替换会把表格里的
# {PERSONA_NAME} 换成人名，填充后原样发出去就是一堆无意义文字。
CALLER_ONLY_RE = re.compile(r'<!--\s*CALLER-ONLY\s*-->.*?<!--\s*/CALLER-ONLY\s*-->', re.S)

OUTPUT_SPEC = """## 输出要求（必须严格遵守）

1. 只返回**一个 JSON 对象**，不要任何解释文字、前后不要 Markdown 代码块
2. 结构固定为：{{"replies": [{{"text": "回复内容", "image": "图片文件名"}}]}}
3. `text` 必填且非空；`image` 可选，**只能**取简报 `candidate_images` 里的文件名
4. 带图需同时满足：简报里 `img_roll` < `image_roll_threshold`；否则一律不带 `image` 字段
5. 条数按简报语境决定，**不得超过 {max_replies} 条**；同一批内不得重复意象 / 话题 / 收尾句式
6. 严格遵守上文硬约束 A（收尾防复读）、B（批内不重复）、C（禁止 AI 自我意识类元探讨）、D（主体归属校验）与时段规则

现在开始生成。若简报 `has_reply` 为 false，直接返回 {{"replies": []}}。"""


class ConfigError(Exception):
    """配置问题：缺 key、缺文件、文件损坏。"""


class LLMError(Exception):
    """调用或解析 LLM 回复失败。"""


def _utf8_stdout():
    """Windows 控制台默认 GBK，直接 print 提示词里的特殊字符会炸；能改就改。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')
        except Exception:
            pass


def log(*args):
    print(*args, flush=True)


def deep_get(data, path, default=None):
    cur = data
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def load_persona(path=None):
    """读人格画像。未指定时优先 persona/persona.json，缺失则回退示例文件。"""
    if path:
        target = path
        if not os.path.exists(target):
            raise ConfigError(f'找不到人格文件：{target}')
        fallback = False
    else:
        fallback = not os.path.exists(PERSONA_FILE)
        target = PERSONA_EXAMPLE_FILE if fallback else PERSONA_FILE

    try:
        with open(target, 'r', encoding='utf-8') as f:
            persona = json.load(f)
    except (ValueError, OSError) as e:
        raise ConfigError(f'人格文件读取失败：{target}（{e}）')

    if not isinstance(persona, dict):
        raise ConfigError(f'人格文件结构不对，应为 JSON 对象：{target}')
    return persona, target, fallback


def load_prompt_template(path=None):
    target = path or REPLY_PROMPT_FILE
    if not os.path.exists(target):
        raise ConfigError(f'找不到回复生成规范：{target}')
    try:
        with open(target, 'r', encoding='utf-8') as f:
            return f.read(), target
    except OSError as e:
        raise ConfigError(f'回复生成规范读取失败：{target}（{e}）')


def strip_caller_only(text):
    """删掉模板里标了 CALLER-ONLY 的区块（没标就原样返回，兼容旧版模板）。"""
    return CALLER_ONLY_RE.sub('', text)


def fill_placeholders(template, persona, data_dir):
    """把规范里的占位符换成真实取值，并在文末标注是否还有未替换的占位符。"""
    stripped = strip_caller_only(template)
    if stripped != template:
        log(f'[reply] 已剥离 {len(template) - len(stripped)} 字调用方专用内容（占位符表 / 命令 / 写回示例）')
    text = stripped
    for token, path in PLACEHOLDER_FIELDS.items():
        value = deep_get(persona, path)
        if value is None:
            value = '（未配置）'
        elif not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        text = text.replace(token, value)

    text = text.replace('{DATA_DIR}', data_dir)
    text = text.replace('{CHECK_SCRIPT}', 'check_reply.py')

    leftover = sorted(set(re.findall(r'\{[A-Z_]+\}', text)))
    if leftover:
        log(f'[reply] 提示：规范里仍有未替换的占位符 {leftover}（人格画像缺对应字段，已按原样保留）')
    return text


def build_messages(persona, report, prompt_text, max_replies):
    """组装 system / user 两条消息。dry-run 与真实调用共用，保证看到的即所发送的。"""
    persona_name = deep_get(persona, ('persona', 'name'), '（未命名）')
    system = (
        f'你是「{persona_name}」。以下是你的人设与回复生成规范，请严格按规范生成回复。\n\n'
        f'{prompt_text}\n\n'
        f'---\n\n'
        f'## 本次人格画像（JSON，权威来源，涉及事实以此为准）\n'
        f'```json\n{json.dumps(persona, ensure_ascii=False, indent=2)}\n```'
    )
    user = (
        f'## 本次检查简报（check_reply.py 输出，只读快照）\n'
        f'```json\n{json.dumps(report, ensure_ascii=False, indent=2)}\n```\n\n'
        f'{OUTPUT_SPEC.format(max_replies=max_replies)}'
    )
    return [{'role': 'system', 'content': system},
            {'role': 'user', 'content': user}]


def call_llm(messages, base_url, api_key, model, timeout, temperature, json_mode=False):
    """调用 OpenAI 兼容的 /chat/completions，返回回复正文（字符串）。"""
    url = base_url.rstrip('/') + '/chat/completions'
    payload = {'model': model, 'messages': messages,
               'temperature': temperature, 'stream': False}
    if json_mode:
        payload['response_format'] = {'type': 'json_object'}
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise LLMError(f'请求 LLM 失败：{e}')

    if resp.status_code != 200:
        # 402/401 通常是额度或密钥问题，单独点出来，省得对着 500 猜
        hint = ''
        if resp.status_code in (401, 403):
            hint = '（检查 LLM_API_KEY 是否正确）'
        elif resp.status_code == 402:
            hint = '（账户额度不足）'
        elif resp.status_code == 404:
            hint = '（检查 LLM_BASE_URL / LLM_MODEL 是否存在）'
        elif resp.status_code == 429:
            hint = '（触发限流，稍后重试）'
        raise LLMError(f'LLM 返回 {resp.status_code}{hint}：{resp.text[:300]}')

    try:
        data = resp.json()
        content = data['choices'][0]['message']['content']
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise LLMError(f'LLM 响应结构异常（{e}）：{resp.text[:300]}')
    if not isinstance(content, str) or not content.strip():
        raise LLMError('LLM 返回了空内容')
    return content


def extract_json(content):
    """从模型输出里抠出 JSON：容忍 ```json 代码块与前后多余的说明文字。"""
    text = content.strip()
    fenced = re.search(r'```(?:json)?\s*(.*?)```', text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    # 退一步：截取第一个 { 到最后一个 }（或 [ 到 ]）
    for opener, closer in (('{', '}'), ('[', ']')):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                continue
    raise LLMError(f'无法从模型输出里解析出 JSON，原文前 300 字：\n{content[:300]}')


def normalize_replies(obj, max_replies, candidate_images):
    """把模型输出规整成 [{text, image?}]。

    image 走白名单校验：只认简报 candidate_images 里的文件名，
    模型编出来的文件名（或路径）一律丢弃——避免把任意路径写进消息对象。
    """
    if isinstance(obj, dict):
        items = obj.get('replies')
        if items is None:
            items = obj.get('messages', obj.get('data', []))
    elif isinstance(obj, list):
        items = obj
    else:
        items = []
    if not isinstance(items, list):
        items = []

    allowed = set()
    for name in candidate_images or []:
        if isinstance(name, str):
            allowed.add(os.path.basename(name))

    out, dropped_image = [], 0
    for item in items:
        if isinstance(item, str):
            text, image = item, None
        elif isinstance(item, dict):
            text, image = item.get('text', item.get('content', '')), item.get('image')
        else:
            continue

        text = text.strip() if isinstance(text, str) else ''
        if not text:
            continue

        reply = {'text': text}
        if isinstance(image, str) and image.strip():
            base = os.path.basename(image.strip())
            if base in allowed:
                reply['image'] = base
            else:
                dropped_image += 1
        out.append(reply)
        if len(out) >= max_replies:
            break

    if dropped_image:
        log(f'[reply] 丢弃了 {dropped_image} 个不在候选清单里的 image 字段（防任意路径写入）')
    return out


def send_replies(replies, chat_base, timeout=10):
    """逐条写回聊天服务。返回成功条数；失败不影响其余条目。"""
    ok = 0
    for item in replies:
        try:
            resp = requests.post(f'{chat_base.rstrip("/")}/api/messages/assistant',
                                 json=item, timeout=timeout)
            if resp.status_code == 200:
                ok += 1
            else:
                log(f'[reply] 写入失败 {resp.status_code}：{resp.text[:160]}')
        except requests.RequestException as e:
            log(f'[reply] 写入失败：{e}')
    return ok


def main(argv=None):
    _utf8_stdout()
    ap = argparse.ArgumentParser(description='LLM 回复链路：检查 → 生成 → 写入')
    ap.add_argument('--data-dir', default=os.environ.get('CHAT_DATA_DIR', APP_DIR),
                    help='数据目录，内含 messages.json（默认取 CHAT_DATA_DIR 或仓库目录）')
    ap.add_argument('--persona', default=None, help='人格画像文件（默认 persona/persona.json，缺失时用示例）')
    ap.add_argument('--chat-base-url', default=os.environ.get('CHAT_BASE_URL', DEFAULT_CHAT_BASE),
                    help=f'聊天服务地址（默认 {DEFAULT_CHAT_BASE}）')
    ap.add_argument('--prompt', default=None, help='回复生成规范文件（默认 persona/reply_prompt.md）')
    ap.add_argument('--base-url', default=os.environ.get('LLM_BASE_URL', DEFAULT_BASE_URL),
                    help=f'LLM 接口地址（默认 {DEFAULT_BASE_URL}）')
    ap.add_argument('--model', default=os.environ.get('LLM_MODEL', DEFAULT_MODEL),
                    help=f'模型名（默认 {DEFAULT_MODEL}）')
    ap.add_argument('--api-key', default=os.environ.get('LLM_API_KEY'),
                    help='接口密钥；推荐用环境变量 LLM_API_KEY 传，避免落进命令历史')
    ap.add_argument('--timeout', type=int, default=int(os.environ.get('LLM_TIMEOUT', DEFAULT_TIMEOUT)))
    ap.add_argument('--temperature', type=float,
                    default=float(os.environ.get('LLM_TEMPERATURE', DEFAULT_TEMPERATURE)))
    ap.add_argument('--max-replies', type=int, default=None,
                    help='回复条数上限（默认取人格画像 reply_policy.max_replies）')
    ap.add_argument('--json-mode', action='store_true',
                    help='要求模型返回 JSON 对象（response_format=json_object，部分服务不支持）')
    ap.add_argument('--dry-run', action='store_true',
                    help='只打印将要发送的请求，不调用 LLM、不写入')
    ap.add_argument('--print-only', action='store_true',
                    help='调用 LLM 但只打印结果，不写入聊天服务')
    args = ap.parse_args(argv)

    try:
        persona, persona_path, using_example = load_persona(args.persona)
        prompt_text, prompt_path = load_prompt_template(args.prompt)
    except ConfigError as e:
        log(f'[reply] 配置错误：{e}')
        return 1

    if using_example:
        log(f'[reply] 未找到 {PERSONA_FILE}，本次使用示例人格 {os.path.basename(persona_path)}'
            f'（示例人物为虚构，仅供跑通链路）')

    max_replies = args.max_replies or deep_get(persona, ('reply_policy', 'max_replies')) or FALLBACK_MAX_REPLIES
    try:
        max_replies = max(1, int(max_replies))
    except (TypeError, ValueError):
        max_replies = FALLBACK_MAX_REPLIES

    # 1. 检查简报
    log(f'[reply] [1/4] 检查简报：{args.data_dir}')
    report = build_report(args.data_dir)
    log(f'[reply]        待回复 {report["pending_count"]} 条，上下文 {len(report["context"])} 条，'
        f'时段 {report["now_phase"]}，候选图 {len(report["candidate_images"])} 张，img_roll={report["img_roll"]}')

    filled = fill_placeholders(prompt_text, persona, args.data_dir)
    messages = build_messages(persona, report, filled, max_replies)

    # 2. 无新消息直接结束——这是常态，也是最省 token 的一条路径
    if not report['has_reply']:
        log('[reply] [2/4] 没有待回复的用户消息（has_reply=false），按规范直接结束，不调用 LLM')
        return 0

    # 3. 组装 & 调用
    log(f'[reply] [2/4] 已组装提示词：system {len(messages[0]["content"])} 字 / '
        f'user {len(messages[1]["content"])} 字（{os.path.basename(prompt_path)} + 人格画像 + 简报）')
    if args.dry_run:
        log('[reply] --dry-run：以下是将要发送的请求，未调用 LLM、未写入\n')
        log(json.dumps({'model': args.model, 'messages': messages,
                        'temperature': args.temperature}, ensure_ascii=False, indent=2))
        return 0

    if not args.api_key:
        log('[reply] 配置错误：缺少 LLM_API_KEY（用环境变量传入，勿写入仓库）。'
            '只想看提示词可加 --dry-run')
        return 1

    log(f'[reply] [3/4] 调用 LLM：{args.base_url} / {args.model}')
    try:
        content = call_llm(messages, args.base_url, args.api_key, args.model,
                           args.timeout, args.temperature, args.json_mode)
        replies = normalize_replies(extract_json(content), max_replies,
                                    report['candidate_images'])
    except LLMError as e:
        log(f'[reply] LLM 失败：{e}')
        return 2

    if not replies:
        log('[reply] 模型返回了空回复列表，不写入')
        return 0
    log(f'[reply]        生成 {len(replies)} 条回复')

    # 4. 写入
    if args.print_only:
        log('[reply] --print-only：以下为生成结果，未写入聊天服务')
        log(json.dumps({'replies': replies}, ensure_ascii=False, indent=2))
        return 0

    log(f'[reply] [4/4] 写入聊天服务：{args.chat_base_url}')
    sent = send_replies(replies, args.chat_base_url)
    if sent != len(replies):
        log(f'[reply] 写入不完整（{sent}/{len(replies)}）。以下为回复原文，'
            f'可用 examples/send_reply.py 补发，无需重新调用 LLM：')
        log(json.dumps({'replies': replies}, ensure_ascii=False, indent=2))
        return 3
    log(f'[reply] 完成：{sent} 条已写入聊天页')
    return 0


if __name__ == '__main__':
    sys.exit(main())
