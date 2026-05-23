"""
auto_reply.py — 群聊自动回复引擎（四步流水线）
==============================================

流程：
  Step1: 存消息到 Tier0 → LLM 判断是否回复（@提及直接回复）
  Step2: LLM 判断是否需要历史记忆（输出 1 或 0）
  Step3: 分支1(查记忆库+chat_analyze回复) / 分支0(仅chat_analyze回复)
  Step4: 回复消息存回 Tier0

依赖：memory_ai.py, chat_analyze 文件
"""

import os
import json
import time
import logging
import random
import re
import requests
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

from memory_ai import (
    MemoryDB,
    extract_keywords,
    clean_message,
    _strip_thinking_process,
    extract_binary_decision,
    MEMORY_DB_PATH as _MEMORY_DB_PATH,
)

# ============================================================
# 日志
# ============================================================
if not logging.getLogger("auto_reply").handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
logger = logging.getLogger("auto_reply")

# ============================================================
# 配置
# ============================================================
try:
    from config import LLM_API_URL, LLM_MODEL_NAME, LLM_API_KEY, BOT_NAME
except ImportError:
    LLM_API_URL = "http://127.0.0.1:1234/v1/chat/completions"
    LLM_MODEL_NAME = "qwen3.5-9b"
    LLM_API_KEY = ""
    BOT_NAME = "A"

LLM_TIMEOUT = 60
LLM_MAX_RETRIES = 2

BOT_QQ = ""

CHAT_ANALYZE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "chat_analyze"
)
MEMORY_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "memory_cache.json"
)


# ============================================================
# LLM 调用
# ============================================================

def _call_llm(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 8192,
    temperature: float = 0.01,
) -> str:
    """调用 LLM，返回纯文本。content（最终答案）优先，reasoning_content（思考）兜底。"""
    payload = {
        "model": LLM_MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"

    for attempt in range(LLM_MAX_RETRIES + 1):
        try:
            resp = requests.post(
                LLM_API_URL, json=payload, headers=headers, timeout=LLM_TIMEOUT, verify=False
            )
            resp.raise_for_status()
            data = resp.json()
            message = data["choices"][0]["message"]
            content = message.get("content", "").strip()
            if not content:
                content = message.get("reasoning_content", "").strip()
            if content:
                logger.debug(f"[_call_llm] content={content[:200]}")
                return content
        except Exception as e:
            if attempt >= LLM_MAX_RETRIES:
                raise
            time.sleep(1.5 ** attempt)
    return ""


# ============================================================
# 工具函数
# ============================================================

def _is_mentioned(content: str) -> bool:
    """检查消息是否 @ 了机器人。"""
    if not content:
        return False
    if BOT_QQ and BOT_QQ in content:
        return True
    if BOT_NAME and BOT_NAME in content:
        return True
    return False


def _load_chat_analyze() -> str:
    """读取 chat_analyze 文件内容。"""
    if not os.path.exists(CHAT_ANALYZE_FILE):
        return ""
    try:
        with open(CHAT_ANALYZE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def _save_memory_cache(keywords: List[str], references: List[Dict[str, Any]]) -> None:
    """将检索到的参考记忆写入缓存文件。"""
    cache = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "keywords": keywords,
        "references": references,
    }
    try:
        with open(MEMORY_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"写入 memory_cache 失败: {e}")


def _query_memory_by_keywords(
    db: MemoryDB, keywords: List[str], content: str, per_keyword: int = 3
) -> List[Dict[str, Any]]:
    """
    对每个关键词检索记忆库，同时尝试时间范围检索。
    如果识别到时间范围，先在时间范围内搜关键词，再全库搜。
    去重后按时间倒序返回。
    """
    from memory_ai import parse_time_keywords
    
    seen_ids = set()
    results: List[Dict[str, Any]] = []
    
    # 先尝试时间范围检索
    time_range = parse_time_keywords(content)
    if time_range:
        start_time, end_time = time_range
        logger.info(f"[_query_memory] 识别到时间范围: {start_time} ~ {end_time}")
        
        # 如果有时间范围和关键词，先在时间范围内搜关键词
        if keywords:
            # 先获取时间范围内的所有消息
            time_hits = db.search_by_time_range(
                start_time, end_time, limit=50, include_merged=True
            )
            # 再在时间范围内的消息里匹配关键词
            for hit in time_hits:
                # 检查关键词是否在内容里
                content_match = any(
                    kw.lower() in hit["content"].lower() for kw in keywords
                )
                # 检查关键词是否在 keywords 字段里
                keyword_match = any(
                    kw.lower() in " ".join(hit.get("keywords", [])).lower() for kw in keywords
                )
                if content_match or keyword_match:
                    if hit["msg_id"] not in seen_ids:
                        seen_ids.add(hit["msg_id"])
                        results.append(hit)
        
        # 即使没有关键词，也获取时间范围内的消息
        if not keywords or len(results) < 5:
            time_results = db.search_by_time_range(
                start_time, end_time, limit=20, include_merged=True
            )
            for hit in time_results:
                if hit["msg_id"] not in seen_ids:
                    seen_ids.add(hit["msg_id"])
                    results.append(hit)
    
    # 再进行关键词检索（全库）
    for kw in keywords:
        hits = db.search_by_keyword([kw], limit=per_keyword, include_merged=True)
        for hit in hits:
            if hit["msg_id"] not in seen_ids:
                seen_ids.add(hit["msg_id"])
                results.append(hit)
    
    results.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return results


# ============================================================
# Step 1: 判断是否需要回复
# ============================================================

def step1_should_reply(content: str, user_name: str) -> Tuple[bool, str]:
    """
    Step1: 判断是否需要回复（超级兜底）。
    
    规则：
      - @提及机器人 → 直接回复
      - @提及其他人 → 不回复
      - 否则 → 几乎所有消息都回复，只有明显不需要回复的才不回复。
    
    返回:
        (should_reply, reason)
    """
    if not content or not isinstance(content, str):
        return False, "空消息"

    if _is_mentioned(content):
        return True, "@提及"
    
    # 检查是否在艾特别人（有 @ 但不是艾特我们）
    if "@" in content and not _is_mentioned(content):
        return False, "在艾特别人"

    # 检查是否是明显不需要回复的内容
    stripped = content.strip()
    if not stripped:
        return False, "空消息"
    
    # 检查是否纯标点符号
    if re.fullmatch(r'[^\w\s]+', stripped):
        return False, "纯标点"
    
    # 检查是否是极短的无意义词
    no_reply_words = ["嗯嗯", "哦", "好", "ok", "好的", "收到", "哈哈", "嗯"]
    if stripped in no_reply_words:
        return False, "无意义词"

    # 如果不是以上情况，直接回复！不用管 LLM 怎么判断！
    return True, "直接回复"



# ============================================================
# Step 2: 判断是否需要历史记忆
# ============================================================

def step2_need_memory(content: str) -> bool:
    """
    Step2: 判断回复是否需要历史记忆。
    
    规则（按优先级）：
      1. 包含记忆触发关键词 → True
      2. 包含问号/疑问 → True（可能是询问历史）
      3. 纯打招呼/闲聊/短陈述 → False
      4. 其他情况 → 默认 True
    
    返回:
        True 需要 / False 不需要
    """
    memory_keywords = [
        "之前", "以前", "过去", "上次", "之前说", "之前问", "聊过",
        "怎么了", "怎么回事", "发生什么", "发生了什么", "结果",
        "为什么", "为啥", "原因", "经过", "发生", "关于",
        "某人", "群里", "聊天记录", "记忆", "历史", "提到",
        "昨天", "前天", "上午", "下午", "晚上", "上周", "本周",
    ]
    for kw in memory_keywords:
        if kw in content:
            logger.info(f"[Step2] 关键词命中: '{kw}' → 强制使用记忆库")
            return True

    no_memory_starters = [
        "你好", "嗨", "hi", "hello", "哈喽", "早上好", "晚上好", "下午好",
        "今天天气", "今天心情", "吃饭了吗", "睡了吗", "在吗", "在不在",
    ]
    for s in no_memory_starters:
        if content.startswith(s):
            logger.info(f"[Step2] 纯打招呼/闲聊 → 不需要记忆库")
            return False

    has_question = any(q in content for q in ["？", "?", "吗", "呢", "吧", "谁", "哪", "怎", "啥", "几"])
    if has_question:
        logger.info(f"[Step2] 包含疑问 → 需要记忆库")
        return True

    stripped = content.strip()
    if len(stripped) <= 4:
        logger.info(f"[Step2] 短消息({len(stripped)}字) → 不需要记忆库")
        return False

    simple_statements = [
        "今天天气", "我好", "我觉得", "我想", "我要", "我喜欢",
        "哈哈哈", "笑死", "绝了", "牛逼", "厉害",
    ]
    for s in simple_statements:
        if stripped.startswith(s):
            logger.info(f"[Step2] 简单陈述 → 不需要记忆库")
            return False

    logger.info(f"[Step2] 规则默认 → 需要记忆库")
    return True


# ============================================================
# Step 3: 生成回复
# ============================================================

def _super_clean(text: str) -> str:
    """
    终极清理函数（白名单模式）：
    只保留中文汉字和基本中文标点，其余全部丢弃。
    然后修复符号粘连。
    """
    if not text:
        return ""

    KEEP_PUNCT = set('。，！？、；：')

    result = []
    for ch in text:
        if '\u4e00' <= ch <= '\u9fff':
            result.append(ch)
        elif '0' <= ch <= '9':
            result.append(ch)
        elif ch in KEEP_PUNCT:
            result.append(ch)

    text = ''.join(result)

    if not text:
        return ""

    for p in KEEP_PUNCT:
        while p + p in text:
            text = text.replace(p + p, p)

    punct_list = list(KEEP_PUNCT)
    for i in range(len(punct_list)):
        for j in range(len(punct_list)):
            if i != j:
                text = text.replace(punct_list[i] + punct_list[j], punct_list[j])

    while text and text[0] in KEEP_PUNCT:
        text = text[1:]
    while text and text[-1] in KEEP_PUNCT:
        text = text[:-1]

    if len(re.findall(r'[\u4e00-\u9fff]', text)) < 2:
        return ""

    return text


def _simple_summary_from_memory(references: List[Dict[str, Any]]) -> str:
    """
    完全不依赖 LLM 的简单总结方案。
    直接从记忆里拿最近几条拼起来，每条都经过 _super_clean 清理。
    """
    if not references:
        return ""

    recent = references[:5]
    if len(recent) == 0:
        return ""

    contents = []
    for ref in recent:
        content = ref.get("content", "")
        if content.startswith("system: "):
            content = content[len("system: "):]
        content = _super_clean(content)
        if content:
            contents.append(content)

    if not contents:
        return ""

    if len(contents) == 1:
        return contents[0]
    else:
        return "，还有".join(contents[:3])


def _smart_default_reply(content: str, no_memory: bool = False) -> str:
    """
    根据输入内容智能选择默认回复。
    """
    content_lower = content.strip().lower()
    
    # 打招呼类
    greetings = ["你好", "嗨", "hi", "hello", "哈喽", "早上好", "晚上好", "下午好"]
    for g in greetings:
        if g in content_lower:
            return random.choice(["你好呀~", "嗨！", "你好你好", "哈喽~"])
    
    # 询问类（无记忆时）
    if no_memory:
        questions = ["什么", "怎么", "为什么", "咋", "哪", "谁", "多少", "几"]
        for q in questions:
            if q in content_lower:
                return random.choice([
                    "这个我不太清楚诶",
                    "我不记得了",
                    "好像不太记得了",
                    "不清楚呢",
                ])
        return random.choice([
            "这个我不太清楚诶",
            "我不记得了",
            "这个没印象了",
        ])
    
    # 有记忆但没总结好时，给个更自然的回复
    if not no_memory:
        if "聊" in content_lower or "聊过" in content_lower or "聊了" in content_lower:
            return random.choice([
                "好像聊了不少有趣的话题呢",
                "之前聊了很多有意思的内容",
                "记得之前大家聊得挺热闹的",
            ])
    
    # 普通消息的默认回复
    return random.choice([
        "哈哈",
        "嗯嗯",
        "好的呀",
        "收到~",
        "了解了解",
        "可以可以",
        "没问题~",
        "👍",
        "😄",
    ])


def analyze_question_type(content: str) -> Dict[str, Any]:
    """
    分析问题类型，决定是概括性总结还是关键词搜索。
    
    返回:
        {
            "type": "summary" | "search",  # summary = 概括性问题，search = 具体问题
            "keywords": List[str],          # 有用的关键词
            "has_time": bool,               # 是否有时间词
        }
    """
    from memory_ai import parse_time_keywords
    
    # 1. 检查是否有时间范围
    time_range = parse_time_keywords(content)
    has_time = time_range is not None
    
    # 2. 提取关键词（过滤掉没用的）
    raw_keywords = extract_keywords(content)
    # 过滤掉没用的词
    useless_words = {
        "什么", "怎么", "为什么", "咋", "哪", "谁", "多少", "几", "吗", "了",
        "the", "is", "are", "what", "how", "why", "who", "when", "where",
        "聊", "聊天", "群聊", "昨天", "今天", "前天", "上周", "本周",
        "上午", "下午", "晚上", "早上",
    }
    useful_keywords = [
        kw for kw in raw_keywords 
        if kw.lower() not in useless_words 
        and len(kw) > 1
    ]
    
    # 3. 判断问题类型
    # 概括性问题的模式
    summary_patterns = [
        "聊了些什么", "聊什么", "聊了什么", "聊了些啥", "聊啥",
        "聊了些什么内容", "聊了什么内容", "聊了些什么话题", "聊了什么话题",
        "都聊了什么", "都聊了些什么", "都聊了些啥", "都聊了啥",
        "在聊什么", "聊的什么", "聊的啥", "聊些什么",
    ]
    
    is_summary = any(p in content for p in summary_patterns)
    
    # 如果是概括性问题，或者关键词少于2个且有时间，就 summary
    if is_summary or (has_time and len(useful_keywords) < 2):
        logger.info(f"[analyze_question] 类型: summary (概括性问题)")
        return {
            "type": "summary",
            "keywords": useful_keywords,
            "has_time": has_time,
            "time_range": time_range,
        }
    
    logger.info(f"[analyze_question] 类型: search (关键词搜索), 关键词={useful_keywords}")
    return {
        "type": "search",
        "keywords": useful_keywords,
        "has_time": has_time,
        "time_range": time_range,
    }


def step3_reply_with_memory(
    content: str,
    user_name: str,
    db: MemoryDB,
    chat_style: str,
) -> str:
    """
    Step3 分支1：先分析问题类型，再决定是概括总结还是关键词搜索。
    """
    from memory_ai import parse_time_keywords
    
    # 1. 分析问题类型
    analysis = analyze_question_type(content)
    q_type = analysis["type"]
    useful_keywords = analysis["keywords"]
    time_range = analysis["time_range"]
    
    references = []
    
    if q_type == "summary":
        # ========== 概括性问题：直接获取时间范围内的所有消息
        logger.info(f"[Step3] 概括性问题，获取时间范围内的消息")
        if time_range:
            start_time, end_time = time_range
            references = db.search_by_time_range(
                start_time, end_time, limit=30, include_merged=True
            )
        else:
            # 没有时间范围，取最近的消息
            references = db.search_by_keyword(["聊天记录"], limit=20, include_merged=True)
    else:
        if not useful_keywords:
            logger.info(f"[Step3] 无有用关键词，走直接回复")
            return step3_reply_direct(content, user_name, chat_style)

        logger.info(f"[Step3] 关键词搜索，关键词={useful_keywords}")
        references = _query_memory_by_keywords(db, useful_keywords, content, per_keyword=3)
    
    _save_memory_cache(useful_keywords if q_type == "search" else ["summary"], references)

    if references:
        ref_text = "\n".join(
            f"  [{r.get('timestamp', '')[:16]}] {r.get('user_name', '')}: {r.get('content', '')[:80]}"
            for r in references[:12]
        )
        if q_type == "summary":
            memory_part = f"参考记忆（按时间顺序）：\n{ref_text}"
        else:
            memory_part = f"参考记忆：\n{ref_text}"
    else:
        memory_part = "（无相关历史记忆）"

    style_part = f"语言风格参考：\n{chat_style}" if chat_style else ""

    if q_type == "summary":
        system = (
            "你是QQ群聊机器人。根据参考记忆总结群里聊过的话题，"
            "用2-3句简短中文口语回复。不要思考，直接输出回复。"
        )
    else:
        system = (
            "你是QQ群聊机器人。用1-2句简短中文口语回复。"
            "不要思考，直接输出回复。"
        )
    
    user = (
        f"{user_name} 说：\"{content[:200]}\"\n\n"
        f"{memory_part}\n\n"
        f"{style_part}\n\n"
        f"回复："
    )

    try:
        reply = _call_llm(system, user, temperature=0.7)
        logger.info(f"[Step3-分支1] 原始回复: {reply[:200]}")

        stripped = _strip_thinking_process(reply).strip()
        clean = _super_clean(stripped)

        if not clean:
            clean = _super_clean(reply)
            logger.info(f"[Step3-分支1] 直接clean: {clean[:200]}")

        if not clean:
            logger.warning(f"[Step3-分支1] 回复无中文，用简单总结")
            simple_reply = _simple_summary_from_memory(references)
            if simple_reply:
                return simple_reply
            return _smart_default_reply(content, no_memory=(not references))

        logger.info(f"[Step3-分支1] 最终回复: {clean[:200]}")
        return clean
    except Exception as e:
        logger.error(f"Step3-分支1 LLM 失败: {e}")
        simple_reply = _simple_summary_from_memory(references)
        if simple_reply:
            return simple_reply
        return _smart_default_reply(content, no_memory=True)


def step3_reply_direct(content: str, user_name: str, chat_style: str) -> str:
    """
    Step3 分支0：仅根据原消息 + chat_analyze 风格回复。
    """
    style_part = f"语言风格参考：\n{chat_style}" if chat_style else ""

    system = (
        "你是QQ群聊机器人。用1-2句简短中文口语回复。"
        "不要思考，直接输出回复。"
    )
    user = (
        f"{user_name} 说：\"{content[:200]}\"\n\n"
        f"{style_part}\n\n"
        f"回复："
    )

    try:
        reply = _call_llm(system, user, temperature=0.7)
        logger.info(f"[Step3-分支0] 原始回复: {reply[:200]}")

        stripped = _strip_thinking_process(reply).strip()
        clean = _super_clean(stripped)

        if not clean:
            clean = _super_clean(reply)
            logger.info(f"[Step3-分支0] 直接clean: {clean[:200]}")

        if not clean:
            logger.warning(f"[Step3-分支0] 回复无中文，用智能默认回复")
            return _smart_default_reply(content)

        logger.info(f"[Step3-分支0] 最终回复: {clean[:200]}")
        return clean
    except Exception as e:
        logger.error(f"Step3-分支0 LLM 失败: {e}")
        return _smart_default_reply(content)


# ============================================================
# Step 4: 存储回复
# ============================================================

def step4_store_reply(db: MemoryDB, reply: str) -> Optional[int]:
    """Step4: 将回复消息存入 Tier0。"""
    try:
        msg_id = db.add_message_analysis(
            user_id="bot",
            user_name=BOT_NAME,
            content=reply,
            keep_content=True,
        )
        if msg_id:
            logger.info(f"Step4: 回复已存储 msg_id={msg_id}")
        return msg_id
    except Exception as e:
        logger.error(f"Step4 存储失败: {e}")
        return None


# ============================================================
# 主入口
# ============================================================

def handle_message(
    user_id: str,
    user_name: str,
    content: str,
    db_path: str = "",
    store_in_memory: bool = True,
) -> Optional[str]:
    """
    处理一条群聊消息，返回回复文本（无需回复时返回 None）。
    
    参数:
        user_id:        发送者 QQ 号
        user_name:     发送者昵称
        content:       消息文本
        db_path:        记忆库路径（默认使用 MEMORY_DB_PATH）
        store_in_memory: 是否将消息和回复存入记忆库（默认 True）
    
    返回:
        str  — 回复文本
        None — 无需回复
    """
    if not db_path:
        try:
            from config import get_memory_db_path
            db_path = get_memory_db_path("default")
        except ImportError:
            db_path = _MEMORY_DB_PATH

    cleaned = clean_message(content)
    if cleaned is False:
        return None

    db = MemoryDB(db_path)
    t_start = time.time()

    # ========== Step 1: 存消息 + 判断是否回复 ==========
    if store_in_memory:
        db.add_message_analysis(
            user_id=user_id,
            user_name=user_name,
            content=content,
            keep_content=True,
        )

    should_reply, reason = step1_should_reply(content, user_name)
    logger.info(
        f"[{user_name}] \"{content[:40]}\" → Step1: {reason}"
    )

    if not should_reply:
        return None

    # ========== Step 2: 判断是否需要历史记忆 ==========
    need_memory = step2_need_memory(content)
    logger.info(f"[{user_name}] Step2: need_memory={need_memory}")

    # ========== Step 3: 生成回复 ==========
    chat_style = _load_chat_analyze()

    if need_memory:
        reply = step3_reply_with_memory(content, user_name, db, chat_style)
    else:
        reply = step3_reply_direct(content, user_name, chat_style)

    # ========== Step 4: 存储回复 ==========
    if store_in_memory:
        step4_store_reply(db, reply)

    elapsed = time.time() - t_start
    logger.info(
        f"[{user_name}] 回复完成 ({elapsed:.1f}s): \"{reply[:50] if reply else ''}\""
    )

    return reply


# ============================================================
# 命令行测试
# ============================================================

if __name__ == "__main__":
    print("=" * 50)
    print("  auto_reply 测试")
    print("=" * 50)

    test_messages = [
        ("测试用户A", "今天天气真好啊"),
        ("测试用户B", f"@{BOT_NAME} 你在吗"),
        ("测试用户C", "有人知道明天考试吗"),
    ]

    for name, msg in test_messages:
        print(f"\n>>> [{name}]: {msg}")
        result = handle_message("test_id", name, msg)
        if result:
            print(f"  → 回复: {result}")
        else:
            print(f"  → 不回复")

    print("\n" + "=" * 50)
    print("  测试完毕")
    print("=" * 50)
