#!/usr/bin/env python3
"""
daily_maintenance.py — 每日维护（语言分析 + 记忆平移）
====================================================

时间跨度定义：
- Tier 0：当天（00:00~23:59，实时缓存）
- Tier 1：第1-2天（按30分钟一块，共48块/天）
- Tier 2：第3-6天（按12小时一块，共2块/天）
- Tier 3：≥7天（按1天一块，共1块/天）

每日维护流程（每天调用一次）：
  0. 语言分析：分析 Tier 0（昨天）消息的语言特征，更新 chat_analyze 文件
  1. Tier 2 → Tier 3：第7天的2个12小时块合并成1个天块
  2. Tier 1 → Tier 2：第3天的48个30分钟块合并成2个12小时块
  3. Tier 0 → Tier 1：昨天的消息按30分钟块合并进入 Tier 1
  4. 清理检索日志 + VACUUM
"""

import os
import time
import logging
import argparse
from datetime import datetime, timedelta

# 日志配置
log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daily_maintenance.log")
if not logging.getLogger("daily_maintenance").handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
logger = logging.getLogger("daily_maintenance")

# 从 config.py 导入配置
try:
    from config import MEMORY_DB_PATH
except ImportError:
    MEMORY_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory.db")
    logger.warning("未找到 config.py，使用默认数据库路径。")


def get_window_key(dt: datetime, tier: int) -> str:
    """
    获取窗口键（固定时间格子划分）
    
    时间划分方式：
      - Tier 0/1（30分钟）：从 00:00 到 23:30 共 48 个格子
      - Tier 2（12小时）：从 00:00 到 12:00 共 2 个格子
      - Tier 3（1天）：1个格子
    """
    if tier in (0, 1):
        # 30分钟格子
        dt_window = dt.replace(
            minute=(dt.minute // 30) * 30,
            second=0,
            microsecond=0
        )
        return f"t1_{dt_window.strftime('%Y%m%d_%H%M')}"
    elif tier == 2:
        # 12小时格子
        hour = (dt.hour // 12) * 12
        dt_window = dt.replace(
            hour=hour,
            minute=0,
            second=0,
            microsecond=0
        )
        return f"t2_{dt_window.strftime('%Y%m%d_%H')}"
    elif tier == 3:
        # 1天格子
        dt_window = dt.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0
        )
        return f"t3_{dt_window.strftime('%Y%m%d')}"
    else:
        return ""


def merge_window_messages(msgs: list, db_path: str, window_ts: str) -> int:
    """
    将一组消息合并为一条
      - msgs: [{"id": ..., "user_name": ..., "content": ..., "timestamp": ...}, ...]
      - db_path: 数据库路径
      - window_ts: 合并消息的时间戳
      - 返回: 合并后的新 msg_id，失败返回 -1
    """
    if len(msgs) <= 1:
        return -1
    
    # 调用 LLM 合并
    try:
        from memory_ai import merge_messages
        merge_input = [{"user_name": m["user_name"], "content": m["content"], "timestamp": m["timestamp"]} for m in msgs]
        result = merge_messages(merge_input)
        summary = result["summary"]
        keywords = result["keywords"]
    except Exception as e:
        logger.error(f"合并消息失败: {e}")
        return -1
    
    if not summary:
        return -1
    
    # 插入合并消息
    try:
        import sqlite3
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        
        cur.execute("""
            INSERT INTO messages (user_id, user_name, content, timestamp, content_state, last_maintained)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("system", "system", summary, window_ts, "merged", now_str))
        merged_msg_id = cur.lastrowid
        
        # 添加关键词
        if keywords:
            cur.executemany("""
                INSERT OR IGNORE INTO keywords (keyword, msg_id) VALUES (?, ?)
            """, [(kw, merged_msg_id) for kw in keywords])
        
        conn.commit()
        conn.close()
        
        logger.debug(f"合并窗口完成：{len(msgs)} 条 → 1 条 (id={merged_msg_id})")
        return merged_msg_id
    except Exception as e:
        logger.error(f"插入合并消息失败: {e}")
        return -1


def get_day_range(day_offset: int) -> tuple:
    """
    获取某一天的时间范围
    
    参数:
        day_offset: 天数偏移，0=今天，1=昨天，7=7天前
    
    返回:
        (start_str, end_str) 时间字符串
    """
    now = datetime.now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=day_offset)
    day_end = day_start + timedelta(days=1)
    return (
        day_start.strftime("%Y-%m-%d %H:%M:%S"),
        day_end.strftime("%Y-%m-%d %H:%M:%S")
    )


def tier0_to_tier1(db_path: str) -> dict:
    """
    Tier 0 → Tier 1：把当天（昨天）的消息按30分钟一块合并进 Tier 1
    """
    import sqlite3
    
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    
    # 获取昨天的时间范围（昨天是Tier 0，今天调用维护时处理昨天）
    yesterday_start, yesterday_end = get_day_range(1)
    
    logger.info(f"Tier 0 → Tier 1：处理昨天 {yesterday_start} ~ {yesterday_end}")
    
    stats = {"windows": 0, "merged": 0, "errors": 0}
    
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        
        # 查询昨天的消息（content不为空且不是merged/cleared）
        cur.execute("""
            SELECT id, user_id, user_name, content, timestamp
            FROM messages
            WHERE timestamp >= ?
              AND timestamp < ?
              AND content != ''
              AND content_state != 'merged'
              AND content_state != 'cleared'
            ORDER BY timestamp
        """, (yesterday_start, yesterday_end))
        rows = cur.fetchall()
        conn.close()
        
        if not rows:
            logger.info("Tier 0 → Tier 1：无消息需要处理")
            return stats
        
        # 按30分钟窗口分组
        windows = {}
        for row in rows:
            msg_id, user_id, user_name, content, ts = row
            try:
                msg_dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            
            window_key = get_window_key(msg_dt, tier=1)
            if window_key not in windows:
                windows[window_key] = []
            windows[window_key].append({
                "id": msg_id,
                "user_id": user_id,
                "user_name": user_name,
                "content": content,
                "timestamp": ts,
            })
        
        # 处理每个窗口
        for window_key, msgs in windows.items():
            if len(msgs) <= 1:
                continue
            
            _, time_str = window_key.split("_", 1)
            try:
                window_dt = datetime.strptime(time_str, "%Y%m%d_%H%M")
                window_ts = window_dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception as e:
                logger.warning(f"解析窗口时间失败: {window_key}, {e}")
                continue
            
            stats["windows"] += 1
            merged_id = merge_window_messages(msgs, db_path, window_ts)
            
            if merged_id > 0:
                stats["merged"] += 1
                conn2 = sqlite3.connect(db_path)
                cur2 = conn2.cursor()
                msg_ids = [m["id"] for m in msgs]
                cur2.executemany("""
                    UPDATE messages
                    SET content = '',
                        content_state = 'cleared',
                        last_maintained = ?
                    WHERE id = ?
                """, [(now_str, mid) for mid in msg_ids])
                conn2.commit()
                conn2.close()
            else:
                stats["errors"] += 1
        
        logger.info(f"Tier 0 → Tier 1：处理了 {stats['windows']} 个窗口，合并了 {stats['merged']} 个")
    except Exception as e:
        logger.error(f"Tier 0 → Tier 1 失败: {e}")
        stats["errors"] += 1
    
    return stats


def tier1_to_tier2(db_path: str) -> dict:
    """
    Tier 1 → Tier 2：把第3天的48个30分钟块合并成2个12小时块进入 Tier 2
    """
    import sqlite3
    
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    
    # 获取第3天的时间范围
    day3_start, day3_end = get_day_range(3)
    
    logger.info(f"Tier 1 → Tier 2：处理第3天 {day3_start} ~ {day3_end}")
    
    stats = {"windows": 0, "merged": 0, "errors": 0}
    
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        
        # 查询第3天的消息（content不为空且不是merged/cleared）
        cur.execute("""
            SELECT id, user_id, user_name, content, timestamp
            FROM messages
            WHERE timestamp >= ?
              AND timestamp < ?
              AND content != ''
              AND content_state != 'merged'
              AND content_state != 'cleared'
            ORDER BY timestamp
        """, (day3_start, day3_end))
        rows = cur.fetchall()
        conn.close()
        
        if not rows:
            logger.info("Tier 1 → Tier 2：无消息需要处理")
            return stats
        
        # 按12小时窗口分组
        windows = {}
        for row in rows:
            msg_id, user_id, user_name, content, ts = row
            try:
                msg_dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            
            window_key = get_window_key(msg_dt, tier=2)
            if window_key not in windows:
                windows[window_key] = []
            windows[window_key].append({
                "id": msg_id,
                "user_id": user_id,
                "user_name": user_name,
                "content": content,
                "timestamp": ts,
            })
        
        # 处理每个窗口
        for window_key, msgs in windows.items():
            if len(msgs) <= 1:
                continue
            
            _, time_str = window_key.split("_", 1)
            try:
                window_dt = datetime.strptime(time_str, "%Y%m%d_%H")
                window_ts = window_dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception as e:
                logger.warning(f"解析窗口时间失败: {window_key}, {e}")
                continue
            
            stats["windows"] += 1
            merged_id = merge_window_messages(msgs, db_path, window_ts)
            
            if merged_id > 0:
                stats["merged"] += 1
                conn2 = sqlite3.connect(db_path)
                cur2 = conn2.cursor()
                msg_ids = [m["id"] for m in msgs]
                cur2.executemany("""
                    UPDATE messages
                    SET content = '',
                        content_state = 'cleared',
                        last_maintained = ?
                    WHERE id = ?
                """, [(now_str, mid) for mid in msg_ids])
                conn2.commit()
                conn2.close()
            else:
                stats["errors"] += 1
        
        logger.info(f"Tier 1 → Tier 2：处理了 {stats['windows']} 个窗口，合并了 {stats['merged']} 个")
    except Exception as e:
        logger.error(f"Tier 1 → Tier 2 失败: {e}")
        stats["errors"] += 1
    
    return stats


def tier2_to_tier3(db_path: str) -> dict:
    """
    Tier 2 → Tier 3：把第7天的2个12小时块合并成1个天块进入 Tier 3
    """
    import sqlite3
    
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    
    # 获取第7天的时间范围
    day7_start, day7_end = get_day_range(7)
    
    logger.info(f"Tier 2 → Tier 3：处理第7天 {day7_start} ~ {day7_end}")
    
    stats = {"windows": 0, "merged": 0, "errors": 0}
    
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        
        # 查询第7天的消息（content不为空且不是merged/cleared）
        cur.execute("""
            SELECT id, user_id, user_name, content, timestamp
            FROM messages
            WHERE timestamp >= ?
              AND timestamp < ?
              AND content != ''
              AND content_state != 'merged'
              AND content_state != 'cleared'
            ORDER BY timestamp
        """, (day7_start, day7_end))
        rows = cur.fetchall()
        conn.close()
        
        if not rows:
            logger.info("Tier 2 → Tier 3：无消息需要处理")
            return stats
        
        # 按1天窗口分组（所有消息在一个窗口）
        msgs = []
        for row in rows:
            msg_id, user_id, user_name, content, ts = row
            msgs.append({
                "id": msg_id,
                "user_id": user_id,
                "user_name": user_name,
                "content": content,
                "timestamp": ts,
            })
        
        if len(msgs) <= 1:
            return stats
        
        # 第7天的开始时间作为窗口时间
        window_ts = day7_start
        
        stats["windows"] += 1
        merged_id = merge_window_messages(msgs, db_path, window_ts)
        
        if merged_id > 0:
            stats["merged"] += 1
            conn2 = sqlite3.connect(db_path)
            cur2 = conn2.cursor()
            msg_ids = [m["id"] for m in msgs]
            cur2.executemany("""
                UPDATE messages
                SET content = '',
                    content_state = 'cleared',
                    last_maintained = ?
                WHERE id = ?
            """, [(now_str, mid) for mid in msg_ids])
            conn2.commit()
            conn2.close()
        else:
            stats["errors"] += 1
        
        logger.info(f"Tier 2 → Tier 3：处理了 {stats['windows']} 个窗口，合并了 {stats['merged']} 个")
    except Exception as e:
        logger.error(f"Tier 2 → Tier 3 失败: {e}")
        stats["errors"] += 1
    
    return stats


def cleanup_retrieval_log(db_path: str, keep_days: int = 30) -> int:
    """清理检索日志，只保留最近 keep_days 天"""
    import sqlite3
    from datetime import datetime, timedelta
    
    now = datetime.now()
    cutoff = (now - timedelta(days=keep_days)).strftime("%Y-%m-%d %H:%M:%S")
    
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("DELETE FROM retrieval_log WHERE timestamp < ?", (cutoff,))
        count = cur.rowcount
        conn.commit()
        conn.close()
        logger.info(f"清理检索日志：删除了 {count} 条")
        return count
    except Exception as e:
        logger.error(f"清理检索日志失败: {e}")
        return 0


def vacuum_db(db_path: str) -> bool:
    """执行 VACUUM 压缩数据库"""
    import sqlite3
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("VACUUM")
        conn.commit()
        conn.close()
        logger.info("执行 VACUUM 完成")
        return True
    except Exception as e:
        logger.error(f"VACUUM 失败: {e}")
        return False


def _ensure_indexes(db_path: str) -> None:
    """确保关键索引存在"""
    import sqlite3
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_keywords_keyword ON keywords(keyword)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp)"
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"索引检查失败（可忽略）: {e}")


# ======================== 语言分析 ========================

CHAT_ANALYZE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "chat_analyze"
)


def _call_llm(system_prompt: str, user_prompt: str, max_tokens: int = 8192) -> str:
    """调用 LLM，返回纯文本结果。"""
    import requests
    try:
        from memory_ai import LLM_API_URL, LLM_MODEL_NAME, LLM_TIMEOUT, LLM_MAX_RETRIES
        try:
            from config import LLM_API_KEY
        except ImportError:
            LLM_API_KEY = ""
    except ImportError:
        LLM_API_URL = "http://localhost:1234/v1/chat/completions"
        LLM_MODEL_NAME = "local-model"
        LLM_API_KEY = ""
        LLM_TIMEOUT = 60
        LLM_MAX_RETRIES = 2

    payload = {
        "model": LLM_MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }
    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"

    for attempt in range(LLM_MAX_RETRIES + 1):
        try:
            resp = requests.post(LLM_API_URL, json=payload, headers=headers, timeout=LLM_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            message = data["choices"][0]["message"]
            content = ""
            if "content" in message and message["content"]:
                content = message["content"].strip()
            if not content and "reasoning_content" in message and message["reasoning_content"]:
                content = message["reasoning_content"].strip()
            if content:
                return content
        except Exception as e:
            if attempt >= LLM_MAX_RETRIES:
                raise
            time.sleep(1.5 ** attempt)
    return ""


def analyze_daily_language(db_path: str) -> str:
    """
    分析 Tier 0（昨天）消息的语言特征。
    
    返回:
        str — 今日语言分析文本，失败返回空字符串。
    """
    import sqlite3
    
    yesterday_start, yesterday_end = get_day_range(1)
    
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("""
            SELECT user_name, content
            FROM messages
            WHERE timestamp >= ?
              AND timestamp < ?
              AND content != ''
              AND content_state != 'cleared'
            ORDER BY timestamp
        """, (yesterday_start, yesterday_end))
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        logger.error(f"读取 Tier 0 消息失败: {e}")
        return ""
    
    if not rows:
        logger.info("语言分析：Tier 0 无消息，跳过")
        return ""
    
    # 构建消息样本（最多取 100 条，避免 token 溢出）
    sample = rows[:100]
    messages_text = "\n".join(
        f"[{user_name}]: {content}" for user_name, content in sample
    )
    
    logger.info(f"语言分析：读取 {len(rows)} 条消息，采样 {len(sample)} 条")
    
    system = "你是一个群聊语言分析师。直接输出分析结果，不要思考过程。"
    user = (
        "分析以下群聊消息的语言特征，输出简洁的要点（每条一行）：\n"
        "1. 高频词汇（3-5个）\n"
        "2. 语言风格（口语化/正式/幽默/吐槽等）\n"
        "3. 常用表情/符号/语气词\n"
        "4. 今日话题倾向\n"
        "5. 特殊表达习惯\n\n"
        "消息：\n"
        f"{messages_text[:3000]}"
    )
    
    try:
        result = _call_llm(system, user, max_tokens=512)
        if result:
            # 剥离思考过程
            from memory_ai import _strip_thinking_process
            result = _strip_thinking_process(result)
        return result.strip() if result else ""
    except Exception as e:
        logger.error(f"语言分析 LLM 调用失败: {e}")
        return ""


def update_chat_analyze(new_analysis: str) -> bool:
    """
    将今日分析合并到 chat_analyze 文件中。
    
    流程：
      1. 读取已有 chat_analyze（如果存在）
      2. 调用 LLM 将旧分析 + 新分析合并为简洁摘要
      3. 写入 chat_analyze
    
    返回:
        bool — 是否成功
    """
    old_analysis = ""
    if os.path.exists(CHAT_ANALYZE_FILE):
        try:
            with open(CHAT_ANALYZE_FILE, "r", encoding="utf-8") as f:
                old_analysis = f.read().strip()
        except Exception as e:
            logger.warning(f"读取 chat_analyze 失败: {e}")
    
    if not old_analysis and not new_analysis:
        return True
    
    if not old_analysis:
        # 首次创建
        try:
            with open(CHAT_ANALYZE_FILE, "w", encoding="utf-8") as f:
                f.write(new_analysis)
            logger.info("chat_analyze 文件已创建")
            return True
        except Exception as e:
            logger.error(f"写入 chat_analyze 失败: {e}")
            return False
    
    # 合并旧分析 + 新分析
    system = "你是一个群聊语言分析师。直接输出合并结果，不要思考，不要解释。"
    user = (
        "以下是群聊语言分析的【历史记录】和【今日分析】。\n"
        "请将它们合并为一份简洁的总结（不超过15行），保留最重要的信息：\n\n"
        f"【历史记录】\n{old_analysis[:1500]}\n\n"
        f"【今日分析】\n{new_analysis[:1500]}\n\n"
        "合并后的总结："
    )
    
    try:
        merged = _call_llm(system, user)
        if merged:
            from memory_ai import _strip_thinking_process
            merged = _strip_thinking_process(merged)
        
        if not merged:
            merged = new_analysis if new_analysis else old_analysis
        
        with open(CHAT_ANALYZE_FILE, "w", encoding="utf-8") as f:
            f.write(merged.strip())
        logger.info("chat_analyze 文件已更新")
        return True
    except Exception as e:
        logger.error(f"合并 chat_analyze 失败: {e}")
        # 兜底：直接追加
        try:
            combined = old_analysis + "\n---\n" + new_analysis
            with open(CHAT_ANALYZE_FILE, "w", encoding="utf-8") as f:
                f.write(combined[:2000])
            return True
        except:
            return False


def do_maintenance():
    """执行完整维护（语言分析 → 记忆平移 → 清理）"""
    logger.info("=" * 60)
    logger.info("开始每日维护（语言分析 + 记忆平移）")
    logger.info("=" * 60)
    
    now = datetime.now()
    logger.info(f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}")
    
    db_path = MEMORY_DB_PATH
    
    # 确保索引存在
    _ensure_indexes(db_path)
    
    # ========== 步骤 0：语言分析（在记忆平移之前） ==========
    logger.info("\n--- 步骤 0/5：Tier 0 语言分析 ---")
    new_analysis = analyze_daily_language(db_path)
    if new_analysis:
        logger.info(f"今日语言分析完成（{len(new_analysis)} 字符）")
        update_chat_analyze(new_analysis)
    else:
        logger.info("今日无新消息，跳过语言分析")
    
    # ========== 步骤 1：Tier 2 → Tier 3（第7天） ==========
    logger.info("\n--- 步骤 1/5：Tier 2 → Tier 3 ---")
    stats_t2t3 = tier2_to_tier3(db_path)
    
    # ========== 步骤 2：Tier 1 → Tier 2（第3天） ==========
    logger.info("\n--- 步骤 2/5：Tier 1 → Tier 2 ---")
    stats_t1t2 = tier1_to_tier2(db_path)
    
    # ========== 步骤 3：Tier 0 → Tier 1（昨天） ==========
    logger.info("\n--- 步骤 3/5：Tier 0 → Tier 1 ---")
    stats_t0t1 = tier0_to_tier1(db_path)
    
    # ========== 步骤 4：清理检索日志 + VACUUM ==========
    logger.info("\n--- 步骤 4/5：清理 + VACUUM ---")
    log_deleted = cleanup_retrieval_log(db_path, keep_days=30)
    vacuum_db(db_path)
    
    # ========== 报告 ==========
    logger.info("\n" + "=" * 60)
    logger.info("维护完成")
    logger.info("=" * 60)
    logger.info(f"  语言分析：{'完成' if new_analysis else '跳过（无新消息）'}")
    logger.info(f"  Tier 2 → Tier 3：处理 {stats_t2t3['windows']} 窗口，合并 {stats_t2t3['merged']} 个")
    logger.info(f"  Tier 1 → Tier 2：处理 {stats_t1t2['windows']} 窗口，合并 {stats_t1t2['merged']} 个")
    logger.info(f"  Tier 0 → Tier 1：处理 {stats_t0t1['windows']} 窗口，合并 {stats_t0t1['merged']} 个")
    logger.info(f"  清理检索日志：删除 {log_deleted} 条")
    logger.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="每日维护（语言分析 + 记忆平移）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
流程（每天调用一次）：
  0. 语言分析：分析 Tier 0（昨天）消息的语言特征，更新 chat_analyze 文件
  1. Tier 2 → Tier 3：第7天的2个12小时块合并成1个天块
  2. Tier 1 → Tier 2：第3天的48个30分钟块合并成2个12小时块
  3. Tier 0 → Tier 1：昨天的消息按30分钟块合并进入 Tier 1
  4. 清理检索日志 + VACUUM
        """,
    )
    args = parser.parse_args()
    
    do_maintenance()


if __name__ == "__main__":
    main()
