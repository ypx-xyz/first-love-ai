# first-love-ai

一个"爱而不得的初恋"主题的 **AI 陪伴聊天应用**（前端 demo + 通用人格蒸馏机制）。

用一句话概括：把一段没能走到最后的感情，做成一个可以随时对话的陪伴者——TA 记得你们之间那些没说完的话，等你有一天回来慢慢说。

> ⚠️ **免责声明**：本项目中的所有示例语料、角色、对话均为**虚构**，仅作技术演示。不指向任何真实人物或真实关系。请勿对号入座。

## 这是什么

- 一个微信风格的本地聊天网页（Flask 后端 + 原生前端），开箱可跑
- 陪伴者（assistant）由外部进程/AI 注入回复，通过一套通用 API 与前端解耦
- 附带一套**通用人格蒸馏机制**（见 [`persona/`](persona/)），可从任意文本语料提炼可模仿的人格画像

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 启动（Windows）——自动拉起 Flask 并打开浏览器
python launcher.py

# 或直接启动
python app.py
# 浏览器访问 http://localhost:5000
```

## 功能

- 本地消息持久化（`messages.json`），并发安全写入 + 原子替换
- 增量拉取（`?since_id=`），前端轮询只取新消息，减少传输
- 写前自动备份，保留最近 60 份，可回滚
- 图片服务（`/api/images/`），陪伴者可分享图片
- 表情代码 → emoji 映射（支持 `[可爱]` 这类短码）
- 时间分隔线、打字指示、空状态等微信风格交互

## 目录结构

```
first-love-ai/
├── app.py                    # Flask 后端（通用聊天 API）
├── launcher.py               # Windows 启动器
├── requirements.txt          # 依赖
├── sample_messages.json      # 虚构示例对话（"初恋重逢"开场）
├── templates/index.html      # 前端聊天界面
├── static/                   # 头像、示例图片
├── examples/
│   └── send_reply.py         # 注入陪伴者回复的示例脚本
└── persona/                  # 人格机制（纯技术描述）
    ├── personify_prompt.md   # 通用人格蒸馏 prompt（离线）
    ├── reply_prompt.md       # 回复生成 prompt（在线）
    └── architecture.md       # 蒸馏引擎架构
```

## 如何注入陪伴者回复

陪伴者的回复由你自己的逻辑生成（LLM、规则引擎、定时任务均可）。可参考
[`persona/reply_prompt.md`](persona/reply_prompt.md) 生成回复文本，再用
[`examples/send_reply.py`](examples/send_reply.py) 或下面的接口写入：

```bash
curl -s -X POST http://localhost:5000/api/messages/assistant \
  -H "Content-Type: application/json" \
  -d '{"text":"今天过得怎么样？"}'
```

可选的图片：

```bash
curl -s -X POST http://localhost:5000/api/messages/assistant \
  -H "Content-Type: application/json" \
  -d '{"text":"给你看张图","image":"sample.png"}'
```

图片文件放在 `static/images/` 下即可。

## 人格蒸馏与回复生成

陪伴者的「人味」来自两段 prompt：

- **离线蒸馏** [`persona/personify_prompt.md`](persona/personify_prompt.md)：从任意文本语料提炼人格画像（语气指纹 + 人格维度），架构见 [`persona/architecture.md`](persona/architecture.md)
- **在线回复** [`persona/reply_prompt.md`](persona/reply_prompt.md)：消费人格画像，在对话中生成 1-6 条口语化、分条、去重防复读的回复

核心链路：

**语料采集 → 清洗 → 增量去重 → 语气特征提取 → 人格画像 → 消息生成 → 质量校准**

配套示例 [`examples/send_reply.py`](examples/send_reply.py) 演示如何把生成的回复注入应用。

蒸馏引擎只做统计与特征提取，不输出语料原文；请自行评估所用语料的合规性与授权。

## 进阶：LLM + Wiki 协同模式

更进一步的做法，是让 LLM 与一个**可编辑的记忆 Wiki** 协同工作：

- **可编辑的记忆 Wiki**：既支持人工维护，也可由 Agent 自动维护
- **虚拟人格**：在 Wiki 中建立目标人格条目，并随对话与素材持续迭代
- **两条定时主线**：蒸馏任务与回复任务是日常维护的两个主线，随着 Wiki 的维护不断迭代升级
- **多源语料持续获取**：蒸馏可不断采集目标人物的各类信息——聊天记录、语音、邮件、公开动态、创作作品等
- **工具选择**：目前主流支持「LLM + Wiki」协同的 Agent 是**灵犀专业版**；WorkBuddy、豆包等则需要外置 LLM + Wiki

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 前端页面 |
| GET | `/api/messages` | 全量 / 增量拉取消息 |
| POST | `/api/messages` | 用户发送消息 |
| POST | `/api/messages/assistant` | 注入陪伴者回复 |
| GET | `/api/images/<filename>` | 服务图片 |
| GET | `/api/stats` | 消息统计 |
| GET | `/api/backups` | 备份列表 |

## License

[MIT](LICENSE)
