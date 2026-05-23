"""
trend_analyzer.py — 群聊趋势分析AI模块
======================================
功能：
  对 memory.db 中的群聊数据进行多维度趋势分析，输出结构化 JSON 报告。
  五个分析维度：
    1. 热词检测（概念漂移）
    2. 新词检测
    3. 句式风格变化
    4. 话题热度迁移
    5. 情绪周期

依赖：Python 3.10+，sqlite3，json，re，math，datetime，collections，logging
      可选 jieba（用于中文分词，未安装则回退到字符级切分）
"""

import sqlite3
import json
import os
import re
import math
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Tuple, Optional
from collections import defaultdict, Counter

# ============================================================
# 日志
# ============================================================
if not logging.getLogger("trend_analyzer").handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
logger = logging.getLogger("trend_analyzer")

# ============================================================
# 可选 jieba 分词
# ============================================================
try:
    import jieba
    _JIEBA_AVAILABLE = True
except ImportError:
    _JIEBA_AVAILABLE = False
    logger.info("jieba 未安装，将使用字符级切分作为回退方案。")

# ============================================================
# 内置最小中文情感词典
# ============================================================
_POSITIVE_WORDS: set = {
    "好", "开心", "哈哈", "喜欢", "牛", "赞", "笑死", "绝了",
    "棒", "厉害", "优秀", "爱", "快乐", "高兴", "舒服", "爽",
    "美", "帅", "酷", "感谢", "谢谢", "太棒了", "不错", "可以",
    "行", "强", "稳", "妙", "牛逼", "给力", "666", "哈哈哈",
    "嘻嘻", "嘿嘿", "哇", "耶", "完美", "精彩", "惊艳", "好评",
    "支持", "顶", "靠谱", "到位", "nice", "good", "great",
    "有意思", "有趣", "好玩", "期待", "激动", "感动", "温暖",
}

_NEGATIVE_WORDS: set = {
    "烦", "累", "无聊", "气死", "讨厌", "垃圾", "无语",
    "难受", "痛苦", "伤心", "难过", "生气", "愤怒", "糟糕",
    "差", "烂", "恶心", "崩溃", "绝望", "失败", "不行",
    "惨", "坏", "坑", "愁", "烦死了", "受不了", "想哭",
    "失望", "后悔", "尴尬", "丢脸", "害怕", "担心", "焦虑",
    "抑郁", "暴躁", "想死", "滚", "傻逼", "卧槽", "草",
    "唉", "哎", "晕", "靠", "擦", "日", "操",
}

# ============================================================
# 工具函数
# ============================================================

def _ensure_db(db_path: str) -> sqlite3.Connection:
    """打开数据库连接并确保 trend_log 表存在。"""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trend_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_time TEXT    NOT NULL DEFAULT '',
            period_start  TEXT    NOT NULL DEFAULT '',
            period_end    TEXT    NOT NULL DEFAULT '',
            analysis_json TEXT    NOT NULL DEFAULT ''
        )
    """)
    # 索引：按分析时间快速检索最近报告
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trend_log_time ON trend_log(analysis_time)"
    )
    conn.commit()
    return conn


def _fmt_ts(dt: datetime) -> str:
    """datetime → 'YYYY-MM-DD HH:MM:SS'"""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _tokenize(text: str) -> List[str]:
    """
    中文分词：优先 jieba，否则字符级 2-gram 切分。
    返回词语列表。
    """
    if _JIEBA_AVAILABLE:
        return [w.strip() for w in jieba.cut(text) if len(w.strip()) >= 1]
    # 回退：按标点/空白切分 + 2-gram 补充
    segments = re.split(r"[，。！？、；：\s]+", text)
    tokens = []
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        if len(seg) <= 2:
            tokens.append(seg)
        else:
            # 2-gram 滑动窗口
            for i in range(len(seg) - 1):
                tokens.append(seg[i:i + 2])
    return tokens


def _classify_sentiment(text: str) -> str:
    """
    基于内置情感词典分类单条消息的情感倾向。
    
    返回: "positive" / "negative" / "neutral"
    """
    tokens = _tokenize(text)
    pos = sum(1 for t in tokens if t in _POSITIVE_WORDS)
    neg = sum(1 for t in tokens if t in _NEGATIVE_WORDS)
    if pos > neg:
        return "positive"
    elif neg > pos:
        return "negative"
    return "neutral"


# ============================================================
# 维度 1：热词检测（概念漂移）
# ============================================================

def detect_hot_words(
    conn: sqlite3.Connection,
    recent_start: str,
    recent_end: str,
    baseline_start: str,
    baseline_end: str,
    growth_factor: float = 1.5,
    min_users: int = 2,
) -> List[Dict[str, Any]]:
    """
    热词检测：对比近期与基线期的关键词频率，找出上升显著的词。
    
    算法：
      1. 近期窗口内统计每个关键词的出现次数与使用人数。
      2. 基线窗口内统计每个关键词的出现次数。
      3. 筛选：recent_freq > baseline_freq × growth_factor 且 ≥ min_users 用户。
      4. 按增长率降序排列。
    
    SQL 优化：利用 idx_keywords_msg_id + idx_messages_timestamp 索引，
             通过 JOIN + WHERE timestamp BETWEEN 避免全表扫描。
    """
    cur = conn.cursor()

    # 近期关键词统计（带用户去重）
    cur.execute(
        "SELECT k.keyword, COUNT(*) AS freq, COUNT(DISTINCT m.user_id) AS users "
        "FROM keywords k "
        "JOIN messages m ON k.msg_id = m.id "
        "WHERE m.timestamp >= ? AND m.timestamp < ? "
        "GROUP BY k.keyword",
        (recent_start, recent_end),
    )
    recent_map: Dict[str, Tuple[int, int]] = {
        row[0]: (row[1], row[2]) for row in cur.fetchall()
    }

    # 基线关键词统计
    cur.execute(
        "SELECT k.keyword, COUNT(*) AS freq "
        "FROM keywords k "
        "JOIN messages m ON k.msg_id = m.id "
        "WHERE m.timestamp >= ? AND m.timestamp < ? "
        "GROUP BY k.keyword",
        (baseline_start, baseline_end),
    )
    baseline_map: Dict[str, int] = {row[0]: row[1] for row in cur.fetchall()}

    # 筛选热词
    hot: List[Dict[str, Any]] = []
    for word, (freq_r, users_r) in recent_map.items():
        freq_b = baseline_map.get(word, 0)
        if freq_b == 0:
            continue  # 基线为0的词归入新词检测
        if users_r < min_users:
            continue
        if freq_r <= freq_b * growth_factor:
            continue
        growth = round(freq_r / max(freq_b, 1), 2)
        hot.append({
            "word": word,
            "freq_recent": freq_r,
            "freq_baseline": freq_b,
            "growth_rate": growth,
        })

    hot.sort(key=lambda x: x["growth_rate"], reverse=True)
    logger.info(f"热词检测: 发现 {len(hot)} 个上升热词。")
    return hot


# ============================================================
# 维度 2：新词检测
# ============================================================

def detect_new_words(
    conn: sqlite3.Connection,
    recent_start: str,
    recent_end: str,
    baseline_start: str,
    baseline_end: str,
    min_occurrences: int = 3,
    min_users: int = 2,
) -> List[Dict[str, Any]]:
    """
    新词检测：近期出现但基线期完全未出现的关键词。
    
    条件：
      - 近期出现 ≥ min_occurrences 次
      - 近期使用人数 ≥ min_users
      - 基线期出现次数 = 0
    
    SQL 优化：先查近期高频词，再用 NOT EXISTS 子查询排除基线期存在的词，
             避免拉取全量基线数据到 Python 侧比对。
    """
    cur = conn.cursor()

    cur.execute(
        "SELECT k.keyword, COUNT(*) AS freq, COUNT(DISTINCT m.user_id) AS users "
        "FROM keywords k "
        "JOIN messages m ON k.msg_id = m.id "
        "WHERE m.timestamp >= ? AND m.timestamp < ? "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM keywords k2 "
        "  JOIN messages m2 ON k2.msg_id = m2.id "
        "  WHERE k2.keyword = k.keyword "
        "  AND m2.timestamp >= ? AND m2.timestamp < ?"
        ") "
        "GROUP BY k.keyword "
        "HAVING freq >= ? AND users >= ? "
        "ORDER BY freq DESC",
        (recent_start, recent_end, baseline_start, baseline_end,
         min_occurrences, min_users),
    )
    rows = cur.fetchall()

    new_words = [
        {"word": row[0], "recent_count": row[1], "user_count": row[2]}
        for row in rows
    ]
    logger.info(f"新词检测: 发现 {len(new_words)} 个新词。")
    return new_words


# ============================================================
# 维度 3：句式风格变化
# ============================================================

def _compute_style_metrics(messages: List[str]) -> Dict[str, float]:
    """
    对一批消息计算句式风格指标。
    
    返回:
        avg_sentence_len: 平均句长（字符数）
        exclamation_ratio: 含感叹号的消息占比
        question_ratio: 含问号的消息占比
    """
    if not messages:
        return {"avg_sentence_len": 0.0, "exclamation_ratio": 0.0, "question_ratio": 0.0}

    total_len = 0
    exclam = 0
    question = 0
    n = len(messages)

    for msg in messages:
        total_len += len(msg)
        if "！" in msg or "!" in msg:
            exclam += 1
        if "？" in msg or "?" in msg:
            question += 1

    return {
        "avg_sentence_len": round(total_len / n, 2),
        "exclamation_ratio": round(exclam / n, 4),
        "question_ratio": round(question / n, 4),
    }


def analyze_style_changes(
    conn: sqlite3.Connection,
    recent_start: str,
    recent_end: str,
    baseline_start: str,
    baseline_end: str,
) -> Dict[str, Dict[str, float]]:
    """
    句式风格变化：比较近期与基线期的平均句长、感叹号占比、问号占比。
    
    SQL 优化：仅拉取 content 字段，WHERE timestamp BETWEEN 利用索引。
    """
    cur = conn.cursor()

    def _fetch_messages(start: str, end: str) -> List[str]:
        cur.execute(
            "SELECT content FROM messages "
            "WHERE timestamp >= ? AND timestamp < ? "
            "AND content_state = 'original' AND content != ''",
            (start, end),
        )
        return [row[0] for row in cur.fetchall()]

    recent_msgs = _fetch_messages(recent_start, recent_end)
    baseline_msgs = _fetch_messages(baseline_start, baseline_end)

    recent_metrics = _compute_style_metrics(recent_msgs)
    baseline_metrics = _compute_style_metrics(baseline_msgs)

    result: Dict[str, Dict[str, float]] = {}
    for key in ["avg_sentence_len", "exclamation_ratio", "question_ratio"]:
        r = recent_metrics[key]
        b = baseline_metrics[key]
        result[key] = {
            "recent": r,
            "baseline": b,
            "delta": round(r - b, 4),
        }

    logger.info(
        f"句式风格: 近期{len(recent_msgs)}条 vs 基线{len(baseline_msgs)}条。"
    )
    return result


# ============================================================
# 维度 4：话题热度迁移
# ============================================================

def analyze_topic_shifts(
    conn: sqlite3.Connection,
    recent_start: str,
    recent_end: str,
    baseline_start: str,
    baseline_end: str,
    recent_days: int = 7,
    baseline_days: int = 21,
) -> List[Dict[str, Any]]:
    """
    话题热度迁移：基于 events.subject 计算日均事件数，分类趋势。
    
    算法：
      1. 近期窗口：统计每个 subject 的事件数 → 日均 = 总数 / recent_days
      2. 基线窗口：统计每个 subject 的事件数 → 日均 = 总数 / baseline_days
      3. 趋势判定：
           - 近期日均 ≥ 基线日均 × 1.3 → "上升"
           - 近期日均 ≤ 基线日均 × 0.7 → "下降"
           - 否则 → "稳定"
    
    SQL 优化：WHERE timestamp BETWEEN + GROUP BY subject，
             排除 msg_id=-1 的合并事件。
    """
    cur = conn.cursor()

    def _fetch_subject_counts(start: str, end: str) -> Dict[str, int]:
        cur.execute(
            "SELECT subject, COUNT(*) AS cnt FROM events "
            "WHERE timestamp >= ? AND timestamp < ? "
            "AND msg_id != -1 AND subject != '' "
            "GROUP BY subject",
            (start, end),
        )
        return {row[0]: row[1] for row in cur.fetchall()}

    recent_counts = _fetch_subject_counts(recent_start, recent_end)
    baseline_counts = _fetch_subject_counts(baseline_start, baseline_end)

    all_subjects = set(recent_counts.keys()) | set(baseline_counts.keys())
    shifts: List[Dict[str, Any]] = []

    for subject in all_subjects:
        rc = recent_counts.get(subject, 0)
        bc = baseline_counts.get(subject, 0)
        daily_r = rc / recent_days
        daily_b = bc / max(baseline_days, 1)

        if daily_r >= daily_b * 1.3:
            trend = "上升"
        elif daily_r <= daily_b * 0.7 and daily_b > 0:
            trend = "下降"
        else:
            trend = "稳定"

        shifts.append({
            "topic": subject,
            "recent_count": rc,
            "baseline_count": bc,
            "trend": trend,
        })

    # 按近期事件数降序
    shifts.sort(key=lambda x: x["recent_count"], reverse=True)
    rising = sum(1 for s in shifts if s["trend"] == "上升")
    falling = sum(1 for s in shifts if s["trend"] == "下降")
    logger.info(f"话题迁移: {len(shifts)} 个话题（↑{rising} ↓{falling} →{len(shifts)-rising-falling}）。")
    return shifts


# ============================================================
# 维度 5：情绪周期
# ============================================================

def analyze_emotion_cycle(
    conn: sqlite3.Connection,
    period_start: str,
    period_end: str,
) -> Dict[str, int]:
    """
    情绪周期：统计每日各时段的正/负/中性消息占比，找出情绪高峰。
    
    算法：
      1. 拉取周期内所有原始消息的 content + timestamp。
      2. 逐条用内置情感词典分类。
      3. 按小时（0-23）聚合正/负消息数，找正负情绪峰值小时。
      4. 按星期几（0=周一, 6=周日）聚合，找正负情绪峰值日。
    
    SQL 优化：仅拉取 content + timestamp，WHERE timestamp BETWEEN。
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT content, timestamp FROM messages "
        "WHERE timestamp >= ? AND timestamp < ? "
        "AND content_state = 'original' AND content != ''",
        (period_start, period_end),
    )
    rows = cur.fetchall()

    if not rows:
        logger.warning("情绪周期: 无消息数据。")
        return {
            "positive_peak_hour": -1,
            "negative_peak_hour": -1,
            "positive_peak_weekday": -1,
            "negative_peak_weekday": -1,
        }

    # 按小时聚合
    hour_pos: Dict[int, int] = defaultdict(int)
    hour_neg: Dict[int, int] = defaultdict(int)
    # 按星期几聚合
    wday_pos: Dict[int, int] = defaultdict(int)
    wday_neg: Dict[int, int] = defaultdict(int)

    for content, ts in rows:
        sentiment = _classify_sentiment(content)
        try:
            dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue

        hour = dt.hour
        # Python weekday(): 0=周一, 6=周日
        wday = dt.weekday()

        if sentiment == "positive":
            hour_pos[hour] += 1
            wday_pos[wday] += 1
        elif sentiment == "negative":
            hour_neg[hour] += 1
            wday_neg[wday] += 1

    def _peak(d: Dict[int, int]) -> int:
        if not d:
            return -1
        return max(d, key=lambda k: d[k])

    result = {
        "positive_peak_hour": _peak(hour_pos),
        "negative_peak_hour": _peak(hour_neg),
        "positive_peak_weekday": _peak(wday_pos),
        "negative_peak_weekday": _peak(wday_neg),
    }
    logger.info(
        f"情绪周期: 正峰={result['positive_peak_hour']}h, "
        f"负峰={result['negative_peak_hour']}h。"
    )
    return result


# ============================================================
# 主函数：run_full_analysis
# ============================================================

def run_full_analysis(
    db_path: str,
    recent_hours: int = 24,
    baseline_days: int = 7,
) -> str:
    """
    执行全维度趋势分析，返回 JSON 字符串。
    
    时间窗口设计（所有窗口互不重叠，避免数据泄漏）：
      - 热词/新词 近期: [now - recent_hours, now]
      - 热词/新词 基线: [now - baseline_days*24h, now - recent_hours]
      - 句式风格 近期: [now - 3d, now]
      - 句式风格 基线: [now - 6d, now - 3d]
      - 话题迁移 近期: [now - 7d, now]
      - 话题迁移 基线: [now - 28d, now - 7d]
      - 情绪周期: [now - 7d, now]
    
    参数:
        db_path:       memory.db 路径
        recent_hours:  近期窗口（小时），默认 24
        baseline_days: 基线窗口（天），默认 7
    
    返回:
        str — JSON 格式的趋势报告。
    """
    now = datetime.now()
    now_str = _fmt_ts(now)

    # ---- 热词 / 新词 时间窗口 ----
    hot_recent_start = _fmt_ts(now - timedelta(hours=recent_hours))
    hot_recent_end = now_str
    hot_baseline_start = _fmt_ts(now - timedelta(days=baseline_days))
    hot_baseline_end = hot_recent_start  # 基线不含近期

    # ---- 句式风格 时间窗口（近3天 vs 前3天） ----
    style_recent_start = _fmt_ts(now - timedelta(days=3))
    style_recent_end = now_str
    style_baseline_start = _fmt_ts(now - timedelta(days=6))
    style_baseline_end = style_recent_start

    # ---- 话题迁移 时间窗口（近1周 vs 前3周） ----
    topic_recent_start = _fmt_ts(now - timedelta(days=7))
    topic_recent_end = now_str
    topic_baseline_start = _fmt_ts(now - timedelta(days=28))
    topic_baseline_end = topic_recent_start

    # ---- 情绪周期 时间窗口（近7天） ----
    emotion_start = _fmt_ts(now - timedelta(days=7))
    emotion_end = now_str

    # ---- 整体分析周期 ----
    period_start = _fmt_ts(now - timedelta(days=max(baseline_days, 28)))
    period_end = now_str

    logger.info(
        f"====== 开始全维度趋势分析 ======\n"
        f"  热词近期: [{hot_recent_start}, {hot_recent_end})\n"
        f"  热词基线: [{hot_baseline_start}, {hot_baseline_end})\n"
        f"  风格近期: [{style_recent_start}, {style_recent_end})\n"
        f"  风格基线: [{style_baseline_start}, {style_baseline_end})\n"
        f"  话题近期: [{topic_recent_start}, {topic_recent_end})\n"
        f"  话题基线: [{topic_baseline_start}, {topic_baseline_end})\n"
        f"  情绪周期: [{emotion_start}, {emotion_end})"
    )

    conn = _ensure_db(db_path)

    try:
        # 维度 1
        hot_words = detect_hot_words(
            conn,
            hot_recent_start, hot_recent_end,
            hot_baseline_start, hot_baseline_end,
        )

        # 维度 2
        new_words = detect_new_words(
            conn,
            hot_recent_start, hot_recent_end,
            hot_baseline_start, hot_baseline_end,
        )

        # 维度 3
        style_changes = analyze_style_changes(
            conn,
            style_recent_start, style_recent_end,
            style_baseline_start, style_baseline_end,
        )

        # 维度 4
        topic_shifts = analyze_topic_shifts(
            conn,
            topic_recent_start, topic_recent_end,
            topic_baseline_start, topic_baseline_end,
        )

        # 维度 5
        emotion_cycle = analyze_emotion_cycle(
            conn,
            emotion_start, emotion_end,
        )

        # ---- 组装结果 ----
        result: Dict[str, Any] = {
            "period": f"{period_start}_{period_end}",
            "hot_words": hot_words,
            "new_words": new_words,
            "style_changes": style_changes,
            "topic_shifts": topic_shifts,
            "emotion_cycle": emotion_cycle,
        }

        result_json = json.dumps(result, ensure_ascii=False, indent=2)

        # ---- 存入 trend_log 表 ----
        conn.execute(
            "INSERT INTO trend_log (analysis_time, period_start, period_end, analysis_json) "
            "VALUES (?, ?, ?, ?)",
            (now_str, period_start, period_end, result_json),
        )
        conn.commit()

        logger.info(
            f"趋势分析完成。热词={len(hot_words)} 新词={len(new_words)} "
            f"话题={len(topic_shifts)}。已存入 trend_log。"
        )
        return result_json

    except Exception as e:
        logger.exception(f"趋势分析异常: {e}")
        error_result = json.dumps({
            "period": f"{period_start}_{period_end}",
            "hot_words": [],
            "new_words": [],
            "style_changes": {},
            "topic_shifts": [],
            "emotion_cycle": {},
            "error": str(e),
        }, ensure_ascii=False)
        return error_result

    finally:
        conn.close()


# ============================================================
# 命令行测试入口
# ============================================================

if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("  群聊趋势分析AI · 独立测试")
    print("=" * 60)

    # 默认使用同目录下的 memory.db
    test_db = os.path.join(os.path.dirname(__file__), "memory.db")
    if len(sys.argv) > 1:
        test_db = sys.argv[1]

    print(f"\n数据库路径: {test_db}")
    if not os.path.exists(test_db):
        print("⚠ 数据库文件不存在，将创建空库并输出空报告。")

    print("\n>>> run_full_analysis() 执行中...\n")
    report = run_full_analysis(test_db, recent_hours=24, baseline_days=7)

    print("\n--- 趋势报告 (JSON) ---")
    # 美化打印
    try:
        parsed = json.loads(report)
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
    except json.JSONDecodeError:
        print(report)

    # 额外：读取最近一次 trend_log
    print("\n--- 最近 trend_log 记录 ---")
    conn = _ensure_db(test_db)
    cur = conn.cursor()
    cur.execute(
        "SELECT analysis_time, period_start, period_end FROM trend_log "
        "ORDER BY id DESC LIMIT 3"
    )
    for row in cur.fetchall():
        print(f"  [{row[0]}] {row[1]} ~ {row[2]}")
    conn.close()

    print("\n" + "=" * 60)
    print("  测试完毕")
    print("=" * 60)
