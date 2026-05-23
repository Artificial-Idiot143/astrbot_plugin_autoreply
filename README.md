# AutoReply — QQ 群聊 AI 自动回复插件

基于大语言模型（LLM）的 QQ 群聊 / 私聊自动回复系统，核心理念是**带记忆的智能群友**。

## 功能

- 📩 自动接收 QQ 群聊与私聊消息
- 🧠 LLM 智能生成口语化回复（支持 deepseek / 本地模型 / OpenAI 兼容 API）
- 💾 三层分级结构化记忆库，记住群聊历史
- 🔍 关键词 + 时间范围 双重检索历史聊天
- 🔄 每日自动维护（消息合并 / 记忆迁移 / 语言风格分析）
- 🎛️ `/autoreply on|off` 命令控制开关

## 快速开始

1. 将本文件夹放入 AstrBot 的 `data/plugins/autoreply/`
2. 编辑 `config.py`，填入你的 LLM API 信息
3. 启动 AstrBot，插件自动加载
4. 在 QQ 群发送 `/autoreply` 确认状态

## 配置

编辑 `config.py`：

```python
LLM_API_URL = "https://api.llm.ustc.edu.cn/v1/chat/completions"
LLM_MODEL_NAME = "deepseek-v4-pro"
LLM_API_KEY = "sk-xxx"
BOT_NAME = "A"
```

## 依赖

- Python 3.10+
- requests

## 文件结构

| 文件 | 用途 |
|------|------|
| `main.py` | AstrBot 插件入口 |
| `auto_reply.py` | 四步流水线回复引擎 |
| `memory_ai.py` | SQLite 记忆库引擎 |
| `daily_maintenance.py` | 每日维护脚本 |
| `config.py` | 集中配置 |
| `chat_analyze` | 语言风格描述文件 |

## 更多

详见 `instruction.md`（部署指南）和 `process.md`（流程说明）。

## License

MIT