"""
reply_engine.py — 群聊自动回复引擎
==================================
核心能力：
  1. 接收新消息 → 异步分析存入记忆库（memory_ai）。
  2. 检索历史语录（风格模仿）和历史事件（关联推测）。
  3. 基于行为规律进行推测联想，生成带"记忆推理"色彩的回复。

推测流程：
  当前消息 → 提取关键词/事件
    ├─ 关键词匹配历史语录 → 风格参考
    ├─ 事件 LIKE 匹配历史事件 → 关联上下文
    └─ 触发词匹配 patterns 规律 → 行为推测
    → LLM 生成推测句 → 注入最终回复提示词

依赖：Python 3.10+，memory_ai.py，sqlite3，requests，threading，json，logging
"""

import os
import json
import time
import sqlite3
import logging
import threading
import requests
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

from memory_ai import (
    extract_keywords,
    extract_event,
    clean_message,
    MemoryDB,
    MEMORY_DB_PATH,
)

# ============================================================
# 日志
# ============================================================
if not logging.getLogger("reply_engine").handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
logger = logging.getLogger("reply_engine")

# ============================================================
# 配置区（优先从 config.py 导入，否则使用默认值）
# ============================================================
try:
    from config import (  # type: ignore
        DB_PATH,
        MEMORY_DB_PATH as _CFG_MEMORY_DB,
        LLM_API_URL as _CFG_API_URL,
        LLM_MODEL_NAME as _CFG_MODEL,
        LLM_API_KEY as _CFG_API_KEY,
    )
    DB_PATH = DB_PATH
    MEMORY_DB_PATH = _CFG_MEMORY_DB
    LLM_API_URL = _CFG_API_URL
    LLM_MODEL_NAME = _CFG_MODEL
    LLM_API_KEY = _CFG_API_KEY
    logger.info("已从 config.py 加载配置。")
except ImportError:
    logger.warning("config.py 不存在，使用内置默认配置。")
    DB_PATH = os.path.join(os.path.dirname(__file__), "chat.db")
    MEMORY_DB_PATH = os.path.join(os.path.dirname(__file__), "memory.db")
    LLM_API_URL = "http://localhost:1234/v1/chat/completions"
    LLM_MODEL_NAME = "local-model"
    LLM_API_KEY = ""

# ---- 全局开关与参数 ----
ENABLE_INFERENCE = True          # 调试时可关闭推测功能
LLM_TIMEOUT = 5                  # 单次 LLM 请求超时（秒）
LLM_MAX_RETRIES = 2              # LLM 请求最大重试次数
INFERENCE_OVERALL_TIMEOUT = 3    # 推测引擎整体超时（秒），超时则跳过推测
MAX_WORKERS = 2                  # 线程池最大 worker 数（控制 LLM 并发）

# ---- 线程池（用于异步记忆存储） ----
_async_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="reply")

# ---- 默认回复（消息无效时） ----
DEFAULT_FALLBACK_REPLY = "..."

# ============================================================
# LLM 调用工具（独立封装，支持可变 max_tokens / temperature）
# ============================================================

def _call_llm(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 8192,
    temperature: float = 0.7,
    timeout: int = LLM_TIMEOUT,
    max_retries: int = LLM_MAX_RETRIES,
) -> str:
    """
    通过 LM Studio 本地 API 调用 LLM。
    内置超时与重试，失败抛出 ConnectionError。
    """
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

    last_error = ""
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(LLM_API_URL, json=payload, headers=headers, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            message = data["choices"][0]["message"]
            content = ""
            if "reasoning_content" in message and message["reasoning_content"]:
                content = message["reasoning_content"].strip()
            if not content and "content" in message:
                content = message["content"].strip()
            if not content:
                raise ValueError("AI返回空内容")
            return content
        except requests.exceptions.Timeout:
            last_error = f"超时（{timeout}s）"
        except requests.exceptions.ConnectionError:
            last_error = f"无法连接 {LLM_API_URL}"
        except requests.exceptions.RequestException as e:
            last_error = str(e)
        except (KeyError, IndexError, TypeError) as e:
            last_error = f"响应格式异常: {e}"

        if attempt < max_retries:
            time.sleep(1.5 ** attempt)

    raise ConnectionError(f"LLM 调用失败（已重试 {max_retries} 次）: {last_error}")


# ============================================================
# 1. 规律提炼 — patterns 表与 update_patterns()
# ============================================================

def init_patterns_table(db_path: str) -> None:
    """
    在 memory.db 中创建 patterns 表（如不存在）。
    
    表结构：
      patterns(id, category, description, trigger_words,
               supporting_events, confidence, created_at)
    
    trigger_words: 逗号分隔的触发词，回复时用于匹配当前消息。
    supporting_events: JSON 数组，记录支撑该规律的原始事件摘要。
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_patterns_category ON patterns(category)"
    )
    conn.commit()
    conn.close()
    logger.info("patterns 表初始化完成。")


def update_patterns(db_path: str, days: int = 7) -> int:
    """
    规律提炼：查询近 `days` 天的 events，调用 LLM 归纳行为规律，
    清空旧规律并插入新规律。
    
    可被定时任务（如每12小时）调用。
    
    返回:
        int — 新提炼的规律数量。
    """
    init_patterns_table(db_path)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # 查询近 N 天事件（排除合并事件 msg_id=-1）
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    cur.execute(
        "SELECT timestamp, user_name, subject, verb, object, summary FROM events "
        "WHERE timestamp >= ? AND msg_id != -1 AND summary != '' "
        "ORDER BY timestamp ASC",
        (cutoff,),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        logger.info(f"update_patterns: 近 {days} 天无事件，跳过规律提炼。")
        return 0

    # 组装事件文本
    events_text = "\n".join(
        f"[{r[0][:10]}] {r[1]}: {r[2]} {r[3]} {r[4]}（{r[5]}）"
        for r in rows
    )

    # 调用 LLM 归纳规律
    system = "你是一个行为规律分析器。只输出合法 JSON 数组，不要任何解释或 Markdown。"
    user = (
        "根据以下一周的群聊事件，归纳出群成员稳定的行为规律。"
        "输出 JSON 数组，每个对象包含："
        "category(类别)、description(规律描述)、"
        "trigger_words(触发词，逗号分隔)、confidence(高/中/低)。"
        "若无规律则返回空数组。\n"
        f"事件列表：\n{events_text}"
    )

    try:
        output = _call_llm(system, user, max_tokens=512, temperature=0.2)
    except Exception as e:
        logger.error(f"规律提炼 LLM 调用失败: {e}")
        return 0

    # 解析 JSON
    patterns_list = _parse_patterns_json(output)
    if not patterns_list:
        logger.info("LLM 未发现可归纳的规律。")
        return 0

    # 写入数据库：先清空旧规律，再批量插入
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("DELETE FROM patterns")
    for p in patterns_list:
        cur.execute(
            "INSERT INTO patterns (category, description, trigger_words, "
            "supporting_events, confidence, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                p.get("category", ""),
                p.get("description", ""),
                p.get("trigger_words", ""),
                json.dumps(p.get("supporting_events", []), ensure_ascii=False),
                p.get("confidence", "中"),
                now_str,
            ),
        )
    conn.commit()
    conn.close()

    logger.info(f"规律提炼完成: 发现 {len(patterns_list)} 条规律。")
    return len(patterns_list)


def _parse_patterns_json(raw: str) -> List[Dict[str, Any]]:
    """安全解析 LLM 返回的规律 JSON 数组。"""
    # 清理 Markdown 代码块
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL)
    if m:
        raw = m.group(1).strip()
    # 提取第一个 [ ... ]
    start = raw.find("[")
    end = raw.rfind("]")
    if start != -1 and end != -1:
        raw = raw[start:end + 1]
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [p for p in parsed if isinstance(p, dict)]
    except json.JSONDecodeError:
        logger.warning(f"规律 JSON 解析失败，原始输出: {raw[:200]}")
    return []


# ============================================================
# 2. 检索器
# ============================================================

def retrieve_quotes(db_path: str, current_msg: str, limit: int = 3) -> List[str]:
    """
    检索历史语录（风格模仿用）。
    
    流程：
      1. 调用 memory_ai.extract_keywords 获取当前消息关键词。
      2. 在 keywords 表匹配关键词，取关联 msg_id。
      3. 从 messages 表取出原文（包括 original 和 merged 状态），
         按时间倒序取前 limit 条。
    
    返回:
        list[str] — 历史语录列表，无结果时返回空列表。
    """
    keywords = extract_keywords(current_msg)
    if not keywords:
        return []

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    placeholders = ",".join("?" for _ in keywords)
    cur.execute(
        f"SELECT DISTINCT k.msg_id FROM keywords k "
        f"WHERE k.keyword IN ({placeholders})",
        keywords,
    )
    matched_ids = [row[0] for row in cur.fetchall()]

    if not matched_ids:
        conn.close()
        return []

    id_placeholders = ",".join("?" for _ in matched_ids)
    cur.execute(
        f"SELECT content FROM messages "
        f"WHERE id IN ({id_placeholders}) "
        f"AND content_state IN ('original', 'merged') AND content != '' "
        f"ORDER BY timestamp DESC LIMIT ?",
        matched_ids + [limit],
    )
    quotes = [row[0] for row in cur.fetchall()]
    conn.close()

    logger.debug(f"retrieve_quotes: 关键词={keywords} → {len(quotes)} 条语录。")
    return quotes


def retrieve_related_events(
    db_path: str, current_msg: str, limit: int = 3
) -> List[Dict[str, Any]]:
    """
    检索历史相关事件（关联推测用）。
    
    流程：
      1. 调用 memory_ai.extract_event 获取当前消息的结构化事件。
      2. 在 events 表中模糊匹配：
           subject LIKE '%当前subject%' OR verb LIKE '%当前verb%'
           OR object LIKE '%当前object%'
      3. 按时间倒序取前 limit 条 summary。
    
    注意：当前使用简单 LIKE 查询。
          预留升级接口：可替换为向量检索（embedding + cosine similarity），
          只需将本函数的 LIKE 匹配逻辑替换为向量相似度 TopK 查询即可，
          接口签名保持不变。
    
    返回:
        list[dict] — [{"summary": ..., "user_name": ..., "timestamp": ...}, ...]
    """
    event = extract_event(current_msg)
    if not event:
        return []

    subj = event.get("subject", "")
    verb = event.get("verb", "")
    obj = event.get("object", "")

    # 至少有一个非空字段才检索
    if not subj and not verb and not obj:
        return []

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # 构建 LIKE 条件（动态拼接，避免全表扫描时仍用时间范围兜底）
    conditions: List[str] = []
    params: List[str] = []
    if subj:
        conditions.append("subject LIKE ?")
        params.append(f"%{subj}%")
    if verb:
        conditions.append("verb LIKE ?")
        params.append(f"%{verb}%")
    if obj:
        conditions.append("object LIKE ?")
        params.append(f"%{obj}%")

    where = " OR ".join(conditions)
    # 加时间范围限制（近90天），避免扫描全表
    cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    cur.execute(
        f"SELECT summary, user_name, timestamp FROM events "
        f"WHERE ({where}) AND msg_id != -1 AND timestamp >= ? "
        f"ORDER BY timestamp DESC LIMIT ?",
        params + [cutoff, limit],
    )
    rows = cur.fetchall()
    conn.close()

    results = [
        {"summary": r[0], "user_name": r[1], "timestamp": r[2]}
        for r in rows
    ]
    logger.debug(
        f"retrieve_related_events: event={event} → {len(results)} 条。"
    )
    return results


# ============================================================
# 3. 推测引擎
# ============================================================

def generate_inference(db_path: str, current_msg: str) -> str:
    """
    推测引擎：基于历史事件和行为规律生成推测句。
    
    流程：
      1. 获取当前消息的事件（若无则返回空）。
      2. 检索历史相关事件摘要。
      3. 扫描 patterns 表，检查当前消息是否包含某规律的 trigger_words。
      4. 若既无历史事件又无触发规律 → 返回空字符串。
      5. 组装"推测上下文" → 调用 LLM 生成推测句。
    
    返回:
        str — 推测句（如"说起来，上个月张哥也说过类似的话……"），
              无法推测时返回空字符串。
    """
    if not ENABLE_INFERENCE:
        return ""

    # Step 1: 获取当前事件
    current_event = extract_event(current_msg)

    # Step 2: 检索历史事件
    related_events = retrieve_related_events(db_path, current_msg, limit=3)

    # Step 3: 扫描 patterns 表
    triggered_patterns = _match_patterns(db_path, current_msg)

    # Step 4: 无信息则跳过
    if not related_events and not triggered_patterns:
        return ""

    # Step 5: 组装推测上下文
    context_parts: List[str] = []

    if related_events:
        events_text = "\n".join(
            f"- [{e['timestamp'][:10]}] {e['user_name']}: {e['summary']}"
            for e in related_events
        )
        context_parts.append(f"【历史相关事件】\n{events_text}")

    if triggered_patterns:
        patterns_text = "\n".join(
            f"- [{p['confidence']}置信度] {p['description']}"
            for p in triggered_patterns
        )
        context_parts.append(f"【触发的行为规律】\n{patterns_text}")

    inference_context = "\n\n".join(context_parts)

    # Step 6: 调用 LLM 生成推测句
    system = "你是一个群聊记忆助手。只输出一句话推测，不要任何解释。"
    user = (
        "根据以下历史信息，生成一句自然、口语化的推测或联想，"
        "以\"说起来，\"或\"我记得\"开头。"
        "若信息不足以推测，返回空。\n"
        f"历史信息：\n{inference_context}\n\n"
        f"当前消息：{current_msg}"
    )

    try:
        inference = _call_llm(system, user, max_tokens=80, temperature=0.6)
        inference = inference.strip()
        if not inference or inference in ("空", "无", "None", "null", "{}", "[]"):
            return ""
        return inference
    except Exception as e:
        logger.warning(f"推测句生成失败: {e}")
        return ""


def _match_patterns(db_path: str, current_msg: str) -> List[Dict[str, Any]]:
    """
    扫描 patterns 表，检查当前消息是否触发某规律。
    
    匹配规则：将规律的 trigger_words 按逗号拆分，
             逐一检查是否包含在当前消息文本中（子串匹配）。
    
    返回:
        list[dict] — 触发的规律列表。
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, category, description, trigger_words, confidence FROM patterns"
    )
    rows = cur.fetchall()
    conn.close()

    matched: List[Dict[str, Any]] = []
    for row in rows:
        pid, cat, desc, triggers, conf = row
        if not triggers:
            continue
        # 拆分触发词
        words = [w.strip() for w in triggers.replace("，", ",").split(",") if w.strip()]
        # 任一触发词在当前消息中出现即命中
        if any(w in current_msg for w in words):
            matched.append({
                "id": pid,
                "category": cat,
                "description": desc,
                "confidence": conf,
            })

    if matched:
        logger.debug(f"_match_patterns: 命中 {len(matched)} 条规律。")
    return matched


# ============================================================
# 4. 最终回复生成
# ============================================================

def generate_final_reply(
    current_msg: str,
    retrieved_quotes: List[str],
    inference_sentence: str,
) -> str:
    """
    生成最终回复。
    
    组装 system prompt：
      - 注入历史语录作为风格参考。
      - 选择性融入推测句（如果自然的话）。
      - 要求用轻松口吻，控制在三句话内。
    
    参数:
        current_msg:        当前用户消息
        retrieved_quotes:   检索到的历史语录列表
        inference_sentence: 推测句（可为空）
    
    返回:
        str — 生成的回复文本。
    """
    # 组装风格参考
    if retrieved_quotes:
        quotes_text = "\n".join(f"  · {q}" for q in retrieved_quotes)
        style_part = f"你的说话风格要模仿下面这些群友的历史语录：\n{quotes_text}"
    else:
        style_part = "你的说话风格要自然、口语化，像群友聊天。"

    # 组装推测部分
    if inference_sentence:
        inference_part = (
            f"此外，你可以选择性地融入以下基于记忆的推测（如果自然的话）：\n"
            f"  {inference_sentence}"
        )
    else:
        inference_part = ""

    # 最终 system prompt
    system = (
        f"你是一个群聊机器人。不要思考，直接回复。\n"
        f"{style_part}\n"
        f"{inference_part}\n"
        f"现在请用轻松的口吻回复这条消息，控制在三句话内。"
    ).strip()

    user = f"回复以下消息：{current_msg}"

    try:
        reply = _call_llm(system, user, temperature=0.8)
        return reply.strip()
    except Exception as e:
        logger.error(f"最终回复生成失败: {e}")
        return DEFAULT_FALLBACK_REPLY


# ============================================================
# 5. 主回复入口（供 Flask 调用）
# ============================================================

def handle_message(
    db_path: str,
    user_id: str,
    user_name: str,
    content: str,
) -> str:
    """
    主回复入口函数。
    
    完整流程：
      1. 异步：将消息分析存入记忆库（不阻塞回复）。
      2. 同步：清洗检查 → 无效消息返回默认回复。
      3. 检索历史语录（风格参考）。
      4. 推测引擎（事件检索 + 规律匹配，整体超时 INFERENCE_OVERALL_TIMEOUT 秒）。
      5. 记录检索命中日志。
      6. 生成最终回复并返回。
    
    参数:
        db_path:   memory.db 路径
        user_id:   发送者 ID
        user_name: 发送者昵称
        content:   消息文本
    
    返回:
        str — 回复文本。
    """
    # ---- Step 1: 异步存储（不阻塞） ----
    _async_executor.submit(
        _async_store_message, db_path, user_id, user_name, content
    )

    # ---- Step 2: 同步清洗检查 ----
    cleaned = clean_message(content)
    if cleaned is False:
        logger.debug(f"消息无效，返回默认回复: [{user_name}] {content[:40]}")
        return DEFAULT_FALLBACK_REPLY

    # ---- Step 3: 检索历史语录 ----
    quotes = retrieve_quotes(db_path, content, limit=3)

    # ---- Step 4: 推测引擎（带整体超时） ----
    inference = ""
    if ENABLE_INFERENCE:
        try:
            future = _async_executor.submit(generate_inference, db_path, content)
            inference = future.result(timeout=INFERENCE_OVERALL_TIMEOUT)
        except FutureTimeoutError:
            logger.debug("推测引擎超时，跳过推测。")
        except Exception as e:
            logger.warning(f"推测引擎异常: {e}")

    # ---- Step 5: 记录检索命中 ----
    if quotes:
        _log_retrieval_hits(db_path, content, quotes)

    # ---- Step 6: 生成最终回复 ----
    reply = generate_final_reply(content, quotes, inference)

    logger.info(
        f"回复生成: [{user_name}] \"{content[:30]}...\" → "
        f"语录={len(quotes)} 推测={'有' if inference else '无'} "
        f"→ \"{reply[:40]}...\""
    )
    return reply


def _async_store_message(
    db_path: str, user_id: str, user_name: str, content: str
) -> None:
    """异步线程：将消息分析并存入记忆库。"""
    try:
        db = MemoryDB(db_path)
        msg_id = db.add_message_analysis(user_id, user_name, content)
        if msg_id:
            logger.debug(f"异步存储完成: msg_id={msg_id}")
    except Exception as e:
        logger.error(f"异步存储失败 [{user_name}]: {e}")


def _log_retrieval_hits(
    db_path: str, query_text: str, quotes: List[str]
) -> None:
    """
    记录检索命中日志。
    通过匹配 quotes 内容反查 msg_id，写入 retrieval_log。
    """
    if not quotes:
        return
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    placeholders = ",".join("?" for _ in quotes)
    cur.execute(
        f"SELECT id FROM messages WHERE content IN ({placeholders})",
        quotes,
    )
    msg_ids = [row[0] for row in cur.fetchall()]
    conn.close()

    if msg_ids:
        db = MemoryDB(db_path)
        db.log_retrieval(msg_ids, query_text)


# ============================================================
# 命令行测试入口
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  群聊回复引擎 · 独立测试")
    print("=" * 60)

    test_db = os.path.join(os.path.dirname(__file__), "memory.db")
    print(f"\n数据库: {test_db}")

    # ---- 测试 1: 初始化 patterns 表 ----
    print("\n>>> [1] init_patterns_table()")
    init_patterns_table(test_db)
    print("  patterns 表就绪。")

    # ---- 测试 2: 规律提炼 ----
    print("\n>>> [2] update_patterns(days=7)")
    count = update_patterns(test_db, days=7)
    print(f"  提炼规律: {count} 条")

    # ---- 测试 3: 检索语录 ----
    print("\n>>> [3] retrieve_quotes()")
    test_msg = "今天天气真好，适合出去玩"
    quotes = retrieve_quotes(test_db, test_msg, limit=3)
    for i, q in enumerate(quotes):
        print(f"  [{i}] {q[:60]}")

    # ---- 测试 4: 检索事件 ----
    print("\n>>> [4] retrieve_related_events()")
    events = retrieve_related_events(test_db, test_msg, limit=3)
    for i, e in enumerate(events):
        print(f"  [{i}] {e['user_name']}: {e['summary'][:60]}")

    # ---- 测试 5: 推测引擎 ----
    print("\n>>> [5] generate_inference()")
    inference = generate_inference(test_db, test_msg)
    print(f"  推测句: \"{inference}\"" if inference else "  （无推测）")

    # ---- 测试 6: 最终回复 ----
    print("\n>>> [6] generate_final_reply()")
    reply = generate_final_reply(test_msg, quotes, inference)
    print(f"  回复: \"{reply}\"")

    # ---- 测试 7: 完整 handle_message ----
    print("\n>>> [7] handle_message() 完整流程")
    reply = handle_message(test_db, "test_user", "测试用户", test_msg)
    print(f"  最终回复: \"{reply}\"")

    print("\n" + "=" * 60)
    print("  测试完毕（异步存储可能仍在后台运行）")
    print("=" * 60)
