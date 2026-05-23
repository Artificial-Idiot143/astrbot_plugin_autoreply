"""
batch_import.py — 历史群聊记录离线批量导入脚本（按时间块处理）
============================================================

功能：
  读取历史 JSON 格式的群聊记录，按时间块分组后存入 memory.db。
  支持分级存储、断点续跑、去重、频率控制、错误重试。

时间块定义：
  - Tier 1（≤2天）：30分钟一块
  - Tier 2（3-6天）：12小时一块
  - Tier 3（≥7天）：1天一块

用法：
  python batch_import.py --json_file history.json
  python batch_import.py --json_file history.json --db_path my_memory.db --resume

依赖：Python 3.10+，memory_ai.py（同目录），sqlite3，json，argparse，time，logging
"""

import sys
import os
import json
import time
import logging
import argparse
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

# 确保能导入同目录的 memory_ai
from memory_ai import MemoryDB, MEMORY_DB_PATH

# ============================================================
# 日志配置
# ============================================================
if not logging.getLogger("batch_import").handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
logger = logging.getLogger("batch_import")

# 错误日志单独写入文件
ERROR_LOG_PATH = os.path.join(os.path.dirname(__file__), "error.log")
_error_handler = logging.FileHandler(ERROR_LOG_PATH, encoding="utf-8")
_error_handler.setLevel(logging.WARNING)
_error_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
))
logger.addHandler(_error_handler)

# ============================================================
# 常量
# ============================================================
PROGRESS_FILE = "progress.txt"          # 断点续跑进度文件
API_INTERVAL = 0.3                      # API 调用最小间隔（秒）
COMMIT_INTERVAL = 1000                  # 每 N 条打印汇总进度
PROGRESS_PRINT_INTERVAL = 100           # 每 N 条打印简要进度
MAX_RETRIES = 2                         # LLM 失败最大重试次数
MAX_MESSAGES_PER_BLOCK_BEFORE_SUMMARIZE = 5  # 每个块超过5条消息就先化简


# ============================================================
# 工具函数：时间块处理
# ============================================================

def get_window_key(dt: datetime, tier: int) -> str:
    """
    获取窗口键（固定时间格子划分）
    
    时间划分方式：
      - Tier 1（30分钟）：从 00:00 到 23:30 共 48 个格子
      - Tier 2（12小时）：从 00:00 到 12:00 共 2 个格子
      - Tier 3（1天）：1个格子
    """
    if tier == 1:
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


def get_message_tier(timestamp_str: str) -> int:
    """
    根据时间戳判断消息所属层级。
    
    返回:
        1 — ≤2天（Tier1：30分钟一块）
        2 — 3-6天（Tier2：12小时一块）
        3 — ≥7天（Tier3：1天一块）
    
    无法解析的时间戳视为 Tier3（保守策略）。
    """
    if not timestamp_str:
        return 3
    try:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                msg_dt = datetime.strptime(timestamp_str[:19], fmt)
                break
            except ValueError:
                continue
        else:
            return 3
        days = (datetime.now() - msg_dt).days
        if days <= 2:
            return 1
        elif days <= 6:
            return 2
        else:
            return 3
    except Exception:
        return 3


def summarize_five_messages(messages: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    输入最多5条消息（格式同 chat_records_flat.json），返回 AI 总结 + 平均时间。
    
    返回:
        {"summary": "...", "avg_timestamp": "...", "keywords": ["...", "..."], "source": "ai|rule|fallback"}
        失败返回 None
    """
    if len(messages) == 0:
        return None
    
    # 计算平均时间
    try:
        timestamps = []
        for m in messages:
            ts_str = m.get("timestamp", "")
            if not ts_str:
                continue
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(ts_str[:19], fmt)
                    timestamps.append(dt)
                    break
                except ValueError:
                    continue
        if not timestamps:
            avg_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        else:
            avg_seconds = sum((dt - datetime(1970, 1, 1)).total_seconds() for dt in timestamps) / len(timestamps)
            avg_dt = datetime.fromtimestamp(avg_seconds)
            avg_timestamp = avg_dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        avg_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 调用 LLM 总结
    try:
        from memory_ai import merge_messages
        merge_input = [
            {
                "user_name": m.get("user_name", "未知"),
                "content": m.get("content", ""),
                "timestamp": m.get("timestamp", "")
            }
            for m in messages
        ]
        result = merge_messages(merge_input)
        summary = result.get("summary", "")
        keywords = result.get("keywords", [])
        source = result.get("source", "unknown")
        
        return {
            "summary": summary,
            "avg_timestamp": avg_timestamp,
            "keywords": keywords,
            "source": source
        }
    except Exception as e:
        logger.warning(f"summarize_five_messages 失败: {e}")
        return None


def process_message_block(messages: List[Dict[str, Any]], tier: int) -> List[Dict[str, Any]]:
    """
    处理一个时间块的消息。
    循环合并，直到只剩1条总结！
    
    返回:
        处理后的消息列表（应该只有1条）
    """
    current = messages.copy()
    last_api_time = 0.0
    max_iterations = 10  # 安全上限，防止无限循环
    
    while len(current) > 1 and max_iterations > 0:
        max_iterations -= 1
        logger.info(f"  时间块有 {len(current)} 条消息，开始化简")
        
        result: List[Dict[str, Any]] = []
        i = 0
        total = len(current)
        
        while i < total:
            chunk = current[i:i+5]
            i += 5
            
            if len(chunk) == 0:
                continue
            
            # 频率控制
            elapsed = time.time() - last_api_time
            if elapsed < API_INTERVAL:
                time.sleep(API_INTERVAL - elapsed)
            
            last_api_time = time.time()
            
            # 调用总结
            summary_result = summarize_five_messages(chunk)
            
            if summary_result:
                source = summary_result.get("source", "unknown")
                source_label = {
                    "ai": "[AI总结]",
                    "rule": "[规则合并]",
                    "fallback": "[兜底合并]",
                    "unknown": "[未知来源]"
                }.get(source, f"[{source}]")
                
                summarized_msg = {
                    "user_id": "system",
                    "user_name": "system",
                    "content": summary_result["summary"],
                    "timestamp": summary_result["avg_timestamp"],
                    "keywords": summary_result.get("keywords", []),
                    "source": source,
                }
                result.append(summarized_msg)
                logger.info(f"    化简: [{i-5+1}~{i}]/{total} → 1条总结 {source_label} 摘要: {summary_result['summary']}")
            else:
                # 总结失败，保留原消息
                result.extend(chunk)
                logger.warning(f"    化简: [{i-5+1}~{i}]/{total} 失败，保留原消息")
        
        current = result
        logger.info(f"  化简完成: {total}条 → {len(current)}条")
    
    logger.info(f"  时间块处理完成，最终 {len(current)} 条总结")
    return current


def group_messages_by_time_blocks(messages: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """
    将消息按时间块分组。
    
    返回:
        {window_key: [message1, message2, ...]}
    """
    blocks: Dict[str, List[Dict[str, Any]]] = {}
    
    for msg in messages:
        ts_str = msg.get("timestamp", "")
        if not ts_str:
            continue
        
        # 解析时间戳
        msg_dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                msg_dt = datetime.strptime(ts_str[:19], fmt)
                break
            except ValueError:
                continue
        
        if not msg_dt:
            continue
        
        # 判断层级并获取窗口键
        tier = get_message_tier(ts_str)
        window_key = get_window_key(msg_dt, tier)
        
        if window_key not in blocks:
            blocks[window_key] = []
        blocks[window_key].append(msg)
    
    return blocks


def get_window_key_sort_key(window_key: str) -> tuple:
    """
    将 window_key 转换为可排序的 tuple，确保按时间由近到远排序（tier1 -> tier2 -> tier3）。
    
    返回:
        (tier_priority, datetime), 其中 tier_priority: 0=tier1, 1=tier2, 2=tier3
    """
    try:
        if window_key.startswith("t1_"):
            # t1_20260505_1230
            time_part = window_key[3:]
            dt = datetime.strptime(time_part, "%Y%m%d_%H%M")
            return (0, -dt.timestamp())  # tier1 优先级最高，时间近的在前
        elif window_key.startswith("t2_"):
            # t2_20260505_12
            time_part = window_key[3:]
            dt = datetime.strptime(time_part, "%Y%m%d_%H")
            return (1, -dt.timestamp())
        elif window_key.startswith("t3_"):
            # t3_20260505
            time_part = window_key[3:]
            dt = datetime.strptime(time_part, "%Y%m%d")
            return (2, -dt.timestamp())
        else:
            return (999, 0)
    except Exception:
        return (999, 0)


# ============================================================
# 工具函数
# ============================================================

def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="批量导入历史群聊 JSON 记录到 memory.db（按时间块处理）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python batch_import.py --json_file history.json
  python batch_import.py --json_file history.json --db_path my_memory.db --resume
        """,
    )
    parser.add_argument(
        "--json_file", required=True,
        help="历史群聊 JSON 文件路径",
    )
    parser.add_argument(
        "--db_path", default=MEMORY_DB_PATH,
        help=f"目标 SQLite 数据库路径（默认: {MEMORY_DB_PATH}）",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="启用断点续跑（从 progress.txt 读取上次中断位置）",
    )
    return parser.parse_args()


def load_messages(json_path: str) -> List[Dict[str, Any]]:
    """
    加载并校验 JSON 文件。
    期望格式: [{"timestamp":"...","user_id":"...","user_name":"...","content":"..."}, ...]
    按 timestamp 降序排列（由近到远）。
    """
    if not os.path.exists(json_path):
        raise FileNotFoundError(f"JSON 文件不存在: {json_path}")

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("JSON 顶层必须是数组。")

    # 校验必要字段，过滤无效条目
    valid: List[Dict[str, Any]] = []
    skipped = 0
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            skipped += 1
            continue
        if not all(k in item for k in ("user_id", "content")):
            skipped += 1
            continue
        # 补全缺失字段
        item.setdefault("user_name", item.get("user_id", "未知"))
        item.setdefault("timestamp", "")
        valid.append(item)

    if skipped:
        logger.warning(f"跳过 {skipped} 条格式不完整的记录。")

    # 按 timestamp 降序排列（由近到远）
    valid.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

    logger.info(f"JSON 加载完成: {len(valid)} 条有效记录（跳过 {skipped} 条）。")
    return valid


def read_progress(progress_path: str) -> int:
    """读取断点续跑进度，返回已处理的消息索引（从0开始）。"""
    if not os.path.exists(progress_path):
        return 0
    try:
        with open(progress_path, "r", encoding="utf-8") as f:
            idx = int(f.read().strip())
        logger.info(f"断点续跑: 从索引 {idx} 继续（已处理 {idx} 条）。")
        return idx
    except (ValueError, IOError) as e:
        logger.warning(f"进度文件损坏，从头开始: {e}")
        return 0


def write_progress(progress_path: str, index: int) -> None:
    """写入当前进度。"""
    try:
        with open(progress_path, "w", encoding="utf-8") as f:
            f.write(str(index))
    except IOError as e:
        logger.error(f"写入进度文件失败: {e}")


# ============================================================
# 主流程
# ============================================================

def run_import(
    json_file: str,
    db_path: str,
    resume: bool = False,
) -> Dict[str, int]:
    """
    执行批量导入（按时间块处理）。
    
    返回:
        dict — 统计信息
    """
    # ---- 加载消息 ----
    messages = load_messages(json_file)
    total = len(messages)
    if total == 0:
        logger.warning("无有效消息可导入。")
        return {"total": 0, "stored": 0, "skipped_dup": 0, "skipped_filtered": 0, "errors": 0, "blocks_processed": 0}

    # ---- 按时间块分组 ----
    logger.info("=" * 60)
    logger.info("开始按时间块分组")
    logger.info("=" * 60)
    
    blocks = group_messages_by_time_blocks(messages)
    logger.info(f"  分成 {len(blocks)} 个时间块")
    
    # 处理每个块（超过5条的先化简）
    processed_blocks: List[Dict[str, Any]] = []
    # 按时间由近到远排序（tier1 -> tier2 -> tier3）
    block_keys_sorted = sorted(blocks.keys(), key=get_window_key_sort_key)
    
    for window_key in block_keys_sorted:
        block_messages = blocks[window_key]
        tier = 1 if window_key.startswith("t1") else 2 if window_key.startswith("t2") else 3
        
        logger.info(f"  处理块: {window_key} (Tier{tier}) - {len(block_messages)}条消息")
        processed_messages = process_message_block(block_messages, tier)
        processed_blocks.extend(processed_messages)
    
    logger.info("=" * 60)
    logger.info(f"时间块处理完成: {total}条 → {len(processed_blocks)}条")
    logger.info("=" * 60)
    
    # 准备导入的消息列表（已处理）
    import_messages = processed_blocks
    import_total = len(import_messages)

    # ---- 初始化数据库 ----
    logger.info(f"连接数据库: {db_path}")
    db = MemoryDB(db_path)

    # ---- 断点续跑 ----
    progress_path = os.path.join(os.path.dirname(db_path) or ".", PROGRESS_FILE)
    start_idx = read_progress(progress_path) if resume else 0
    if start_idx >= import_total:
        logger.info("所有消息已处理完毕，无需继续。")
        return {"total": total, "stored": 0, "skipped_dup": 0, "skipped_filtered": 0, "errors": 0, "blocks_processed": len(blocks)}

    # ---- 统计 ----
    stats = {"total": total, "import_total": import_total, "stored": 0, "skipped_dup": 0, 
             "skipped_filtered": 0, "errors": 0, "blocks_processed": len(blocks),
             "tier1": 0, "tier2": 0, "tier3": 0}
    last_api_time = 0.0
    t_start = time.time()

    logger.info(f"====== 开始批量导入（索引 {start_idx} → {import_total - 1}，共 {import_total - start_idx} 条） ======")

    for i in range(start_idx, import_total):
        msg = import_messages[i]
        user_id = str(msg.get("user_id", ""))
        user_name = str(msg.get("user_name", "未知"))
        content = str(msg.get("content", ""))
        timestamp = str(msg.get("timestamp", ""))

        # ---- 去重检查 ----
        if db.message_exists(user_id, content, timestamp):
            stats["skipped_dup"] += 1
            _print_progress(i, import_total, stats, t_start)
            write_progress(progress_path, i + 1)
            continue

        # ---- 三级分层存储 ----
        tier = get_message_tier(timestamp)
        keep = (tier <= 2)

        # ---- 频率控制 ----
        elapsed = time.time() - last_api_time
        if elapsed < API_INTERVAL:
            time.sleep(API_INTERVAL - elapsed)

        # ---- 系统总结消息：直接存储，不调用 LLM ----
        if user_id == "system":
            success = False
            for attempt in range(MAX_RETRIES + 1):
                try:
                    last_api_time = time.time()
                    keywords_list = msg.get("keywords", [])
                    msg_id = db.add_summarized_message(
                        content=content,
                        timestamp=timestamp,
                        keywords_list=keywords_list,
                        user_name=user_name,
                    )
                    if msg_id is not None:
                        stats["stored"] += 1
                        stats[f"tier{tier}"] += 1
                    else:
                        stats["skipped_filtered"] += 1
                    success = True
                    break
                except Exception as e:
                    logger.warning(
                        f"[{i}/{import_total}] 第 {attempt + 1} 次失败 "
                        f"(system: {content[:30]}...): {e}"
                    )
                    if attempt < MAX_RETRIES:
                        time.sleep(1.5 ** attempt)
                    else:
                        logger.error(
                            f"[{i}/{import_total}] 重试耗尽，跳过: "
                            f"system content={content[:50]}... | 错误: {e}"
                        )
                        stats["errors"] += 1
        else:
            # ---- 普通消息：调用分析管线（含重试） ----
            success = False
            for attempt in range(MAX_RETRIES + 1):
                try:
                    last_api_time = time.time()
                    msg_id = db.add_message_analysis(
                        user_id=user_id,
                        user_name=user_name,
                        content=content,
                        keep_content=keep,
                        custom_timestamp=timestamp if timestamp else None,
                    )
                    if msg_id is not None:
                        stats["stored"] += 1
                        stats[f"tier{tier}"] += 1
                    else:
                        stats["skipped_filtered"] += 1
                    success = True
                    break
                except Exception as e:
                    logger.warning(
                        f"[{i}/{import_total}] 第 {attempt + 1} 次失败 "
                        f"({user_name}: {content[:30]}...): {e}"
                    )
                    if attempt < MAX_RETRIES:
                        time.sleep(1.5 ** attempt)
                    else:
                        logger.error(
                            f"[{i}/{import_total}] 重试耗尽，跳过: "
                            f"user={user_name} content={content[:50]}... | 错误: {e}"
                        )
                        stats["errors"] += 1

        # ---- 进度 ----
        if (i + 1) % PROGRESS_PRINT_INTERVAL == 0 or i == start_idx:
            _print_progress(i, import_total, stats, t_start)

        if (i + 1) % COMMIT_INTERVAL == 0:
            _print_summary(i, import_total, stats, t_start)

        write_progress(progress_path, i + 1)

    # ---- 完成 ----
    elapsed_total = time.time() - t_start
    logger.info("=" * 60)
    logger.info(f"  批量导入完成！耗时 {elapsed_total:.1f} 秒")
    logger.info(f"  原始消息数:  {stats['total']}")
    logger.info(f"  处理后消息:  {stats['import_total']}")
    logger.info(f"  时间块数:    {stats['blocks_processed']}")
    logger.info(f"  有效存储:    {stats['stored']}")
    logger.info(f"    Tier1(≤2d):  {stats['tier1']}")
    logger.info(f"    Tier2(3-6d):  {stats['tier2']}")
    logger.info(f"    Tier3(≥7d):  {stats['tier3']}")
    logger.info(f"  跳过(重复):  {stats['skipped_dup']}")
    logger.info(f"  跳过(过滤):  {stats['skipped_filtered']}")
    logger.info(f"  错误跳过:    {stats['errors']}")
    logger.info("=" * 60)

    # 清理进度文件（全部完成）
    if os.path.exists(progress_path):
        os.remove(progress_path)
        logger.info("进度文件已清理。")

    return stats


def _print_progress(idx: int, total: int, stats: Dict[str, int], t_start: float) -> None:
    """打印简要进度行。"""
    pct = (idx + 1) / total * 100
    elapsed = time.time() - t_start
    rate = (idx + 1 - stats["skipped_dup"]) / max(elapsed, 0.1)
    logger.info(
        f"[{idx + 1}/{total}] {pct:.1f}% | "
        f"存{stats['stored']} 重{stats['skipped_dup']} "
        f"滤{stats['skipped_filtered']} 错{stats['errors']} | "
        f"{rate:.1f}条/秒"
    )


def _print_summary(idx: int, total: int, stats: Dict[str, int], t_start: float) -> None:
    """打印阶段性汇总。"""
    elapsed = time.time() - t_start
    remaining = total - (idx + 1)
    rate = (idx + 1 - stats["skipped_dup"]) / max(elapsed, 0.1)
    eta = remaining / max(rate, 0.01)
    logger.info(
        f"--- 阶段汇总 [{idx + 1}/{total}] "
        f"速率={rate:.1f}条/秒 预计剩余={eta:.0f}秒 ---"
    )


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    args = parse_args()

    # 检查 JSON 文件
    if not os.path.exists(args.json_file):
        logger.error(f"JSON 文件不存在: {args.json_file}")
        sys.exit(1)

    logger.info(f"JSON 文件: {args.json_file}")
    logger.info(f"目标数据库: {args.db_path}")
    logger.info(f"断点续跑: {'是' if args.resume else '否'}")

    try:
        stats = run_import(
            json_file=args.json_file,
            db_path=args.db_path,
            resume=args.resume,
        )
    except KeyboardInterrupt:
        logger.warning("用户中断。进度已保存，可使用 --resume 继续。")
        sys.exit(130)
    except Exception as e:
        logger.exception(f"批量导入异常终止: {e}")
        sys.exit(1)

    # 返回码：有错误则非零
    if stats.get("errors", 0) > 0:
        logger.warning(f"导入完成但有 {stats['errors']} 条错误，详见 {ERROR_LOG_PATH}")
        sys.exit(2)
    else:
        logger.info("导入成功，无错误。")
        sys.exit(0)
