"""
memory_ai.py — 群聊记忆管理AI模块
================================
功能：
  1. 消息清洗 → 关键词提取 → 事件抽取 → 存入 SQLite 记忆库。
  2. 记忆迭代清理（三层分级 + 多因素评分），防止数据库无限膨胀。
  3. 提供 MemoryDB 类供主程序调用，所有数据库操作线程安全。

算法参考：
  - Intelligent Decay (时间衰减加权)
  - FadeMem (多因素重要性评分)
  - QDP (Query-Driven Pruning，检索命中加权)

依赖：Python 3.10+，sqlite3，requests，threading，json，re，os，time，logging，math
"""

import sqlite3
import json
import os
import re
import time
import math
import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple, Union

# ============================================================
# 日志
# ============================================================
if not logging.getLogger("memory_ai").handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
logger = logging.getLogger("memory_ai")

# ============================================================
# 配置：优先从 config.py 导入，否则使用默认值
# ============================================================
try:
    from config import (  # type: ignore
        DB_PATH,
        MEMORY_DB_PATH,
        LLM_API_URL,
        LLM_MODEL_NAME,
        LLM_API_KEY,
    )
    logger.info("已从 config.py 加载配置。")
except ImportError:
    logger.warning("config.py 不存在，使用内置默认配置。")
    DB_PATH = os.path.join(os.path.dirname(__file__), "chat.db")
    MEMORY_DB_PATH = os.path.join(os.path.dirname(__file__), "memory.db")
    LLM_API_URL = "http://localhost:1234/v1/chat/completions"
    LLM_MODEL_NAME = "local-model"
    LLM_API_KEY = ""

# ============================================================
# 默认清理配置（所有权重与阈值均可外部覆盖）
# ============================================================
DEFAULT_MAINTENANCE_CONFIG: Dict[str, Any] = {
    # --- 重要性评分权重（Intelligent Decay + FadeMem + QDP） ---
    "weight_time": 0.40,          # 时效分权重
    "weight_density": 0.35,       # 信息密度分权重
    "weight_retrieval": 0.25,     # 检索命中分权重

    # --- 时效衰减参数 ---
    "lambda_decay": 0.08,         # λ: 指数衰减系数，越大衰减越快（稍微加快）

    # --- 信息密度归一化参数 ---
    "max_content_len": 200,       # 消息长度归一化上限（字符）
    "max_keywords": 3,            # 关键词数归一化上限

    # --- 检索命中衰减 ---
    "retrieval_decay_lambda": 0.10,  # 检索命中指数衰减系数

    # --- 三层分级阈值（新策略） ---
    "tier1_hours": 72,            # 第一层：<3 天（72 小时），以半小时合并
    "tier2_days": 7,              # 第二层：3-7 天，以半天合并
    "keep_threshold": 0.70,       # ≥此值保留原文
    "compress_threshold": 0.35,   # ≥此值压缩为摘要，<此值清空原文

    # --- 时段合并配置 ---
    "tier1_merge_minutes": 30,    # 第一层合并窗口：30 分钟
    "tier2_merge_hours": 12,      # 第二层合并窗口：12 小时（半天）
    "merge_enabled": True,        # 是否启用时段合并

    # --- 检索日志清理 ---
    "retrieval_log_keep_days": 30,  # 只保留最近 30 天的检索日志

    # --- 季度合并 ---
    "merge_events_enabled": False,  # 暂时关闭，后续可再优化
}

# LLM 请求配置
LLM_TIMEOUT = 60       # 大幅增加，思维链模型需要更长时间
LLM_MAX_RETRIES = 2


# ============================================================
# 工具函数
# ============================================================

def _is_pure_emoji(text: str) -> bool:
    """
    判断消息是否为纯表情/无意义符号。
    策略：先直接统计有效字符（中文/英文/数字），数量够就直接通过。
    """
    if not text or not text.strip():
        return True

    # 1. 直接统计有效字符（中文、英文、数字）
    valid_chars = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", text)
    if len(valid_chars) >= 2:
        return False  # 有足够的有效字符，不是纯表情

    # 2. 有效字符少，检查是否为纯表情/颜文字/标点
    # 先移除一些常见的纯表情/符号
    cleaned = re.sub(
        r"[\U0001F600-\U0001F64F"
        r"\U0001F300-\U0001F5FF"
        r"\U0001F680-\U0001F6FF"
        r"\U0001F1E0-\U0001F1FF"
        r"\U00002702-\U000027B0"
        r"\U000024C2-\U0001F251"
        r"\U0001F900-\U0001F9FF"
        r"\U0001FA00-\U0001FA6F"
        r"\U0001FA70-\U0001FAFF"
        r"\U00002600-\U000026FF"
        r"\U0000FE00-\U0000FE0F"
        r"\U0000200D"
        r"\u2600-\u27BF"
        r"\u2B50"
        r"\u2764"
        r"\u00A9\u00AE"
        r"\s\W_]+", "", text
    )
    return len(cleaned) < 2


# 无意义词列表（匹配即过滤）
_MEANINGLESS_PATTERNS = re.compile(
    r"^(哈哈|呵呵|嘿嘿|嘻嘻|好的|收到|嗯|哦|噢|额|\+1|1|顶|赞|打卡|签到|冒泡|潜水|路过|晚安|早安|午安)[！!。.]*$",
    re.IGNORECASE,
)


def clean_message(text: str) -> Union[bool, str]:
    """
    清洗单条消息。
    
    过滤规则：
      - 空消息 / 纯空白
      - 纯表情 / 颜文字
      - 仅含无意义词（哈哈、好的、收到、+1、嗯、哦、打卡 等）
      - LLM 思考过程（"Thinking Process:" 开头的）
    
    返回:
        False — 该消息应被丢弃
        str  — 清洗后的文本（去除首尾空白）
    """
    if not text or not isinstance(text, str):
        return False

    text = text.strip()
    if not text:
        return False

    # 过滤掉 LLM 思考过程
    if text.lower().startswith("thinking process") or text.lower().startswith("thinking:"):
        return False
    if "**Analyze the Request:**" in text or text.startswith("**Analyze"):
        return False
    if "Role:" in text and "Task:" in text:
        return False

    if _is_pure_emoji(text):
        return False

    if re.match(r'^\[.+\]$', text):
        return False

    if _MEANINGLESS_PATTERNS.match(text):
        return False

    return text


# ============================================================
# LLM 调用（LM Studio OpenAI 兼容 API）
# ============================================================

def _call_llm_api(system_prompt: str, user_prompt: str) -> str:
    """
    通过 LM Studio 本地 API 调用 LLM。
    超时 LLM_TIMEOUT 秒，最多重试 LLM_MAX_RETRIES 次。
    支持思维链模型（优先使用 reasoning_content）。
    """
    import requests

    payload = {
        "model": LLM_MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 8192,
    }
    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"

    last_error = ""
    for attempt in range(LLM_MAX_RETRIES + 1):
        try:
            resp = requests.post(
                LLM_API_URL,
                json=payload,
                headers=headers,
                timeout=LLM_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            
            logger.debug(f"[_call_llm_api] 完整响应: {data}")
            
            # content（最终答案）优先，reasoning_content（思考）兜底
            message = data["choices"][0]["message"]
            content = ""
            
            if "content" in message and message["content"]:
                content = message["content"].strip()
                logger.debug(f"[_call_llm_api] 使用 content: {content[:200]}...")
            
            if not content and "reasoning_content" in message and message["reasoning_content"]:
                content = message["reasoning_content"].strip()
                logger.debug(f"[_call_llm_api] 使用 reasoning_content: {content[:200]}...")
            
            if not content:
                raise ValueError("AI返回空内容")
            
            return content
        except requests.exceptions.Timeout:
            last_error = f"超时（{LLM_TIMEOUT}s）"
        except requests.exceptions.ConnectionError:
            last_error = f"无法连接 {LLM_API_URL}"
        except requests.exceptions.RequestException as e:
            last_error = str(e)
        except (KeyError, IndexError, TypeError, ValueError) as e:
            last_error = f"响应格式异常: {e}"

        if attempt < LLM_MAX_RETRIES:
            time.sleep(1.5 ** attempt)

    raise ConnectionError(f"LLM 调用失败（已重试 {LLM_MAX_RETRIES} 次）: {last_error}")


def _strip_thinking_process(output: str) -> str:
    """
    从思维链输出中提取最终答案。
    思维链模型会先输出思考过程，最后才输出答案。
    关键：思维链结构行（以 * 或数字开头、含 Role:/Task:/Constraint 等）
    一律过滤，不管是否含中文。因为 LLM 会在思维链中回显系统提示词。
    """
    if not output:
        return ""

    THINKING_STRUCTURAL = [
        "Thinking Process", "Analyze the Request", "Role:", "Task:",
        "Constraint", "Input", "Let me", "I need", "First", "Next",
        "Then", "Finally", "**Analyze", "*   **", "*   Role:",
        "*   Task:", "*   Input", "Wait,", "Wait, the",
        "我来分析", "我需要", "首先", "其次", "最后", "思考", "分析",
        "Constraints:", "Options:", "Option", "Let's", "Let us",
        "Final Polish", "Final Polish:", "Final Polish:",
        "Language Constraint", "Task Type", "QQ Group Chat Bot",
    ]

    def _is_thinking_line(line: str) -> bool:
        s = line.strip()
        if not s:
            return True
        if re.match(r'^\*+\s', s):
            return True
        if s.startswith('- ') or s.startswith('* '):
            return True
        if re.match(r'^\d+[\.\)]\s', s):
            return True
        if any(p in s for p in THINKING_STRUCTURAL):
            return True
        chinese_chars = sum(1 for c in s if '\u4e00' <= c <= '\u9fff')
        total_chars = len(s.replace(' ', ''))
        if total_chars > 0 and chinese_chars / total_chars < 0.3:
            return True
        return False

    for marker in [
        "Final Answer:", "最终答案：", "最终答案:", "Answer:", "答案：", "答案:",
        "Selected:", "选择：", "选择:", "选:", "选：",
        "回复：", "回复:", "我会说：", "我会说:",
    ]:
        idx = output.rfind(marker)
        if idx != -1:
            after = output[idx + len(marker):].strip()
            if after:
                after = _clean_final_answer(after)
                if after:
                    return after

    lines = [line.strip() for line in output.strip().split("\n") if line.strip()]
    if not lines:
        return output

    candidates = []
    for line in reversed(lines):
        if _is_thinking_line(line):
            continue
        cleaned = _clean_final_answer(line)
        if cleaned:
            candidates.append(cleaned)

    if candidates:
        last_candidate = candidates[0]
        chinese_count = sum(1 for c in last_candidate if '\u4e00' <= c <= '\u9fff')
        if chinese_count >= 2:
            return last_candidate

        best_candidate = None
        max_chinese = 0
        for c in candidates:
            cnt = sum(1 for char in c if '\u4e00' <= char <= '\u9fff')
            if cnt > max_chinese:
                max_chinese = cnt
                best_candidate = c
        if best_candidate:
            return best_candidate
        return candidates[0]

    last_line = lines[-1]
    return _clean_final_answer(last_line)


def _clean_final_answer(text: str) -> str:
    """清理最终答案的辅助函数。"""
    if not text:
        return ""
    
    text = text.strip()
    
    # 去除开头的英文引导语（直到第一个中文字符或中文标点）
    # 找到第一个中文字符或中文标点的位置
    first_chinese_idx = None
    for i, c in enumerate(text):
        if '\u4e00' <= c <= '\u9fff' or c in '。，！？、；：':
            first_chinese_idx = i
            break
    if first_chinese_idx is not None and first_chinese_idx > 0:
        text = text[first_chinese_idx:].strip()
    
    # 去除括号注释
    text = re.sub(r"\s*\([^)]*\)$", "", text).strip()
    
    # 去除引号
    text = re.sub(r"^\s*\"", "", text)
    text = re.sub(r"\"[^\"]*$", "", text)
    text = re.sub(r"^\s*“", "", text)
    text = re.sub(r"”[^\”]*$", "", text)
    
    return text.strip()


def extract_binary_decision(output: str) -> str:
    """
    专门从思维链输出中提取二值判定（0 或 1）。
    
    策略（按优先级）：
      1. 整行只包含 0 或 1 → 直接返回
      2. 行末是独立的 0 或 1 → 返回
      3. 有明确的中文决策标记（"输出: 0"、"答案是1"等）→ 返回
      4. 默认返回 "1"（倾向于回复/使用记忆库）
    
    注意：不匹配英文 decision/output/selected 等标记，
         因为这些经常出现在模型的思考草稿中导致误判。
    """
    if not output:
        return "1"
    
    lines = [line.strip() for line in output.strip().split("\n") if line.strip()]
    if not lines:
        return "1"
    
    # 1. 最优先：整行只包含 0 或 1（最可靠的信号）
    for line in reversed(lines):
        line_clean = re.sub(r"[^01]", "", line)
        if line_clean == "1":
            return "1"
        if line_clean == "0":
            return "0"
    
    # 2. 行末是独立的 0 或 1（后面只有标点/空格）
    end_pattern = re.compile(r"(?<!\w)[01][\s.。！!?]*$")
    for line in reversed(lines):
        match = end_pattern.search(line)
        if match:
            char = match.group(0)[0]
            return char
    
    # 3. 只匹配中文决策标记（更可靠，不容易误匹配）
    cn_decision_pattern = re.compile(r"(?:输出|答案|最终|选择|判定|结果)[：:]\s*(0|1)")
    for line in reversed(lines):
        match = cn_decision_pattern.search(line)
        if match:
            return match.group(1)
    
    # 4. 默认返回 1（倾向于回复/使用记忆库）
    return "1"


def extract_keywords(text: str) -> List[str]:
    """
    调用 LLM 从消息中提取 1-3 个关键词。
    不提取时间词（如"昨天"、"前天"、"上午"），优先提取人名、事件、主题词。
    
    返回:
        list[str] — 关键词列表，失败返回空列表。
    """
    system = (
        "你是一个关键词提取器。"
        "直接输出1-3个关键词（逗号分隔），不要任何思考。"
        "注意：不要提取时间词（如昨天、前天、上午、下午、晚上）。"
        "注意：不要提取疑问词（如什么、怎么、为什么、谁、哪、吗、了）。"
        "注意：不要提取动词（如聊、说、问、看）。"
        "注意：不要提取英文（如 thinking、analyze、task）。"
        "注意：不要提取带引号或标点开头的词。"
        "优先提取人名、事件、主题词、具体物品/话题。"
    )
    user = f"从以下消息中提取1-3个关键词（逗号分隔），不要时间词，直接输出：\n{text}"

    try:
        output = _call_llm_api(system, user)
        # 先尝试剥离思考过程
        clean = _strip_thinking_process(output)
        keywords = [kw.strip() for kw in clean.replace("，", ",").split(",") if kw.strip()]
        # 过滤掉明显是思考过程的关键词
        keywords = [
            kw for kw in keywords
            if len(kw) <= 10
            and len(kw) > 1
            and not kw.startswith("**")
            and not kw.startswith("* ")
            and not kw.startswith("\"")
            and not kw.endswith("\"")
            and not kw.startswith("'")
            and not kw.endswith("'")
            and "Thinking" not in kw
            and "Analyze" not in kw
            and "Role:" not in kw
            and "Task:" not in kw
            and "Condition" not in kw
            and "Request" not in kw
            and not re.search(r'^[a-zA-Z\s]+$', kw)
        ]
        return keywords[:3]
    except Exception as e:
        logger.warning(f"关键词提取失败: {e}")
        return []


def parse_time_keywords(text: str) -> Optional[Tuple[str, str]]:
    """
    从文本中解析时间关键词，转换成时间范围（鲁棒性更强）。
    
    支持:
        今天、昨天、前天
        今天上午、昨天下午
        上周、本周、这周一、这周二...
        上个月、这个月
        三月、四月份、去年五月
    
    返回:
        (start_time, end_time) — 时间范围字符串 (YYYY-MM-DD HH:MM:SS)，未识别时返回 None。
    """
    text = text.lower()
    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    
    # ========== 辅助函数：根据时间点调整上下午 ==========
    def adjust_time_range(date_obj, t_text):
        s = date_obj.strftime("%Y-%m-%d 00:00:00")
        e = date_obj.strftime("%Y-%m-%d 23:59:59")
        if "上午" in t_text:
            e = date_obj.strftime("%Y-%m-%d 12:00:00")
        elif "下午" in t_text or "傍晚" in t_text:
            s = date_obj.strftime("%Y-%m-%d 12:00:00")
        elif "晚上" in t_text:
            s = date_obj.strftime("%Y-%m-%d 18:00:00")
        return s, e
    
    # ========== 1. 今天、昨天、前天 ==========
    if "今天" in text:
        return adjust_time_range(today, text)
    
    if "昨天" in text:
        yesterday = today - timedelta(days=1)
        return adjust_time_range(yesterday, text)
    
    if "前天" in text:
        day_before = today - timedelta(days=2)
        return adjust_time_range(day_before, text)
    
    # ========== 2. 上周、本周、这周一、这周二 ==========
    if "本周" in text:
        weekday = today.weekday()  # 周一=0
        week_start = today - timedelta(days=weekday)
        start = week_start.strftime("%Y-%m-%d 00:00:00")
        end = now.strftime("%Y-%m-%d %H:%M:%S")
        return start, end
    
    if "上周" in text:
        weekday = today.weekday()
        last_week_start = today - timedelta(days=weekday + 7)
        last_week_end = last_week_start + timedelta(days=6)
        start = last_week_start.strftime("%Y-%m-%d 00:00:00")
        end = last_week_end.strftime("%Y-%m-%d 23:59:59")
        return start, end
    
    # 这周一、这周二...
    weekday_map = {
        "周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6,
    }
    for wk_day, offset in weekday_map.items():
        if wk_day in text and ("这" in text or "本周" in text):
            weekday = today.weekday()
            target_day = today - timedelta(days=weekday - offset)
            # 如果目标日期在今天之后，说明是上一周
            if target_day > today:
                target_day = target_day - timedelta(days=7)
            return adjust_time_range(target_day, text)
    
    # ========== 3. 上个月、这个月 ==========
    if "这个月" in text:
        month_start = today.replace(day=1)
        start = month_start.strftime("%Y-%m-%d 00:00:00")
        end = now.strftime("%Y-%m-%d %H:%M:%S")
        return start, end
    
    if "上个月" in text:
        # 上个月的第一天
        if today.month == 1:
            last_month = today.replace(year=today.year-1, month=12, day=1)
        else:
            last_month = today.replace(month=today.month-1, day=1)
        # 上个月的最后一天
        if last_month.month == 12:
            next_month = last_month.replace(year=last_month.year+1, month=1, day=1)
        else:
            next_month = last_month.replace(month=last_month.month+1, day=1)
        last_month_end = next_month - timedelta(days=1)
        start = last_month.strftime("%Y-%m-%d 00:00:00")
        end = last_month_end.strftime("%Y-%m-%d 23:59:59")
        return start, end
    
    # ========== 4. 月份（三月、四月份、去年五月） ==========
    month_map = {
        "一月": 1, "二月": 2, "三月": 3, "四月": 4, "五月": 5, "六月": 6,
        "七月": 7, "八月": 8, "九月": 9, "十月": 10, "十一月": 11, "十二月": 12,
        "1月": 1, "2月": 2, "3月": 3, "4月": 4, "5月": 5, "6月": 6,
        "7月": 7, "8月": 8, "9月": 9, "10月": 10, "11月": 11, "12月": 12,
    }
    for m_str, m_num in month_map.items():
        if m_str in text:
            target_year = today.year
            if "去年" in text:
                target_year = today.year - 1
            
            month_start = datetime(target_year, m_num, 1)
            # 这个月的最后一天
            if m_num == 12:
                next_month = datetime(target_year + 1, 1, 1)
            else:
                next_month = datetime(target_year, m_num + 1, 1)
            month_end = next_month - timedelta(days=1)
            
            start = month_start.strftime("%Y-%m-%d 00:00:00")
            end = month_end.strftime("%Y-%m-%d 23:59:59")
            return start, end
    
    return None


def extract_event(text: str) -> Dict[str, Any]:
    """
    调用 LLM 将消息转换为结构化事件。
    
    返回:
        dict — {"subject": ..., "verb": ..., "object": ..., "summary": ...}
               无事件时返回 {}。
    """
    system = "你是一个事件抽取器。直接输出JSON，不要任何思考过程。"
    user = (
        "将以下群聊消息转换为事件JSON，含subject/verb/object/summary。"
        "无事件则返回{}。不要思考过程，直接输出JSON。\n"
        f"消息：{text}"
    )

    try:
        output = _call_llm_api(system, user)
        
        json_str = ""
        
        # 1. 先找代码块
        code_block_match = re.findall(r"```(?:json)?\s*\n?(.*?)\n?```", output, re.DOTALL)
        if code_block_match:
            json_str = code_block_match[-1].strip()
        
        # 2. 如果没有代码块，找最后一个 {}
        if not json_str:
            start = output.find("{")
            end = output.rfind("}")
            if start != -1 and end != -1:
                json_str = output[start:end + 1]
        
        # 3. 如果还没找到，尝试剥离思考过程后再找
        if not json_str:
            clean = _strip_thinking_process(output)
            start = clean.find("{")
            end = clean.rfind("}")
            if start != -1 and end != -1:
                json_str = clean[start:end + 1]
        
        if json_str:
            event = json.loads(json_str)
            if not isinstance(event, dict) or not event:
                return {}
            return {
                "subject": str(event.get("subject", "")),
                "verb": str(event.get("verb", "")),
                "object": str(event.get("object", "")),
                "summary": str(event.get("summary", "")),
            }
        return {}
    except Exception as e:
        logger.warning(f"事件提取失败: {e}")
        return {}


def compress_message(text: str) -> str:
    """
    调用 LLM 将消息压缩为一句话摘要。
    """
    system = "你是一个文本压缩器。直接输出摘要，不要任何思考过程。"
    user = f"将以下群聊消息压缩为一句话摘要，不要思考过程：\n{text}"

    try:
        output = _call_llm_api(system, user)
        clean = _strip_thinking_process(output)
        result = clean.strip()
        if result:
            return result
        return text[:80] + ("…" if len(text) > 80 else "")
    except Exception as e:
        logger.warning(f"消息压缩失败: {e}")
        return text[:80] + ("…" if len(text) > 80 else "")


def merge_messages(messages: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    合并多条消息为一条概括（一句话摘要 + 关键词）。
    
    参数:
        messages: [{"user_name": "...", "content": "...", "timestamp": "..."}, ...]
    
    返回:
        {"summary": "...", "keywords": ["...", "..."], "source": "ai|rule|fallback"}
    """
    if not messages:
        return {"summary": "", "keywords": [], "source": "fallback"}
    
    # 拼接消息文本
    text = "\n".join([f"[{m['user_name']}] {m['content']}" for m in messages if m['content'].strip()])
    if not text:
        return {"summary": "", "keywords": [], "source": "fallback"}
    
    # 先尝试用 LLM 合并
    try:
        logger.info(f"[AI合并] 尝试合并{len(messages)}条消息...")
        system = "你是一个消息合并器。简单任务，不要思考，直接输出JSON。"
        user = (
            "合并以下多条群聊消息，直接输出包含两个字段的JSON：\n"
            "  1. summary: 一句话概括（不超过50字）\n"
            "  2. keywords: 1-3个关键词数组\n"
            "直接输出JSON，不要思考。\n"
            f"消息：\n{text}"
        )
        
        output = _call_llm_api(system, user)
        
        if output and output.strip():
            # 策略1：从思维链输出中提取最后一个JSON（思维链会先思考，最后输出答案）
            json_str = ""
            
            # 1. 先找代码块
            code_block_match = re.findall(r"```(?:json)?\s*\n?(.*?)\n?```", output, re.DOTALL)
            if code_block_match:
                json_str = code_block_match[-1].strip()  # 取最后一个
            
            # 2. 如果没有代码块，找最后一个 {}
            if not json_str:
                start = output.find("{")
                end = output.rfind("}")
                if start != -1 and end != -1:
                    json_str = output[start:end + 1]
            
            if json_str:
                try:
                    result = json.loads(json_str)
                    
                    summary = str(result.get("summary", "")).strip()
                    keywords = result.get("keywords", [])
                    if isinstance(keywords, str):
                        keywords = [keywords.strip()]
                    keywords = [str(kw).strip() for kw in keywords if str(kw).strip()]
                    
                    if summary:
                        logger.info(f"[AI合并] 成功！摘要: {summary}")
                        return {"summary": summary, "keywords": keywords[:3], "source": "ai"}
                except Exception as e:
                    # 继续尝试策略2
                    pass
            
            # 策略2：从思考过程中提取信息（即使JSON没输出）
            # 尝试找 "Final Summary Draft" 或 "Draft"
            summary = ""
            summary_match = re.search(r"Final Summary Draft[:：]\s*([^\n]+)", output)
            if not summary_match:
                summary_match = re.search(r"Draft\s*\d+[:：]\s*([^\n]+)", output)
            if summary_match:
                summary = summary_match.group(1).strip()
                # 清理括号里的英文
                summary = re.sub(r"\([^)]*\)", "", summary).strip()
            
            # 如果没找到，找 "summary" 或 "Summary"
            if not summary:
                summary_match = re.search(r"[Ss]ummary[:：]\s*([^\n]{10,100})", output)
                if summary_match:
                    summary = summary_match.group(1).strip()
            
            # 尝试找关键词
            keywords = []
            keyword_match = re.search(r"[Pp]otential [Kk]eywords[:：]\s*([^\n]+)", output)
            if keyword_match:
                keyword_text = keyword_match.group(1).strip()
                keywords = re.split(r"[，,、]", keyword_text)
                keywords = [kw.strip() for kw in keywords if kw.strip() and len(kw) < 10]
            
            if summary:
                logger.info(f"[AI合并] 成功！摘要: {summary}")
                if not keywords:
                    keywords = ["聊天记录"]
                return {"summary": summary[:50], "keywords": keywords[:3], "source": "ai"}
    except Exception as e:
        logger.info(f"[AI合并] 失败，原因: {e}")
    
    # 兜底：规则合并（不依赖 LLM）
    logger.info(f"[规则合并] 使用规则合并{len(messages)}条消息...")
    try:
        # 提取第一条和最后一条消息
        first_msg = messages[0] if messages else None
        last_msg = messages[-1] if messages else None
        
        # 生成摘要
        if len(messages) == 1:
            summary = first_msg['content'][:50] + ("…" if len(first_msg['content']) > 50 else "")
        else:
            user_count = len(set(m['user_name'] for m in messages))
            summary = f"{first_msg['user_name']}等{user_count}人在群里聊天，共{len(messages)}条消息"
        
        # 简单关键词提取
        keywords = []
        all_text = " ".join(m['content'] for m in messages)
        
        # 尝试从消息中提取一些常见词汇（简单启发式）
        common_words = ["讨论", "说", "问题", "项目", "工作", "今天", "明天", "好的", "可以", "是的"]
        for word in common_words:
            if word in all_text and word not in keywords:
                keywords.append(word)
            if len(keywords) >= 2:
                break
        
        if not keywords:
            keywords = ["聊天记录"]
        
        logger.info(f"[规则合并] 成功！摘要: {summary}")
        return {"summary": summary, "keywords": keywords, "source": "rule"}
    except Exception as e2:
        logger.warning(f"[规则合并] 也失败: {e2}")
        # 终极兜底
        fallback_summary = text[:50] + ("…" if len(text) > 50 else "")
        logger.info(f"[兜底合并] 使用终极兜底")
        return {"summary": fallback_summary, "keywords": ["聊天记录"], "source": "fallback"}


# ============================================================
# MemoryDB — 记忆数据库管理类
# ============================================================

class MemoryDB:
    """
    群聊记忆数据库管理器。
    所有公开方法均通过 _lock 保证线程安全。
    
    表结构：
      messages(id, user_id, user_name, content, timestamp, importance_score,
               compressed_content, content_state, last_maintained)
      keywords(id, keyword, msg_id)
      events(id, timestamp, user_name, subject, verb, object, summary, msg_id)
      retrieval_log(id, msg_id, query_text, timestamp)
    """

    def __init__(self, db_path: str = MEMORY_DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_tables()

    # ---------- 初始化 ----------

    def _init_tables(self) -> None:
        """建表（如不存在）。"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=WAL")      # WAL 模式提升并发读
            conn.execute("PRAGMA foreign_keys=ON")
            cur = conn.cursor()

            cur.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id         TEXT    NOT NULL,
                    user_name       TEXT    NOT NULL DEFAULT '',
                    content         TEXT    NOT NULL DEFAULT '',
                    timestamp       TEXT    NOT NULL DEFAULT '',
                    importance_score REAL   NOT NULL DEFAULT 1.0,
                    compressed_content TEXT DEFAULT NULL,
                    content_state   TEXT    NOT NULL DEFAULT 'original',
                    last_maintained TEXT    DEFAULT NULL
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS keywords (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword TEXT    NOT NULL,
                    msg_id  INTEGER NOT NULL,
                    FOREIGN KEY (msg_id) REFERENCES messages(id) ON DELETE CASCADE
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT    NOT NULL DEFAULT '',
                    user_name TEXT    NOT NULL DEFAULT '',
                    subject   TEXT    NOT NULL DEFAULT '',
                    verb      TEXT    NOT NULL DEFAULT '',
                    object    TEXT    NOT NULL DEFAULT '',
                    summary   TEXT    NOT NULL DEFAULT '',
                    msg_id    INTEGER NOT NULL,
                    FOREIGN KEY (msg_id) REFERENCES messages(id) ON DELETE CASCADE
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS retrieval_log (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    msg_id     INTEGER NOT NULL,
                    query_text TEXT    NOT NULL DEFAULT '',
                    timestamp  TEXT    NOT NULL DEFAULT '',
                    FOREIGN KEY (msg_id) REFERENCES messages(id) ON DELETE CASCADE
                )
            """)

            # 索引
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_dedup ON messages(user_id, content, timestamp)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_keywords_msg_id ON keywords(msg_id)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_keywords_keyword ON keywords(keyword)"
            )
            try:
                cur.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_keywords_dedup ON keywords(keyword, msg_id)"
                )
            except sqlite3.IntegrityError:
                cur.execute(
                    "DELETE FROM keywords WHERE rowid NOT IN "
                    "(SELECT MIN(rowid) FROM keywords GROUP BY keyword, msg_id)"
                )
                cur.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_keywords_dedup ON keywords(keyword, msg_id)"
                )
                logger.info("已清理 keywords 表重复数据并创建去重索引")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_msg_id ON events(msg_id)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_retrieval_log_msg_id ON retrieval_log(msg_id)"
            )

            cur.execute("""
                CREATE TABLE IF NOT EXISTS patterns (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    category          TEXT    NOT NULL DEFAULT '',
                    description       TEXT    NOT NULL DEFAULT '',
                    trigger_words     TEXT    NOT NULL DEFAULT '',
                    supporting_events TEXT    NOT NULL DEFAULT '[]',
                    confidence        TEXT    NOT NULL DEFAULT '中',
                    created_at        TEXT    NOT NULL DEFAULT ''
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_patterns_category ON patterns(category)"
            )

            conn.commit()
            conn.close()
            logger.info("数据库表初始化完成（WAL 模式）。")

    # ---------- 去重检查 ----------

    def message_exists(self, user_id: str, content: str, timestamp: str) -> bool:
        """
        检查消息是否已存在（基于 user_id + content + timestamp 联合去重）。
        用于批量导入时避免重复插入。
        """
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM messages WHERE user_id = ? AND content = ? AND timestamp = ? LIMIT 1",
                (user_id, content, timestamp),
            )
            exists = cur.fetchone() is not None
            conn.close()
        return exists

    # ---------- 核心分析管线 ----------

    def add_message_analysis(
        self,
        user_id: str,
        user_name: str,
        content: str,
        keep_content: bool = True,
        custom_timestamp: Optional[str] = None,
    ) -> Optional[int]:
        """
        完整分析管线：清洗 → 存 messages → 提取关键词 → 存 keywords
        → 提取事件 → 存 events。
        
        参数:
            user_id:          发送者 ID（QQ号等）
            user_name:        发送者昵称
            content:          原始消息文本
            keep_content:     是否保留原文。False 时 content 存空字符串，
                              仅保留关键词和事件索引（用于分级存储）。
            custom_timestamp: 自定义时间戳（YYYY-MM-DD HH:MM:SS）。
                              用于批量导入历史消息时保留原始时间。
                              为 None 则使用当前时间。
        
        返回:
            msg_id (int) — 成功时返回消息ID；消息被清洗过滤时返回 None。
        """
        cleaned = clean_message(content)
        if cleaned is False:
            logger.debug(f"消息被过滤: [{user_name}] {content[:40]}")
            return None

        timestamp = custom_timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        stored_content = cleaned if keep_content else ""

        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA foreign_keys=ON")
            cur = conn.cursor()

            # Step 1: 存入 messages
            cur.execute(
                "INSERT INTO messages (user_id, user_name, content, timestamp) "
                "VALUES (?, ?, ?, ?)",
                (user_id, user_name, stored_content, timestamp),
            )
            msg_id = cur.lastrowid
            conn.commit()

            # Step 2: 提取关键词（LLM 调用在锁内但可接受，批量场景用外部连接）
            keywords = extract_keywords(cleaned)
            if keywords:
                self._add_keywords_internal(cur, msg_id, keywords)

            # Step 3: 提取事件
            event = extract_event(cleaned)
            if event:
                self._add_event_internal(cur, msg_id, user_name, timestamp, event)

            conn.commit()
            conn.close()

        logger.info(
            f"[msg_id={msg_id}] 分析完成: {user_name} | "
            f"keep_content={keep_content} | "
            f"关键词={keywords} | 事件={'有' if event else '无'}"
        )
        return msg_id

    def _add_keywords_internal(
        self, cur: sqlite3.Cursor, msg_id: int, keywords: List[str]
    ) -> None:
        """内部方法：批量插入关键词（INSERT OR IGNORE 防止重复）。"""
        cur.executemany(
            "INSERT OR IGNORE INTO keywords (keyword, msg_id) VALUES (?, ?)",
            [(kw, msg_id) for kw in keywords],
        )

    def _add_event_internal(
        self,
        cur: sqlite3.Cursor,
        msg_id: int,
        user_name: str,
        timestamp: str,
        event: Dict[str, Any],
    ) -> None:
        """内部方法：插入事件（调用方需持有锁）。"""
        cur.execute(
            "INSERT INTO events (timestamp, user_name, subject, verb, object, summary, msg_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                timestamp,
                user_name,
                event.get("subject", ""),
                event.get("verb", ""),
                event.get("object", ""),
                event.get("summary", ""),
                msg_id,
            ),
        )

    # ---------- 公开写入方法 ----------

    def add_summarized_message(
        self,
        content: str,
        timestamp: str,
        keywords_list: List[str],
        user_name: str = "system",
    ) -> Optional[int]:
        """
        直接存储一条已总结的消息（不调用 LLM 提取关键词/事件）。
        用于批量导入时存储合并后的摘要。
        
        返回:
            msg_id — 成功时返回消息ID
        """
        cleaned = clean_message(content)
        if cleaned is False:
            return None

        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA foreign_keys=ON")
            cur = conn.cursor()

            cur.execute(
                "INSERT INTO messages (user_id, user_name, content, timestamp, content_state) "
                "VALUES (?, ?, ?, ?, ?)",
                ("system", user_name, cleaned, timestamp, "merged"),
            )
            msg_id = cur.lastrowid

            if keywords_list:
                self._add_keywords_internal(cur, msg_id, keywords_list)

            conn.commit()
            conn.close()

        logger.info(
            f"[msg_id={msg_id}] 总结消息存储: {cleaned[:50]} | 关键词={keywords_list}"
        )
        return msg_id

    def add_keywords(self, msg_id: int, keywords_list: List[str]) -> None:
        """为指定消息添加关键词。"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            self._add_keywords_internal(cur, msg_id, keywords_list)
            conn.commit()
            conn.close()

    def clean_orphan_keywords(self) -> int:
        """
        删除关联消息已清除（content_state='cleared'）的孤儿关键词。
        返回删除的行数。
        """
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM keywords WHERE msg_id IN "
                "(SELECT id FROM messages WHERE content_state = 'cleared')"
            )
            deleted = cur.rowcount
            conn.commit()
            conn.close()
        if deleted > 0:
            logger.info(f"清理孤儿关键词: {deleted} 条")
        return deleted

    def add_event(self, msg_id: int, user_name: str, event_dict: Dict[str, Any]) -> None:
        """为指定消息添加事件。"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            self._add_event_internal(cur, msg_id, user_name, timestamp, event_dict)
            conn.commit()
            conn.close()

    def log_retrieval(self, msg_ids: List[int], query_text: str) -> None:
        """
        记录检索命中日志。
        每次主程序检索记忆并命中某条消息时调用，
        用于后续 QDP（Query-Driven Pruning）评分。
        """
        if not msg_ids:
            return
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.executemany(
                "INSERT INTO retrieval_log (msg_id, query_text, timestamp) VALUES (?, ?, ?)",
                [(mid, query_text, timestamp) for mid in msg_ids],
            )
            conn.commit()
            conn.close()
        logger.debug(f"检索日志: {len(msg_ids)} 条命中记录")

    # ---------- 重要性评分 ----------

    def calculate_importance(self, msg_id: int) -> float:
        """
        计算单条消息的重要性得分。
        
        算法（FadeMem + Intelligent Decay + QDP）：
          score = 0.40 * time_score + 0.35 * density_score + 0.25 * retrieval_score
        
        1. 时效分 time_score = exp(-λ * days_elapsed)
           来源：Intelligent Decay 论文思路，越旧的消息权重越低。
        
        2. 信息密度分 density_score：
           - 消息长度归一化（/max_content_len，上限1.0）
           - 关键词数归一化（/max_keywords，上限1.0）
           - 是否有事件（0 或 0.3 加成）
           - 取三者均值
        
        3. 检索命中分 retrieval_score：
           从 retrieval_log 统计命中次数，按指数衰减加权：
             Σ exp(-λ_retrieval * days_since_hit)
           归一化到 [0, 1]（以 5 次近期命中为满分基准）。
        
        返回:
            float — [0.0, 1.0] 区间的重要性得分。
        """
        cfg = DEFAULT_MAINTENANCE_CONFIG
        w_time = cfg["weight_time"]
        w_density = cfg["weight_density"]
        w_retrieval = cfg["weight_retrieval"]
        lam = cfg["lambda_decay"]
        lam_ret = cfg["retrieval_decay_lambda"]
        max_len = cfg["max_content_len"]
        max_kw = cfg["max_keywords"]

        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            # --- 获取消息基本信息 ---
            cur.execute(
                "SELECT content, timestamp FROM messages WHERE id = ?", (msg_id,)
            )
            row = cur.fetchone()
            if not row:
                conn.close()
                return 0.0

            content, ts = row
            content = content or ""

            # 计算天数
            try:
                msg_dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                msg_dt = datetime.now()
            days_elapsed = max(0, (datetime.now() - msg_dt).total_seconds() / 86400)

            # --- 1. 时效分 ---
            time_score = math.exp(-lam * days_elapsed)

            # --- 2. 信息密度分 ---
            len_score = min(len(content) / max_len, 1.0)

            cur.execute("SELECT COUNT(*) FROM keywords WHERE msg_id = ?", (msg_id,))
            kw_count = cur.fetchone()[0]
            kw_score = min(kw_count / max_kw, 1.0)

            cur.execute("SELECT COUNT(*) FROM events WHERE msg_id = ?", (msg_id,))
            has_event = 1 if cur.fetchone()[0] > 0 else 0
            event_bonus = 0.3 * has_event

            density_score = min((len_score + kw_score) / 2 + event_bonus, 1.0)

            # --- 3. 检索命中分 ---
            cur.execute(
                "SELECT timestamp FROM retrieval_log WHERE msg_id = ?", (msg_id,)
            )
            hit_timestamps = [r[0] for r in cur.fetchall()]
            conn.close()

            retrieval_score = 0.0
            now = datetime.now()
            for ht in hit_timestamps:
                try:
                    hit_dt = datetime.strptime(ht[:19], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                hit_days = max(0, (now - hit_dt).total_seconds() / 86400)
                retrieval_score += math.exp(-lam_ret * hit_days)

            # 归一化：5 次近期命中 ≈ 1.0
            retrieval_score = min(retrieval_score / 5.0, 1.0)

        score = w_time * time_score + w_density * density_score + w_retrieval * retrieval_score
        score = round(min(max(score, 0.0), 1.0), 4)

        # 回写 importance_score
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "UPDATE messages SET importance_score = ? WHERE id = ?",
                (score, msg_id),
            )
            conn.commit()
            conn.close()

        return score

    # ---------- 时段合并 ----------

    def merge_time_windows(self, config: Dict[str, Any] = None) -> Dict[str, int]:
        """
        执行时段合并（固定时间格子划分）
        
        时间划分方式：
          - Tier 1（第一层，<72小时）：30分钟为一个格子，从 00:00 到 23:30 共 48 个格子
          - Tier 2（第二层，3-7天）：12小时为一个格子，从 00:00 到 12:00 共 2 个格子
        
        合并策略：
          1. 将每个格子内的所有消息调用 LLM 合并为一句话 + 关键词
          2. 创建新的合并消息（content = 概括，content_state = "merged"）
          3. 原格子内的消息清空 content（保留关键词/事件，供检索）
        
        参数:
            config: 可选配置字典
        
        返回:
            统计字典
        """
        cfg = {**DEFAULT_MAINTENANCE_CONFIG, **(config or {})}
        if not cfg.get("merge_enabled", True):
            logger.info("时段合并已禁用，跳过。")
            return {"tier1_windows": 0, "tier1_merged": 0, "tier2_windows": 0, "tier2_merged": 0}
        
        tier1_hours = cfg["tier1_hours"]
        tier2_days = cfg["tier2_days"]
        tier1_merge_minutes = cfg["tier1_merge_minutes"]
        tier2_merge_hours = cfg["tier2_merge_hours"]
        
        now = datetime.now()
        stats = {
            "tier1_windows": 0,
            "tier1_merged": 0,
            "tier2_windows": 0,
            "tier2_merged": 0,
        }
        
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            
            # --- Tier 1：<72小时，每 30 分钟合并 ---
            cutoff_tier1 = (now - timedelta(hours=tier1_hours)).strftime("%Y-%m-%d %H:%M:%S")
            cutoff_tier2 = (now - timedelta(days=tier2_days)).strftime("%Y-%m-%d %H:%M:%S")
            
            # 获取 Tier 1 和 Tier 2 的所有消息（content 不为空且不是 merged）
            cur.execute("""
                SELECT id, user_id, user_name, content, timestamp 
                FROM messages 
                WHERE timestamp >= ? 
                  AND content != '' 
                  AND content_state != 'merged'
                  AND content_state != 'cleared'
                ORDER BY timestamp
            """, (cutoff_tier2,))
            all_rows = cur.fetchall()
            
            # 分组：按窗口
            tier1_windows: Dict[str, List[Dict[str, Any]]] = {}
            tier2_windows: Dict[str, List[Dict[str, Any]]] = {}
            
            for row in all_rows:
                msg_id, user_id, user_name, content, ts = row
                msg_data = {"id": msg_id, "user_id": user_id, "user_name": user_name, "content": content, "timestamp": ts}
                
                try:
                    msg_dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                
                hours_elapsed = (now - msg_dt).total_seconds() / 3600.0
                days_elapsed = (now - msg_dt).days
                
                # Tier 1：<72小时，每 30 分钟一个窗口
                if hours_elapsed < tier1_hours:
                    # 窗口键：向下取整到最近的 30 分钟
                    window_dt = msg_dt.replace(minute=(msg_dt.minute // tier1_merge_minutes) * tier1_merge_minutes, second=0, microsecond=0)
                    window_key = f"t1_{window_dt.strftime('%Y%m%d_%H%M')}"
                    if window_key not in tier1_windows:
                        tier1_windows[window_key] = []
                    tier1_windows[window_key].append(msg_data)
                # Tier 2：3-7天，每 12 小时一个窗口
                elif days_elapsed < tier2_days:
                    # 窗口键：向下取整到最近的 12 小时
                    hour = (msg_dt.hour // tier2_merge_hours) * tier2_merge_hours
                    window_dt = msg_dt.replace(hour=hour, minute=0, second=0, microsecond=0)
                    window_key = f"t2_{window_dt.strftime('%Y%m%d_%H')}"
                    if window_key not in tier2_windows:
                        tier2_windows[window_key] = []
                    tier2_windows[window_key].append(msg_data)
            
            # --- 处理 Tier 1 窗口 ---
            for window_key, msgs in tier1_windows.items():
                if len(msgs) <= 1:
                    continue  # 窗口内只有一条消息，无需合并
                stats["tier1_windows"] += 1
                
                # 提取窗口时间（从 key）
                _, time_str = window_key.split("_", 1)
                window_dt = datetime.strptime(time_str, "%Y%m%d_%H%M")
                window_ts = window_dt.strftime("%Y-%m-%d %H:%M:%S")
                
                # 合并消息
                merge_input = [{"user_name": m["user_name"], "content": m["content"], "timestamp": m["timestamp"]} for m in msgs]
                merge_result = merge_messages(merge_input)
                summary = merge_result["summary"]
                keywords = merge_result["keywords"]
                
                if not summary:
                    continue
                
                # 1. 创建合并消息
                cur.execute("""
                    INSERT INTO messages (user_id, user_name, content, timestamp, content_state, last_maintained)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, ("system", "system", summary, window_ts, "merged", now.strftime("%Y-%m-%d %H:%M:%S")))
                merged_msg_id = cur.lastrowid
                
                # 2. 添加关键词到合并消息
                if keywords:
                    cur.executemany("""
                        INSERT INTO keywords (keyword, msg_id) VALUES (?, ?)
                    """, [(kw, merged_msg_id) for kw in keywords])
                
                # 3. 原时段内的消息清空 content（保留关键词/事件）
                msg_ids = [m["id"] for m in msgs]
                cur.executemany("""
                    UPDATE messages SET content = '', content_state = 'cleared', last_maintained = ?
                    WHERE id = ?
                """, [(now.strftime("%Y-%m-%d %H:%M:%S"), mid) for mid in msg_ids])
                
                stats["tier1_merged"] += 1
                logger.debug(f"Tier 1 合并窗口 {window_key}：{len(msgs)} 条 → 1 条")
            
            # --- 处理 Tier 2 窗口 ---
            for window_key, msgs in tier2_windows.items():
                if len(msgs) <= 1:
                    continue
                stats["tier2_windows"] += 1
                
                _, time_str = window_key.split("_", 1)
                window_dt = datetime.strptime(time_str, "%Y%m%d_%H")
                window_ts = window_dt.strftime("%Y-%m-%d %H:%M:%S")
                
                merge_input = [{"user_name": m["user_name"], "content": m["content"], "timestamp": m["timestamp"]} for m in msgs]
                merge_result = merge_messages(merge_input)
                summary = merge_result["summary"]
                keywords = merge_result["keywords"]
                
                if not summary:
                    continue
                
                cur.execute("""
                    INSERT INTO messages (user_id, user_name, content, timestamp, content_state, last_maintained)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, ("system", "system", summary, window_ts, "merged", now.strftime("%Y-%m-%d %H:%M:%S")))
                merged_msg_id = cur.lastrowid
                
                if keywords:
                    cur.executemany("""
                        INSERT INTO keywords (keyword, msg_id) VALUES (?, ?)
                    """, [(kw, merged_msg_id) for kw in keywords])
                
                msg_ids = [m["id"] for m in msgs]
                cur.executemany("""
                    UPDATE messages SET content = '', content_state = 'cleared', last_maintained = ?
                    WHERE id = ?
                """, [(now.strftime("%Y-%m-%d %H:%M:%S"), mid) for mid in msg_ids])
                
                stats["tier2_merged"] += 1
                logger.debug(f"Tier 2 合并窗口 {window_key}：{len(msgs)} 条 → 1 条")
            
            conn.commit()
            conn.close()
        
        logger.info(f"时段合并完成：Tier 1 {stats['tier1_merged']}/{stats['tier1_windows']} 窗口，Tier 2 {stats['tier2_merged']}/{stats['tier2_windows']} 窗口")
        return stats

    # ---------- 记忆迭代清理 ----------

    def maintenance(self, config: Dict[str, Any] = None) -> str:
        """
        执行记忆迭代清理（时段合并 + 三层分级 + 多因素评分 + 检索日志清理）。
        设计为可被定时任务（如每天凌晨）调用。
        
        执行顺序：
          1. 时段合并（Tier 1：每30分钟；Tier 2：每12小时）
          2. 清理 retrieval_log
          3. 消息分层处理
          4. VACUUM
        
        参数:
            config: 可选配置字典，覆盖 DEFAULT_MAINTENANCE_CONFIG。
        
        返回:
            str — 清理报告（含统计信息）。
        """
        cfg = {**DEFAULT_MAINTENANCE_CONFIG, **(config or {})}
        tier1_hours = cfg["tier1_hours"]
        tier2_days = cfg["tier2_days"]
        keep_th = cfg["keep_threshold"]
        comp_th = cfg["compress_threshold"]
        retrieval_keep_days = cfg["retrieval_log_keep_days"]

        now = datetime.now()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")

        # 1. 时段合并
        merge_stats = self.merge_time_windows(config)

        stats = {
            "total_checked": 0,
            "tier1_skipped": 0,
            "tier2_kept": 0,
            "tier2_compressed": 0,
            "tier2_cleared": 0,
            "tier3_cleared": 0,
            "retrieval_log_deleted": 0,
            "tier1_windows": merge_stats["tier1_windows"],
            "tier1_merged": merge_stats["tier1_merged"],
            "tier2_windows": merge_stats["tier2_windows"],
            "tier2_merged": merge_stats["tier2_merged"],
        }

        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            # 1. 清理 retrieval_log
            cutoff_retrieval = (now - timedelta(days=retrieval_keep_days)).strftime("%Y-%m-%d %H:%M:%S")
            cur.execute("DELETE FROM retrieval_log WHERE timestamp < ?", (cutoff_retrieval,))
            stats["retrieval_log_deleted"] = cur.rowcount

            # 2. 处理消息分层
            cur.execute(
                "SELECT id, content, timestamp, content_state FROM messages "
                "WHERE content_state != 'cleared' OR compressed_content IS NOT NULL"
            )
            rows = cur.fetchall()

            for row in rows:
                msg_id, content, ts, state = row
                stats["total_checked"] += 1

                try:
                    msg_dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                
                hours_elapsed = (now - msg_dt).total_seconds() / 3600.0
                days_elapsed = (now - msg_dt).days

                # Tier 1: <72 小时（<3 天），不处理
                if hours_elapsed < tier1_hours:
                    stats["tier1_skipped"] += 1
                    continue

                # Tier 2: 3-7 天
                if days_elapsed < tier2_days:
                    importance = self.calculate_importance(msg_id)
                    if importance >= keep_th:
                        stats["tier2_kept"] += 1
                        cur.execute(
                            "UPDATE messages SET last_maintained = ? WHERE id = ?",
                            (now_str, msg_id),
                        )
                    elif importance >= comp_th:
                        stats["tier2_compressed"] += 1
                        compressed = compress_message(content or "")
                        cur.execute(
                            "UPDATE messages SET compressed_content = ?, "
                            "content_state = 'compressed', last_maintained = ? "
                            "WHERE id = ?",
                            (compressed, now_str, msg_id),
                        )
                    else:
                        stats["tier2_cleared"] += 1
                        cur.execute(
                            "UPDATE messages SET content = '', "
                            "content_state = 'cleared', last_maintained = ? "
                            "WHERE id = ?",
                            (now_str, msg_id),
                        )
                    continue

                # Tier 3: ≥7 天，完全清空（原文 + 压缩摘要）
                stats["tier3_cleared"] += 1
                cur.execute(
                    "UPDATE messages SET content = '', compressed_content = NULL, "
                    "content_state = 'cleared', last_maintained = ? WHERE id = ?",
                    (now_str, msg_id),
                )

            conn.commit()
            conn.close()

        # 3. VACUUM 压缩数据库文件
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("VACUUM")
            conn.commit()
            conn.close()

        report = (
            f"====== 记忆维护报告 ({now_str}) ======\n"
            f"  时段合并:\n"
            f"    Tier1 (30min):    {stats['tier1_merged']}/{stats['tier1_windows']} 窗口\n"
            f"    Tier2 (12h):       {stats['tier2_merged']}/{stats['tier2_windows']} 窗口\n"
            f"  检查消息总数:       {stats['total_checked']}\n"
            f"  Tier1 跳过 (<72h):  {stats['tier1_skipped']}\n"
            f"  Tier2 保留原文:      {stats['tier2_kept']}\n"
            f"  Tier2 压缩为摘要:    {stats['tier2_compressed']}\n"
            f"  Tier2 清空原文:      {stats['tier2_cleared']}\n"
            f"  Tier3 完全清空:      {stats['tier3_cleared']}\n"
            f"  清理检索日志:       {stats['retrieval_log_deleted']} 条\n"
            f"  执行 VACUUM:         是\n"
            f"========================================"
        )
        logger.info(report)
        return report

    # ---------- 季度事件合并 ----------

    def merge_events_quarterly(self) -> str:
        """
        季度事件合并：将过去 ~90 天的事件按周合并为一条总结。
        每周生成一条合并事件，存入 events 表（msg_id = -1 标记为合并事件）。
        
        返回:
            str — 合并报告。
        """
        cfg = DEFAULT_MAINTENANCE_CONFIG
        if not cfg.get("merge_events_enabled", True):
            return "季度合并已禁用。"

        cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")

        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            # 获取近90天的事件（排除已合并的）
            cur.execute(
                "SELECT id, timestamp, user_name, summary FROM events "
                "WHERE timestamp >= ? AND msg_id != -1 ORDER BY timestamp ASC",
                (cutoff,),
            )
            rows = cur.fetchall()
            conn.close()

        if not rows:
            return "季度合并：近90天无事件可合并。"

        # 按周分组
        weeks: Dict[str, List[Dict[str, str]]] = {}
        for row in rows:
            ev_id, ts, uname, summary = row
            try:
                dt = datetime.strptime(ts[:10], "%Y-%m-%d")
            except ValueError:
                continue
            week_key = (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")
            if week_key not in weeks:
                weeks[week_key] = []
            weeks[week_key].append({"user_name": uname, "summary": summary})

        merged_count = 0
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            for week_start, events in weeks.items():
                if len(events) <= 1:
                    continue

                # 用 LLM 合并该周事件
                events_text = "\n".join(
                    f"- {e['user_name']}: {e['summary']}" for e in events
                )
                system = "你是一个事件摘要器。直接输出一句话总结，不要思考，不要解释。"
                user = f"将以下一周的群聊事件合并为一句话总结：\n{events_text}"
                try:
                    merged_summary = _call_llm_api(system, user)
                except Exception:
                    merged_summary = f"本周共 {len(events)} 条事件"

                cur.execute(
                    "INSERT INTO events (timestamp, user_name, subject, verb, object, summary, msg_id) "
                    "VALUES (?, 'SYSTEM', '', '', '', ?, -1)",
                    (week_start, merged_summary),
                )
                merged_count += 1

            conn.commit()
            conn.close()

        report = (
            f"====== 季度事件合并报告 ======\n"
            f"  原始事件: {len(rows)} 条\n"
            f"  按周分组: {len(weeks)} 周\n"
            f"  合并生成: {merged_count} 条\n"
            f"================================"
        )
        logger.info(report)
        return report

    # ---------- 关键词检索 ----------

    def search_by_keyword(
        self,
        keywords: List[str],
        limit: int = 10,
        include_merged: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        通过关键词检索消息（支持快速定位聊天区块）。
        
        参数:
            keywords:       关键词列表
            limit:          返回条数上限
            include_merged: 是否包含合并/总结消息
        
        返回:
            [{"msg_id": ..., "content": ..., "user_name": ..., "timestamp": ..., "keywords": [...]}, ...]
        """
        if not keywords:
            return []
        
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            
            placeholders = ",".join("?" for _ in keywords)
            
            if include_merged:
                cur.execute(
                    f"SELECT DISTINCT m.id, m.content, m.user_name, m.timestamp, m.content_state "
                    f"FROM messages m "
                    f"INNER JOIN keywords k ON k.msg_id = m.id "
                    f"WHERE k.keyword IN ({placeholders}) "
                    f"AND m.content_state != 'cleared' AND m.content != '' "
                    f"ORDER BY m.timestamp DESC LIMIT ?",
                    keywords + [limit],
                )
            else:
                cur.execute(
                    f"SELECT DISTINCT m.id, m.content, m.user_name, m.timestamp, m.content_state "
                    f"FROM messages m "
                    f"INNER JOIN keywords k ON k.msg_id = m.id "
                    f"WHERE k.keyword IN ({placeholders}) "
                    f"AND m.content_state = 'original' AND m.content != '' "
                    f"ORDER BY m.timestamp DESC LIMIT ?",
                    keywords + [limit],
                )
            
            rows = cur.fetchall()
            
            results = []
            for row in rows:
                msg_id = row["id"]
                cur.execute(
                    "SELECT keyword FROM keywords WHERE msg_id = ?", (msg_id,)
                )
                kw_list = [r[0] for r in cur.fetchall()]
                
                results.append({
                    "msg_id": msg_id,
                    "content": row["content"],
                    "user_name": row["user_name"],
                    "timestamp": row["timestamp"],
                    "content_state": row["content_state"],
                    "keywords": kw_list,
                })
            
            conn.close()
        
        logger.info(
            f"关键词检索: {keywords} → {len(results)} 条结果"
        )
        return results

    def search_by_time_range(
        self,
        start_time: str,
        end_time: str,
        limit: int = 10,
        include_merged: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        通过时间范围检索消息（支持快速定位聊天区块）。
        
        参数:
            start_time:     开始时间 (YYYY-MM-DD HH:MM:SS)
            end_time:       结束时间 (YYYY-MM-DD HH:MM:SS)
            limit:          返回条数上限
            include_merged: 是否包含合并/总结消息
        
        返回:
            [{"msg_id": ..., "content": ..., "user_name": ..., "timestamp": ..., "keywords": [...]}, ...]
        """
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            
            if include_merged:
                cur.execute(
                    "SELECT DISTINCT m.id, m.content, m.user_name, m.timestamp, m.content_state "
                    "FROM messages m "
                    "WHERE m.timestamp >= ? AND m.timestamp <= ? "
                    "AND m.content_state != 'cleared' AND m.content != '' "
                    "ORDER BY m.timestamp DESC LIMIT ?",
                    (start_time, end_time, limit),
                )
            else:
                cur.execute(
                    "SELECT DISTINCT m.id, m.content, m.user_name, m.timestamp, m.content_state "
                    "FROM messages m "
                    "WHERE m.timestamp >= ? AND m.timestamp <= ? "
                    "AND m.content_state = 'original' AND m.content != '' "
                    "ORDER BY m.timestamp DESC LIMIT ?",
                    (start_time, end_time, limit),
                )
            
            rows = cur.fetchall()
            
            results = []
            for row in rows:
                msg_id = row["id"]
                cur.execute(
                    "SELECT keyword FROM keywords WHERE msg_id = ?", (msg_id,)
                )
                kw_list = [r[0] for r in cur.fetchall()]
                
                results.append({
                    "msg_id": msg_id,
                    "content": row["content"],
                    "user_name": row["user_name"],
                    "timestamp": row["timestamp"],
                    "content_state": row["content_state"],
                    "keywords": kw_list,
                })
            
            conn.close()
        
        logger.info(
            f"时间范围检索: {start_time} ~ {end_time} → {len(results)} 条结果"
        )
        return results

    def get_time_block_context(
        self,
        msg_id: int,
    ) -> Dict[str, Any]:
        """
        根据消息ID获取其所在时间块的信息（用于快速定位聊天上下文）。
        
        返回:
            {"msg_id": ..., "content": ..., "timestamp": ..., 
             "same_block_messages": [...], "block_time_range": (start, end)}
        """
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            
            cur.execute(
                "SELECT id, content, user_name, timestamp, content_state "
                "FROM messages WHERE id = ?",
                (msg_id,),
            )
            target = cur.fetchone()
            if not target:
                conn.close()
                return {}
            
            ts = target["timestamp"]
            try:
                dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                conn.close()
                return {
                    "msg_id": msg_id,
                    "content": target["content"],
                    "timestamp": ts,
                    "same_block_messages": [],
                    "block_time_range": (ts, ts),
                }
            
            days_elapsed = (datetime.now() - dt).days
            
            if days_elapsed <= 2:
                block_start = dt.replace(minute=(dt.minute // 30) * 30, second=0, microsecond=0)
                block_end = block_start + timedelta(minutes=30)
            elif days_elapsed <= 6:
                hour = (dt.hour // 12) * 12
                block_start = dt.replace(hour=hour, minute=0, second=0, microsecond=0)
                block_end = block_start + timedelta(hours=12)
            else:
                block_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
                block_end = block_start + timedelta(days=1)
            
            cur.execute(
                "SELECT id, content, user_name, timestamp, content_state "
                "FROM messages "
                "WHERE timestamp >= ? AND timestamp < ? AND content != '' "
                "ORDER BY timestamp",
                (
                    block_start.strftime("%Y-%m-%d %H:%M:%S"),
                    block_end.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            block_msgs = [
                {
                    "msg_id": r["id"],
                    "content": r["content"],
                    "user_name": r["user_name"],
                    "timestamp": r["timestamp"],
                    "content_state": r["content_state"],
                }
                for r in cur.fetchall()
            ]
            
            conn.close()
        
        return {
            "msg_id": msg_id,
            "content": target["content"],
            "user_name": target["user_name"],
            "timestamp": ts,
            "content_state": target["content_state"],
            "same_block_messages": block_msgs,
            "block_time_range": (
                block_start.strftime("%Y-%m-%d %H:%M:%S"),
                block_end.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        }

    # ---------- 统计 ----------

    def get_memory_stats(self) -> Dict[str, Any]:
        """
        获取记忆库统计信息。
        
        返回:
            dict — 包含各表行数、状态分布、存储估算等。
        """
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            cur.execute("SELECT COUNT(*) FROM messages")
            total_msgs = cur.fetchone()[0]

            cur.execute(
                "SELECT content_state, COUNT(*) FROM messages GROUP BY content_state"
            )
            state_dist = dict(cur.fetchall())

            cur.execute("SELECT COUNT(*) FROM keywords")
            total_kw = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM events")
            total_ev = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM retrieval_log")
            total_ret = cur.fetchone()[0]

            # 估算存储大小
            cur.execute("SELECT SUM(LENGTH(content) + LENGTH(COALESCE(compressed_content,''))) FROM messages")
            msg_bytes = cur.fetchone()[0] or 0

            cur.execute("SELECT AVG(importance_score) FROM messages")
            avg_imp = cur.fetchone()[0] or 0

            conn.close()

        return {
            "total_messages": total_msgs,
            "content_state_distribution": state_dist,
            "total_keywords": total_kw,
            "total_events": total_ev,
            "total_retrieval_logs": total_ret,
            "estimated_content_bytes": msg_bytes,
            "estimated_content_mb": round(msg_bytes / (1024 * 1024), 2),
            "average_importance": round(avg_imp, 4),
        }


# ============================================================
# 命令行测试入口
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  群聊记忆管理AI · 独立测试")
    print("=" * 60)

    db = MemoryDB()

    # ---- 测试 1: 消息清洗 ----
    print("\n>>> [1] clean_message() 测试")
    test_cases = [
        ("你好，今天天气不错", "正常消息"),
        ("入机", "正常消息"),
        ("😂😂😂", "纯表情"),
        ("好的", "无意义词"),
        ("收到！", "无意义词"),
        ("+1", "无意义词"),
        ("   ", "空白"),
        ("今天开会讨论了新项目方案", "正常消息"),
        ("哈哈", "无意义词"),
        ("(╯°□°)╯︵ ┻━┻", "颜文字"),
    ]
    for text, desc in test_cases:
        result = clean_message(text)
        status = f"保留 → {result}" if result else "过滤 ✗"
        print(f"  [{desc}] \"{text[:20]}\" → {status}")

    # ---- 测试 2: 添加消息分析 ----
    print("\n>>> [2] add_message_analysis() 测试")
    test_msgs = [
        ("user_001", "张三", "今天下午3点开会讨论新项目方案"),
        ("user_002", "李四", "好的"),
        ("user_003", "王五", "😂😂😂"),
        ("user_001", "张三", "周末一起去爬山吧"),
    ]
    for uid, uname, content in test_msgs:
        msg_id = db.add_message_analysis(uid, uname, content)
        if msg_id:
            print(f"  ✓ [{uname}] msg_id={msg_id}: {content[:30]}")
        else:
            print(f"  ✗ [{uname}] 消息被过滤: {content[:30]}")

    # ---- 测试 3: 检索日志 ----
    print("\n>>> [3] log_retrieval() 测试")
    db.log_retrieval([1, 3], "开会")
    print("  已记录检索日志。")

    # ---- 测试 4: 重要性评分 ----
    print("\n>>> [4] calculate_importance() 测试")
    for mid in [1, 3]:
        score = db.calculate_importance(mid)
        print(f"  msg_id={mid}: importance = {score}")

    # ---- 测试 5: 统计 ----
    print("\n>>> [5] get_memory_stats()")
    stats = db.get_memory_stats()
    for k, v in stats.items():
        print(f"  {k}: {v}")

    # ---- 测试 6: 维护清理 ----
    print("\n>>> [6] maintenance()")
    report = db.maintenance()
    print(report)

    # ---- 测试 7: 季度合并 ----
    print("\n>>> [7] merge_events_quarterly()")
    merge_report = db.merge_events_quarterly()
    print(merge_report)

    print("\n" + "=" * 60)
    print("  测试完毕")
    print("=" * 60)
