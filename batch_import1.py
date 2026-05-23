"""
batch_import1.py — 历史群聊记录批量快速导入（Tier3 专用）
========================================================
与 batch_import.py 的区别：
  - 所有消息直接进入 Tier3（>14天），不保留原文，仅存关键词+事件索引。
  - 采用批量 LLM 调用：一次 API 请求处理多条消息，大幅减少 LLM 调用次数。
  - 分三阶段执行：批量插入 → 批量关键词提取 → 批量事件提取。
  - 每阶段独立支持断点续跑。

性能对比（以 9000 条消息为例）：
  batch_import.py:  ~18000 次 LLM 调用（每条 2 次）
  batch_import1.py: ~600 次 LLM 调用（每批 30 条 × 2 轮）

用法：
  python batch_import1.py --json_file chat_records_flat.json
  python batch_import1.py --json_file chat_records_flat.json --resume
  python batch_import1.py --json_file chat_records_flat.json --batch_size 50

依赖：Python 3.10+，memory_ai.py，sqlite3，json，argparse，time，logging，requests
"""

import sys
import os
import json
import time
import logging
import argparse
import sqlite3
import requests
import re
from datetime import datetime
from typing import Dict, Any, List, Optional

from memory_ai import clean_message, MEMORY_DB_PATH

# ============================================================
# 配置区
# ============================================================
try:
    from config import LLM_API_URL, LLM_MODEL_NAME, LLM_API_KEY
except ImportError:
    LLM_API_URL = "http://127.0.0.1:1234/v1/chat/completions"
    LLM_MODEL_NAME = "qwen3.5-9b"
    LLM_API_KEY = ""

LLM_TIMEOUT = 120
LLM_MAX_RETRIES = 3
BATCH_SIZE = 5
API_INTERVAL = 1.0
COMMIT_INTERVAL = 500
PROGRESS_PRINT_INTERVAL = 100

PROGRESS_FILE_PHASE1 = "progress_phase1.txt"
PROGRESS_FILE_PHASE2 = "progress_phase2.txt"
PROGRESS_FILE_PHASE3 = "progress_phase3.txt"
CONTENT_MAP_FILE = "msg_content_map.json"

# ============================================================
# 日志
# ============================================================
if not logging.getLogger("batch_import1").handlers:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
logger = logging.getLogger("batch_import1")

ERROR_LOG_PATH = os.path.join(os.path.dirname(__file__), "error_fast.log")
_error_handler = logging.FileHandler(ERROR_LOG_PATH, encoding="utf-8")
_error_handler.setLevel(logging.WARNING)
_error_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
))
logger.addHandler(_error_handler)


# ============================================================
# 参数解析
# ============================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="批量快速导入历史群聊 JSON 到 memory.db（Tier3 专用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python batch_import1.py --json_file chat_records_flat.json
  python batch_import1.py --json_file chat_records_flat.json --resume
  python batch_import1.py --json_file chat_records_flat.json --batch_size 50
        """,
    )
    parser.add_argument("--json_file", required=True, help="历史群聊 JSON 文件路径")
    parser.add_argument("--db_path", default=MEMORY_DB_PATH, help="目标数据库路径")
    parser.add_argument("--resume", action="store_true", help="断点续跑")
    parser.add_argument("--batch_size", type=int, default=BATCH_SIZE, help="每批 LLM 处理的消息数")
    return parser.parse_args()


# ============================================================
# 工具函数
# ============================================================

def load_messages(json_path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(json_path):
        raise FileNotFoundError(f"JSON 文件不存在: {json_path}")
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("JSON 顶层必须是数组。")
    valid: List[Dict[str, Any]] = []
    skipped = 0
    for item in data:
        if not isinstance(item, dict):
            skipped += 1
            continue
        if not all(k in item for k in ("user_id", "content")):
            skipped += 1
            continue
        item.setdefault("user_name", item.get("user_id", "未知"))
        item.setdefault("timestamp", "")
        valid.append(item)
    if skipped:
        logger.warning(f"跳过 {skipped} 条格式不完整的记录。")
    valid.sort(key=lambda x: x.get("timestamp", ""))
    logger.info(f"JSON 加载完成: {len(valid)} 条有效记录（跳过 {skipped} 条）。")
    return valid


def read_progress(path: str) -> int:
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (ValueError, IOError):
        return 0


def write_progress(path: str, index: int) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(index))
    except IOError as e:
        logger.error(f"写入进度文件失败: {e}")


def _call_llm(system: str, user: str, max_tokens: int = 8192, temperature: float = 0.2) -> str:
    last_error = ""
    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"
    for attempt in range(LLM_MAX_RETRIES + 1):
        try:
            resp = requests.post(
                LLM_API_URL,
                json={
                    "model": LLM_MODEL_NAME,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                headers=headers,
                timeout=LLM_TIMEOUT,
                verify=False,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except requests.exceptions.Timeout:
            last_error = f"超时（{LLM_TIMEOUT}s）"
        except requests.exceptions.ConnectionError:
            last_error = f"无法连接 {LLM_API_URL}"
        except requests.exceptions.RequestException as e:
            last_error = str(e)
        except (KeyError, IndexError, TypeError) as e:
            last_error = f"响应格式异常: {e}"
        if attempt < LLM_MAX_RETRIES:
            time.sleep(2 ** attempt)
    raise ConnectionError(f"LLM 调用失败（已重试 {LLM_MAX_RETRIES} 次）: {last_error}")


def _extract_json(text: str) -> Optional[Any]:
    """从 LLM 输出中提取 JSON 数组或对象。"""
    if not text:
        logger.debug("_extract_json: 输入为空")
        return None
    
    logger.debug(f"_extract_json 输入: {repr(text[:500])}")
    
    text = text.strip()
    
    # 尝试提取代码块
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
        logger.debug(f"_extract_json 提取代码块后: {repr(text[:300])}")
    
    # 尝试找到 JSON 开始和结束
    start = text.find("[")
    if start == -1:
        start = text.find("{")
    
    end = text.rfind("]")
    if end == -1:
        end = text.rfind("}")
    
    if start != -1 and end != -1 and start < end:
        text = text[start:end + 1]
        logger.debug(f"_extract_json 截取后: {repr(text)}")
    
    # 尝试修复常见的 JSON 问题
    # 问题1: 末尾缺少逗号或括号
    # 简单尝试: 如果以 [ 开头但不以 ] 结尾，尝试添加 ]
    if text.startswith("[") and not text.endswith("]"):
        # 尝试找到最后一个完整的对象
        last_brace = text.rfind("}")
        if last_brace != -1:
            text = text[:last_brace + 1] + "]"
            logger.debug(f"_extract_json 修复后: {repr(text)}")
    
    try:
        result = json.loads(text)
        logger.debug(f"_extract_json 解析成功: 类型={type(result)}")
        return result
    except json.JSONDecodeError as e:
        logger.debug(f"_extract_json 解析失败: {e}")
        logger.debug(f"_extract_json 尝试解析的文本: {repr(text)}")
        return None


def extract_single_keywords(text: str) -> List[str]:
    """单条提取关键词（降级方案）"""
    system = "你是一个关键词提取器。"
    user = (
        f"请为以下消息提取1-3个最重要的关键词，输出一个JSON数组（只输出数组，不要任何解释）。\n"
        f"消息：{text}"
    )
    try:
        output = _call_llm(system, user, max_tokens=256, temperature=0.2)
        parsed = _extract_json(output)
        if isinstance(parsed, list):
            return [str(k).strip() for k in parsed if str(k).strip()]
    except Exception:
        pass
    return []


def extract_single_event(text: str) -> Optional[Dict[str, str]]:
    """单条提取事件（降级方案）"""
    system = "你是一个事件抽取器。"
    user = (
        f"请将以下消息转换为一个事件JSON对象，包含 subject, verb, object, summary 字段。\n"
        f"只输出JSON对象，不要任何解释。如果没有明确事件，输出 null。\n"
        f"消息：{text}"
    )
    try:
        output = _call_llm(system, user, max_tokens=512, temperature=0.2)
        parsed = _extract_json(output)
        if isinstance(parsed, dict):
            return {
                "subject": str(parsed.get("subject", "")),
                "verb": str(parsed.get("verb", "")),
                "object": str(parsed.get("object", "")),
                "summary": str(parsed.get("summary", "")),
            }
    except Exception:
        pass
    return None


def load_content_map(map_path: str) -> Dict[int, str]:
    """加载 msg_id → 原文 映射文件。"""
    if not os.path.exists(map_path):
        return {}
    with open(map_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return {int(k): v for k, v in raw.items()}


def save_content_map(map_path: str, data: Dict[int, str]) -> None:
    with open(map_path, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in data.items()}, f, ensure_ascii=False)


# ============================================================
# Phase 1: 批量插入 messages（Tier3，content=""）
# ============================================================

def phase1_bulk_insert(
    messages: List[Dict[str, Any]],
    db_path: str,
    resume: bool,
) -> Dict[str, int]:
    """
    批量插入所有消息到 messages 表。
    - content 存空字符串（Tier3）。
    - 跳过清洗过滤的消息。
    - 跳过已存在的重复消息。
    - 同时构建 msg_id → 原文 映射，供 Phase2/3 使用。
    """
    total = len(messages)
    progress_path = os.path.join(os.path.dirname(db_path) or ".", PROGRESS_FILE_PHASE1)
    map_path = os.path.join(os.path.dirname(db_path) or ".", CONTENT_MAP_FILE)
    start_idx = read_progress(progress_path) if resume else 0

    if start_idx >= total:
        logger.info("Phase1: 所有消息已插入完毕。")
        return {"total": total, "inserted": 0, "skipped_dup": 0, "skipped_filtered": 0}

    content_map = load_content_map(map_path) if resume else {}

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.cursor()

    stats = {"total": total, "inserted": 0, "skipped_dup": 0, "skipped_filtered": 0}
    t_start = time.time()

    logger.info(f"====== Phase1: 批量插入消息（索引 {start_idx} → {total - 1}） ======")

    for i in range(start_idx, total):
        msg = messages[i]
        user_id = str(msg.get("user_id", ""))
        user_name = str(msg.get("user_name", "未知"))
        content = str(msg.get("content", ""))
        timestamp = str(msg.get("timestamp", ""))

        cleaned = clean_message(content)
        if cleaned is False:
            stats["skipped_filtered"] += 1
            if (i + 1) % PROGRESS_PRINT_INTERVAL == 0:
                _print_phase_progress("Phase1", i, total, stats, t_start)
            write_progress(progress_path, i + 1)
            continue

        cur.execute(
            "SELECT 1 FROM messages WHERE user_id = ? AND content = ? AND timestamp = ? LIMIT 1",
            (user_id, cleaned, timestamp),
        )
        if cur.fetchone():
            stats["skipped_dup"] += 1
            if (i + 1) % PROGRESS_PRINT_INTERVAL == 0:
                _print_phase_progress("Phase1", i, total, stats, t_start)
            write_progress(progress_path, i + 1)
            continue

        cur.execute(
            "INSERT INTO messages (user_id, user_name, content, timestamp) VALUES (?, ?, ?, ?)",
            (user_id, user_name, "", timestamp),
        )
        msg_id = cur.lastrowid
        content_map[msg_id] = cleaned
        stats["inserted"] += 1

        if (i + 1) % COMMIT_INTERVAL == 0:
            conn.commit()
            save_content_map(map_path, content_map)
            _print_phase_progress("Phase1", i, total, stats, t_start)

        if (i + 1) % PROGRESS_PRINT_INTERVAL == 0:
            _print_phase_progress("Phase1", i, total, stats, t_start)

        write_progress(progress_path, i + 1)

    conn.commit()
    conn.close()
    save_content_map(map_path, content_map)

    elapsed = time.time() - t_start
    logger.info(f"Phase1 完成: 插入 {stats['inserted']} 条，"
                f"跳过重复 {stats['skipped_dup']}，过滤 {stats['skipped_filtered']}，"
                f"耗时 {elapsed:.1f}s")

    if os.path.exists(progress_path):
        os.remove(progress_path)

    return stats


# ============================================================
# Phase 2: 批量关键词提取
# ============================================================

def phase2_batch_keywords(
    db_path: str,
    batch_size: int,
    resume: bool,
) -> Dict[str, int]:
    """
    批量提取关键词。
    
    流程：
      1. 从 messages 表读取所有 content="" 且无关键词的消息。
      2. 从 msg_content_map.json 获取原文。
      3. 按 batch_size 分批，每批调用一次 LLM 提取所有消息的关键词。
      4. 解析 JSON 结果，批量写入 keywords 表。
    """
    map_path = os.path.join(os.path.dirname(db_path) or ".", CONTENT_MAP_FILE)
    content_map = load_content_map(map_path)
    if not content_map:
        logger.info("Phase2: 无内容映射，跳过。")
        return {"total": 0, "processed": 0, "batches": 0, "errors": 0}

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    cur = conn.cursor()

    cur.execute("""
        SELECT m.id, m.user_name, m.timestamp
        FROM messages m
        WHERE m.content = ''
          AND m.id NOT IN (SELECT DISTINCT msg_id FROM keywords)
        ORDER BY m.id
    """)
    rows = cur.fetchall()
    conn.close()

    total = len(rows)
    if total == 0:
        logger.info("Phase2: 所有消息已有关键词，跳过。")
        return {"total": 0, "processed": 0, "batches": 0, "errors": 0}

    progress_path = os.path.join(os.path.dirname(db_path) or ".", PROGRESS_FILE_PHASE2)
    start_batch = read_progress(progress_path) if resume else 0

    total_batches = (total + batch_size - 1) // batch_size
    if start_batch >= total_batches:
        logger.info("Phase2: 所有批次已处理完毕。")
        return {"total": total, "processed": 0, "batches": 0, "errors": 0}

    stats = {"total": total, "processed": 0, "batches": 0, "errors": 0}
    t_start = time.time()
    last_api_time = 0.0

    logger.info(f"====== Phase2: 批量关键词提取（{total_batches} 批，每批 ≤{batch_size} 条） ======")

    for batch_idx in range(start_batch, total_batches):
        start = batch_idx * batch_size
        end = min(start + batch_size, total)
        batch_rows = rows[start:end]

        # 组装批量提示词：每条消息编号 + 内容
        msg_parts: List[str] = []
        valid_indices: List[int] = []
        for j, (msg_id, user_name, ts) in enumerate(batch_rows):
            text = content_map.get(msg_id, "")
            if not text:
                continue
            msg_parts.append(f"[{j}] {text}")
            valid_indices.append(j)

        if not msg_parts:
            stats["batches"] += 1
            write_progress(progress_path, batch_idx + 1)
            continue

        msg_block = "\n".join(msg_parts)

        system = "你是一个关键词提取器。直接输出合法JSON数组，不要思考，不要解释。"
        user = (
            "请为以下每条消息提取1-3个最重要的关键词。\n"
            "输出格式：严格JSON数组，每个元素包含 msg_index(整数) 和 keywords(字符串数组)。\n"
            "示例：\n"
            "[{\"msg_index\":0,\"keywords\":[\"游戏\",\"组队\"]},{\"msg_index\":2,\"keywords\":[\"吃饭\",\"约饭\"]}]\n"
            f"消息列表：\n{msg_block}"
        )

        elapsed = time.time() - last_api_time
        if elapsed < API_INTERVAL:
            time.sleep(API_INTERVAL - elapsed)

        success = False
        for attempt in range(LLM_MAX_RETRIES + 1):
            try:
                logger.debug(f"Phase2 批次 {batch_idx} 尝试 {attempt + 1}/{LLM_MAX_RETRIES + 1}...")
                last_api_time = time.time()
                output = _call_llm(system, user, temperature=0.2)
                
                logger.debug(f"Phase2 批次 {batch_idx} LLM 返回类型: {type(output)}, 长度: {len(output) if output else 0}")
                if output:
                    logger.debug(f"Phase2 批次 {batch_idx} LLM 返回内容: {repr(output[:300])}")
                
                if not output:
                    logger.warning(f"Phase2 批次 {batch_idx}: LLM 返回空内容！")
                    if attempt < LLM_MAX_RETRIES:
                        time.sleep(2 ** attempt)
                        continue
                
                parsed = _extract_json(output)

                if isinstance(parsed, list):
                    conn = sqlite3.connect(db_path)
                    cur2 = conn.cursor()
                    inserted = 0
                    for item in parsed:
                        if not isinstance(item, dict):
                            continue
                        idx = item.get("msg_index", -1)
                        kws = item.get("keywords", [])
                        if idx < 0 or idx >= len(batch_rows) or not kws:
                            continue
                        msg_id = batch_rows[idx][0]
                        cur2.executemany(
                            "INSERT INTO keywords (keyword, msg_id) VALUES (?, ?)",
                            [(str(kw).strip(), msg_id) for kw in kws if str(kw).strip()],
                        )
                        inserted += 1
                    conn.commit()
                    conn.close()
                    stats["processed"] += inserted
                else:
                    logger.warning(f"Phase2 批次 {batch_idx}: LLM 返回非数组格式。类型={type(parsed)}")
                    logger.warning(f"Phase2 批次 {batch_idx} 完整输出: {repr(output)}")

                success = True
                break
            except Exception as e:
                logger.warning(f"Phase2 批次 {batch_idx} 第 {attempt + 1} 次失败: {e}")
                if attempt < LLM_MAX_RETRIES:
                    time.sleep(2 ** attempt)
                else:
                    stats["errors"] += 1

        stats["batches"] += 1
        write_progress(progress_path, batch_idx + 1)

        if (batch_idx + 1) % 10 == 0:
            elapsed_total = time.time() - t_start
            rate = (batch_idx + 1 - start_batch) / max(elapsed_total, 0.1)
            logger.info(f"Phase2 [{batch_idx + 1}/{total_batches}] 批次，"
                        f"已处理 {stats['processed']} 条，速率 {rate:.1f} 批/秒")

    elapsed = time.time() - t_start
    logger.info(f"Phase2 批量完成: {stats['batches']} 批次，处理 {stats['processed']} 条，"
                f"{stats['errors']} 错误，耗时 {elapsed:.1f}s")
    
    # ---- 检查是否还有未处理的，降级到单条处理 ----
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM messages m
        WHERE m.content = ''
          AND m.id NOT IN (SELECT DISTINCT msg_id FROM keywords)
    """)
    remaining = cur.fetchone()[0]
    conn.close()
    
    if remaining > 0:
        logger.info(f"====== Phase2: 还有 {remaining} 条未处理，降级到单条模式 ======")
        single_stats = phase2_single_keywords(db_path, content_map)
        stats["processed"] += single_stats["processed"]
        stats["errors"] += single_stats["errors"]
    
    if os.path.exists(progress_path):
        os.remove(progress_path)
    return stats


def phase2_single_keywords(
    db_path: str,
    content_map: Dict[int, str],
) -> Dict[str, int]:
    """单条提取关键词（降级方案）"""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    cur = conn.cursor()
    
    cur.execute("""
        SELECT m.id, m.user_name, m.timestamp
        FROM messages m
        WHERE m.content = ''
          AND m.id NOT IN (SELECT DISTINCT msg_id FROM keywords)
        ORDER BY m.id
    """)
    rows = cur.fetchall()
    conn.close()
    
    total = len(rows)
    if total == 0:
        return {"total": 0, "processed": 0, "errors": 0}
    
    logger.info(f"Phase2 单条模式: 开始处理 {total} 条...")
    
    stats = {"total": total, "processed": 0, "errors": 0}
    t_start = time.time()
    last_api_time = 0.0
    
    for idx, (msg_id, user_name, ts) in enumerate(rows):
        text = content_map.get(msg_id, "")
        if not text:
            continue
        
        elapsed = time.time() - last_api_time
        if elapsed < API_INTERVAL:
            time.sleep(API_INTERVAL - elapsed)
        
        success = False
        for attempt in range(LLM_MAX_RETRIES + 1):
            try:
                last_api_time = time.time()
                keywords = extract_single_keywords(text)
                if keywords:
                    conn2 = sqlite3.connect(db_path)
                    cur2 = conn2.cursor()
                    cur2.executemany(
                        "INSERT INTO keywords (keyword, msg_id) VALUES (?, ?)",
                        [(kw, msg_id) for kw in keywords]
                    )
                    conn2.commit()
                    conn2.close()
                    stats["processed"] += 1
                success = True
                break
            except Exception as e:
                logger.warning(f"Phase2 单条 [{idx}/{total}] 第 {attempt + 1} 次失败: {e}")
                if attempt < LLM_MAX_RETRIES:
                    time.sleep(2 ** attempt)
                else:
                    stats["errors"] += 1
        
        if (idx + 1) % PROGRESS_PRINT_INTERVAL == 0:
            elapsed_total = time.time() - t_start
            logger.info(f"Phase2 单条 [{idx + 1}/{total}] 已处理 {stats['processed']} 条...")
    
    elapsed_total = time.time() - t_start
    logger.info(f"Phase2 单条完成: 处理 {stats['processed']} 条，错误 {stats['errors']}，耗时 {elapsed_total:.1f}s")
    return stats


# ============================================================
# Phase 3: 批量事件提取
# ============================================================

def phase3_batch_events(
    db_path: str,
    batch_size: int,
    resume: bool,
) -> Dict[str, int]:
    """
    批量提取事件。
    
    流程：
      1. 从 messages 表读取所有 content="" 且无事件的消息。
      2. 从 msg_content_map.json 获取原文。
      3. 按 batch_size 分批，每批调用一次 LLM 提取所有消息的事件。
      4. 解析 JSON 结果，批量写入 events 表。
    """
    map_path = os.path.join(os.path.dirname(db_path) or ".", CONTENT_MAP_FILE)
    content_map = load_content_map(map_path)
    if not content_map:
        logger.info("Phase3: 无内容映射，跳过。")
        return {"total": 0, "processed": 0, "batches": 0, "errors": 0}

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    cur = conn.cursor()

    cur.execute("""
        SELECT m.id, m.user_name, m.timestamp
        FROM messages m
        WHERE m.content = ''
          AND m.id NOT IN (SELECT DISTINCT msg_id FROM events WHERE msg_id != -1)
        ORDER BY m.id
    """)
    rows = cur.fetchall()
    conn.close()

    total = len(rows)
    if total == 0:
        logger.info("Phase3: 所有消息已有事件，跳过。")
        return {"total": 0, "processed": 0, "batches": 0, "errors": 0}

    progress_path = os.path.join(os.path.dirname(db_path) or ".", PROGRESS_FILE_PHASE3)
    start_batch = read_progress(progress_path) if resume else 0

    total_batches = (total + batch_size - 1) // batch_size
    if start_batch >= total_batches:
        logger.info("Phase3: 所有批次已处理完毕。")
        return {"total": total, "processed": 0, "batches": 0, "errors": 0}

    stats = {"total": total, "processed": 0, "batches": 0, "errors": 0}
    t_start = time.time()
    last_api_time = 0.0

    logger.info(f"====== Phase3: 批量事件提取（{total_batches} 批，每批 ≤{batch_size} 条） ======")

    for batch_idx in range(start_batch, total_batches):
        start = batch_idx * batch_size
        end = min(start + batch_size, total)
        batch_rows = rows[start:end]

        msg_parts: List[str] = []
        for j, (msg_id, user_name, ts) in enumerate(batch_rows):
            text = content_map.get(msg_id, "")
            if not text:
                continue
            msg_parts.append(f"[{j}] {text}")

        if not msg_parts:
            stats["batches"] += 1
            write_progress(progress_path, batch_idx + 1)
            continue

        msg_block = "\n".join(msg_parts)

        system = "你是一个事件抽取器。直接输出合法JSON数组，不要思考，不要解释。"
        user = (
            "请将以下每条群聊消息转换为事件JSON。\n"
            "输出格式：严格JSON数组，每个元素包含 msg_index(整数) 和 event(对象，含subject/verb/object/summary)。\n"
            "无事件的消息 event 设为 null。\n"
            "示例：\n"
            "[{\"msg_index\":0,\"event\":{\"subject\":\"小明\",\"verb\":\"说\",\"object\":\"游戏\",\"summary\":\"小明说要玩游戏\"}},{\"msg_index\":2,\"event\":null}]\n"
            f"消息列表：\n{msg_block}"
        )

        elapsed = time.time() - last_api_time
        if elapsed < API_INTERVAL:
            time.sleep(API_INTERVAL - elapsed)

        success = False
        for attempt in range(LLM_MAX_RETRIES + 1):
            try:
                logger.debug(f"Phase3 批次 {batch_idx} 尝试 {attempt + 1}/{LLM_MAX_RETRIES + 1}...")
                last_api_time = time.time()
                output = _call_llm(system, user, temperature=0.2)
                
                logger.debug(f"Phase3 批次 {batch_idx} LLM 返回类型: {type(output)}, 长度: {len(output) if output else 0}")
                if output:
                    logger.debug(f"Phase3 批次 {batch_idx} LLM 返回内容: {repr(output[:300])}")
                
                if not output:
                    logger.warning(f"Phase3 批次 {batch_idx}: LLM 返回空内容！")
                    if attempt < LLM_MAX_RETRIES:
                        time.sleep(2 ** attempt)
                        continue
                
                parsed = _extract_json(output)

                if isinstance(parsed, list):
                    conn = sqlite3.connect(db_path)
                    cur2 = conn.cursor()
                    inserted = 0
                    for item in parsed:
                        if not isinstance(item, dict):
                            continue
                        idx = item.get("msg_index", -1)
                        ev = item.get("event")
                        if idx < 0 or idx >= len(batch_rows) or ev is None:
                            continue
                        if not isinstance(ev, dict) or not ev:
                            continue
                        msg_id = batch_rows[idx][0]
                        user_name = batch_rows[idx][1]
                        ts = batch_rows[idx][2]
                        cur2.execute(
                            "INSERT INTO events (timestamp, user_name, subject, verb, object, summary, msg_id) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (
                                ts,
                                user_name,
                                str(ev.get("subject", "")),
                                str(ev.get("verb", "")),
                                str(ev.get("object", "")),
                                str(ev.get("summary", "")),
                                msg_id,
                            ),
                        )
                        inserted += 1
                    conn.commit()
                    conn.close()
                    stats["processed"] += inserted
                else:
                    logger.warning(f"Phase3 批次 {batch_idx}: LLM 返回非数组格式。类型={type(parsed)}")
                    logger.warning(f"Phase3 批次 {batch_idx} 完整输出: {repr(output)}")

                success = True
                break
            except Exception as e:
                logger.warning(f"Phase3 批次 {batch_idx} 第 {attempt + 1} 次失败: {e}")
                if attempt < LLM_MAX_RETRIES:
                    time.sleep(2 ** attempt)
                else:
                    stats["errors"] += 1

        stats["batches"] += 1
        write_progress(progress_path, batch_idx + 1)

        if (batch_idx + 1) % 10 == 0:
            elapsed_total = time.time() - t_start
            rate = (batch_idx + 1 - start_batch) / max(elapsed_total, 0.1)
            logger.info(f"Phase3 [{batch_idx + 1}/{total_batches}] 批次，"
                        f"已处理 {stats['processed']} 条，速率 {rate:.1f} 批/秒")

    elapsed = time.time() - t_start
    logger.info(f"Phase3 批量完成: {stats['batches']} 批次，处理 {stats['processed']} 条，"
                f"{stats['errors']} 错误，耗时 {elapsed:.1f}s")
    
    # ---- 检查是否还有未处理的，降级到单条处理 ----
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM messages m
        WHERE m.content = ''
          AND m.id NOT IN (SELECT DISTINCT msg_id FROM events WHERE msg_id != -1)
    """)
    remaining = cur.fetchone()[0]
    conn.close()
    
    if remaining > 0:
        logger.info(f"====== Phase3: 还有 {remaining} 条未处理，降级到单条模式 ======")
        single_stats = phase3_single_events(db_path, content_map)
        stats["processed"] += single_stats["processed"]
        stats["errors"] += single_stats["errors"]
    
    if os.path.exists(progress_path):
        os.remove(progress_path)
    return stats


def phase3_single_events(
    db_path: str,
    content_map: Dict[int, str],
) -> Dict[str, int]:
    """单条提取事件（降级方案）"""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    cur = conn.cursor()
    
    cur.execute("""
        SELECT m.id, m.user_name, m.timestamp
        FROM messages m
        WHERE m.content = ''
          AND m.id NOT IN (SELECT DISTINCT msg_id FROM events WHERE msg_id != -1)
        ORDER BY m.id
    """)
    rows = cur.fetchall()
    conn.close()
    
    total = len(rows)
    if total == 0:
        return {"total": 0, "processed": 0, "errors": 0}
    
    logger.info(f"Phase3 单条模式: 开始处理 {total} 条...")
    
    stats = {"total": total, "processed": 0, "errors": 0}
    t_start = time.time()
    last_api_time = 0.0
    
    for idx, (msg_id, user_name, ts) in enumerate(rows):
        text = content_map.get(msg_id, "")
        if not text:
            continue
        
        elapsed = time.time() - last_api_time
        if elapsed < API_INTERVAL:
            time.sleep(API_INTERVAL - elapsed)
        
        success = False
        for attempt in range(LLM_MAX_RETRIES + 1):
            try:
                last_api_time = time.time()
                event = extract_single_event(text)
                if event:
                    conn2 = sqlite3.connect(db_path)
                    cur2 = conn2.cursor()
                    cur2.execute(
                        "INSERT INTO events (timestamp, user_name, subject, verb, object, summary, msg_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (ts, user_name, event["subject"], event["verb"], event["object"], event["summary"], msg_id)
                    )
                    conn2.commit()
                    conn2.close()
                    stats["processed"] += 1
                success = True
                break
            except Exception as e:
                logger.warning(f"Phase3 单条 [{idx}/{total}] 第 {attempt + 1} 次失败: {e}")
                if attempt < LLM_MAX_RETRIES:
                    time.sleep(2 ** attempt)
                else:
                    stats["errors"] += 1
        
        if (idx + 1) % PROGRESS_PRINT_INTERVAL == 0:
            elapsed_total = time.time() - t_start
            logger.info(f"Phase3 单条 [{idx + 1}/{total}] 已处理 {stats['processed']} 条...")
    
    elapsed_total = time.time() - t_start
    logger.info(f"Phase3 单条完成: 处理 {stats['processed']} 条，错误 {stats['errors']}，耗时 {elapsed_total:.1f}s")
    return stats


# ============================================================
# 辅助
# ============================================================

def _print_phase_progress(
    phase: str, idx: int, total: int, stats: Dict[str, int], t_start: float
) -> None:
    pct = (idx + 1) / total * 100
    elapsed = time.time() - t_start
    rate = (idx + 1) / max(elapsed, 0.1)
    logger.info(
        f"[{phase}] [{idx + 1}/{total}] {pct:.1f}% | "
        f"插{stats.get('inserted', 0)} 重{stats.get('skipped_dup', 0)} "
        f"滤{stats.get('skipped_filtered', 0)} | {rate:.1f}条/秒"
    )


# ============================================================
# 主入口
# ============================================================

def run_fast_import(
    json_file: str,
    db_path: str,
    resume: bool,
    batch_size: int,
) -> Dict[str, Any]:
    """
    三阶段快速导入。
    
    阶段：
      Phase1 — 批量插入 messages（content=""，Tier3）+ 构建原文映射
      Phase2 — 批量 LLM 提取关键词
      Phase3 — 批量 LLM 提取事件
    """
    messages = load_messages(json_file)
    if not messages:
        logger.warning("无有效消息。")
        return {}

    logger.info(f"配置: batch_size={batch_size}, db={db_path}")

    # ---- Phase 1: 批量插入 ----
    logger.info("\n" + "=" * 60)
    logger.info("  Phase 1/3: 批量插入消息（Tier3，content=\"\"）")
    logger.info("=" * 60)
    p1_stats = phase1_bulk_insert(messages, db_path, resume)

    # ---- Phase 2: 批量关键词 ----
    logger.info("\n" + "=" * 60)
    logger.info("  Phase 2/3: 批量 LLM 提取关键词")
    logger.info("=" * 60)
    p2_stats = phase2_batch_keywords(db_path, batch_size, resume)

    # ---- Phase 3: 批量事件 ----
    logger.info("\n" + "=" * 60)
    logger.info("  Phase 3/3: 批量 LLM 提取事件")
    logger.info("=" * 60)
    p3_stats = phase3_batch_events(db_path, batch_size, resume)

    # ---- 汇总 ----
    logger.info("\n" + "=" * 60)
    logger.info("  全部完成！")
    logger.info("=" * 60)
    logger.info(f"  Phase1 插入: {p1_stats.get('inserted', 0)} 条")
    logger.info(f"  Phase2 关键词: {p2_stats.get('batches', 0)} 批次，处理 {p2_stats.get('processed', 0)} 条")
    logger.info(f"  Phase3 事件:   {p3_stats.get('batches', 0)} 批次，处理 {p3_stats.get('processed', 0)} 条")

    return {"phase1": p1_stats, "phase2": p2_stats, "phase3": p3_stats}


if __name__ == "__main__":
    args = parse_args()
    if not os.path.exists(args.json_file):
        logger.error(f"JSON 文件不存在: {args.json_file}")
        sys.exit(1)

    logger.info(f"JSON: {args.json_file}")
    logger.info(f"DB:   {args.db_path}")
    logger.info(f"批次大小: {args.batch_size}")
    logger.info(f"断点续跑: {'是' if args.resume else '否'}")

    try:
        run_fast_import(
            json_file=args.json_file,
            db_path=args.db_path,
            resume=args.resume,
            batch_size=args.batch_size,
        )
    except KeyboardInterrupt:
        logger.warning("用户中断。可使用 --resume 继续。")
        sys.exit(130)
    except Exception as e:
        logger.exception(f"异常终止: {e}")
        sys.exit(1)
