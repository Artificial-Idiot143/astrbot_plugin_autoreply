#!/usr/bin/env python3
"""
audit_test.py — 全系统审计自测脚本（纯逻辑，不依赖LLM）
"""

import sys
import os
import re
import sqlite3
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from memory_ai import (
    clean_message,
    parse_time_keywords,
    _strip_thinking_process,
    extract_binary_decision,
)

from auto_reply import (
    step1_should_reply,
    step2_need_memory,
    _super_clean,
    _simple_summary_from_memory,
    _smart_default_reply,
    BOT_NAME,
)

PASS = 0
FAIL = 0

def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  <- {extra}")

def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

# ================================================================
# 1. clean_message()
# ================================================================
section("1. clean_message() — 消息清洗")

check("正常中文消息保留", clean_message("你好世界") == "你好世界")
check("带英文的中文保留", clean_message("Hello你好") == "Hello你好")
check("纯表情过滤", clean_message("[表情]") is False)
check("纯无意义词过滤", clean_message("收到") is False)
check("+1过滤", clean_message("+1") is False)
check("空白过滤", clean_message("   ") is False)
check("空字符串过滤", clean_message("") is False)
check("哈哈过滤", clean_message("哈哈") is False)
check("Thinking Process过滤", clean_message("Thinking: hello world") is False)
check("**Analyze过滤", clean_message("**Analyze: something") is False)
check("Role: Task: 过滤", clean_message("Role: assistant Task: reply") is False)
check("短词'入机'保留", clean_message("入机") == "入机")
check("短词'嗯嗯'保留（由step1过滤）", clean_message("嗯嗯") == "嗯嗯")
check("短词'哦'过滤", clean_message("哦") is False)

# ================================================================
# 2. _super_clean()
# ================================================================
section("2. _super_clean() — 终极清理")

check("纯中文保留", _super_clean("你好世界") == "你好世界")
check("英文删除", _super_clean("Hello你好World") == "你好")
check("数字保留", _super_clean("下午3点开会") == "下午3点开会")
check("括号英文删除", _super_clean("多人通知 (Xiao Rui Rui complains)") == "多人通知")
check("标点粘连修复（保留后者）", _super_clean("你好。，世界") == "你好，世界")
check("连续标点去重", _super_clean("你好。。世界") == "你好。世界")
check("首尾标点清理", _super_clean("。你好。") == "你好")
check("空字符串", _super_clean("") == "")
check("纯英文返回空", _super_clean("Hello World") == "")
check("单字中文返回空", _super_clean("哈") == "")
check("思维链输出清理", _super_clean("Thinking Process: 分析中... 最终答案：你好世界") == "分析中最终答案：你好世界")
check("Draft Refinement清理（数字保留）", _super_clean("Draft Refinement: 1. 你好世界") == "1你好世界")

# ================================================================
# 3. step1_should_reply()
# ================================================================
section("3. step1_should_reply() — 回复判断")

check("@机器人回复", step1_should_reply(f"@{BOT_NAME} 你好", "测试用户")[0] is True)
check("@其他人不回复", step1_should_reply("@张三 你好", "测试用户")[0] is False)
check("空消息不回复", step1_should_reply("", "测试用户")[0] is False)
check("None消息不回复", step1_should_reply(None, "测试用户")[0] is False)
check("纯标点不回复", step1_should_reply("。。。", "测试用户")[0] is False)
check("无意义词不回复", step1_should_reply("嗯嗯", "测试用户")[0] is False)
check("正常消息回复", step1_should_reply("今天天气不错", "测试用户")[0] is True)
check("'入机'回复", step1_should_reply("入机", "测试用户")[0] is True)
check("'好的'不回复", step1_should_reply("好的", "测试用户")[0] is False)

# no_reply_words 无重复
import auto_reply as ar_mod
src = open(ar_mod.__file__, "r", encoding="utf-8").read()
match = re.search(r'no_reply_words\s*=\s*\[(.*?)\]', src)
if match:
    words_str = match.group(1)
    words = [w.strip().strip('"').strip("'") for w in words_str.split(",")]
    dupes = [w for w in set(words) if words.count(w) > 1]
    check("no_reply_words 无重复项", len(dupes) == 0, f"重复项: {dupes}")

# ================================================================
# 4. step2_need_memory()
# ================================================================
section("4. step2_need_memory() — 记忆需求判断")

check("'之前'触发记忆", step2_need_memory("之前聊过什么") is True)
check("'昨天'触发记忆", step2_need_memory("昨天发生了什么") is True)
check("'你好'不需要记忆", step2_need_memory("你好") is False)
check("'嗨'不需要记忆", step2_need_memory("嗨") is False)
check("'早上好'不需要记忆", step2_need_memory("早上好") is False)
check("'在吗'不需要记忆", step2_need_memory("在吗") is False)
check("短词'入机'不需要记忆", step2_need_memory("入机") is False)
check("'今天天气真好'不需要记忆", step2_need_memory("今天天气真好") is False)
check("疑问句需要记忆", step2_need_memory("为什么这样") is True)

# ================================================================
# 5. _simple_summary_from_memory()
# ================================================================
section("5. _simple_summary_from_memory() — 记忆简单总结")

check("空列表返回空", _simple_summary_from_memory([]) == "")
check("单条记忆返回内容", _simple_summary_from_memory([{"content": "你好"}]) == "你好")
check("多条记忆拼接", "还有" in _simple_summary_from_memory([
    {"content": "你好"}, {"content": "世界"}
]))
check("英文记忆被清理", _simple_summary_from_memory([
    {"content": "Hello World 你好"}
]) == "你好")
check("system: 前缀处理", _simple_summary_from_memory([
    {"content": "system: 你好"}
]) == "你好")

# ================================================================
# 6. _smart_default_reply()
# ================================================================
section("6. _smart_default_reply() — 智能默认回复")

check("打招呼回复非空", len(_smart_default_reply("你好")) > 0)
check("询问无记忆回复非空", len(_smart_default_reply("为什么", no_memory=True)) > 0)
check("普通消息回复非空", len(_smart_default_reply("今天天气不错")) > 0)

# ================================================================
# 7. parse_time_keywords()
# ================================================================
section("7. parse_time_keywords() — 时间关键词解析")

check("'今天'解析", parse_time_keywords("今天") is not None)
check("'昨天'解析", parse_time_keywords("昨天") is not None)
check("'前天'解析", parse_time_keywords("前天") is not None)
check("'上周'解析", parse_time_keywords("上周") is not None)
check("'本周'解析", parse_time_keywords("本周") is not None)
check("'这个月'解析", parse_time_keywords("这个月") is not None)
check("'上个月'解析", parse_time_keywords("上个月") is not None)
check("'三月'解析", parse_time_keywords("三月") is not None)
check("无时间词返回None", parse_time_keywords("你好") is None)

result_ta = parse_time_keywords("今天上午")
check("'今天上午'解析正确", result_ta is not None)
check("'今天上午'结束时间正确", result_ta is not None and "12:00:00" in result_ta[1])

result_ya = parse_time_keywords("昨天下午")
check("'昨天下午'开始时间正确", result_ya is not None and "12:00:00" in result_ya[0])

# ================================================================
# 8. _strip_thinking_process()
# ================================================================
section("8. _strip_thinking_process() — 思维链剥离")

check("Final Answer提取", _strip_thinking_process("thinking... Final Answer: 你好") == "你好")
check("最终答案提取", _strip_thinking_process("分析中 最终答案：你好") == "你好")
check("纯答案直接返回", _strip_thinking_process("你好") == "你好")
check("空字符串", _strip_thinking_process("") == "")

# ================================================================
# 9. extract_binary_decision()
# ================================================================
section("9. extract_binary_decision() — 二值判定提取")

check("整行0提取", extract_binary_decision("0") == "0")
check("整行1提取", extract_binary_decision("1") == "1")
check("中文标记提取", extract_binary_decision("不需要") == "1")
check("默认返回1", extract_binary_decision("乱七八糟") == "1")
check("思维链中提取", extract_binary_decision("thinking... 0") == "0")

# ================================================================
# 10. 数据库表结构检查
# ================================================================
section("10. 数据库表结构检查")

db_path = os.path.join(os.path.dirname(__file__), "memory.db")
try:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]
    for t in ["messages", "keywords", "events", "retrieval_log", "patterns"]:
        check(f"表 '{t}' 存在", t in tables, f"缺少表: {t}")

    cur.execute("SELECT name FROM sqlite_master WHERE type='index'")
    indexes = [r[0] for r in cur.fetchall()]
    for idx in ["idx_messages_timestamp", "idx_keywords_keyword",
                "idx_keywords_msg_id", "idx_events_msg_id"]:
        check(f"索引 '{idx}' 存在", idx in indexes, f"缺少索引: {idx}")

    cur.execute("PRAGMA table_info(messages)")
    cols = {r[1]: r[2] for r in cur.fetchall()}
    for c in ["content_state", "importance_score", "compressed_content", "last_maintained"]:
        check(f"messages 有 {c} 列", c in cols)

    conn.close()
except Exception as e:
    check("数据库连接", False, str(e))

# ================================================================
# 11. 维护流程一致性检查
# ================================================================
section("11. 维护流程一致性检查")

import daily_maintenance as dm
check("daily_maintenance 有 merge_window_messages", hasattr(dm, "merge_window_messages"))
check("daily_maintenance 有 tier0_to_tier1", hasattr(dm, "tier0_to_tier1"))
check("daily_maintenance 有 tier1_to_tier2", hasattr(dm, "tier1_to_tier2"))
check("daily_maintenance 有 tier2_to_tier3", hasattr(dm, "tier2_to_tier3"))
check("daily_maintenance 有 do_maintenance", hasattr(dm, "do_maintenance"))

# ================================================================
# 12. 死代码检查
# ================================================================
section("12. 死代码检查")

ar_src = open(os.path.join(os.path.dirname(__file__), "auto_reply.py"),
              "r", encoding="utf-8").read()
check("_force_chinese 已移除", "_force_chinese" not in ar_src)
check("_translate_to_chinese 已移除", "_translate_to_chinese" not in ar_src)

# ================================================================
# 13. 边界条件测试
# ================================================================
section("13. 边界条件测试")

check("_super_clean 处理超长字符串", len(_super_clean("你好" * 500)) > 0)
check("_super_clean 处理纯标点", _super_clean("。，！？") == "")
check("_super_clean 处理混合中英文标点",
      _super_clean("Hello。你好，World！") == "你好")
check("_simple_summary 处理含空content的refs",
      _simple_summary_from_memory([
          {"content": "", "timestamp": "2025-01-01 12:00:00"},
          {"content": "你好", "timestamp": "2025-01-01 13:00:00"},
      ]) == "你好")
check("step1 处理None", step1_should_reply(None, "测试")[0] is False)
check("step1 处理超长消息", step1_should_reply("你好" * 500, "测试")[0] is True)

# ================================================================
# 14. 回复逻辑端到端模拟
# ================================================================
section("14. 回复逻辑端到端模拟")

test_cases = [
    ("入机", "短词/游戏术语"),
    ("你好", "打招呼"),
    ("今天天气真好", "闲聊"),
    ("聊了些什么", "概括性问题"),
    ("昨天发生了什么", "时间+概括"),
    ("@张三 在吗", "艾特别人"),
    (f"@{BOT_NAME} 帮我查一下", "@机器人"),
    ("之前说的那个项目怎么样了", "记忆关键词"),
    ("嗯嗯", "无意义词"),
    ("", "空消息"),
]

for msg, desc in test_cases:
    should_reply, reason = step1_should_reply(msg, "测试用户")
    need_mem = step2_need_memory(msg) if should_reply else None

    print(f"  [{desc}] \"{msg[:30]}\"")
    print(f"    Step1: {reason} → {'回复' if should_reply else '不回复'}")
    if should_reply:
        print(f"    Step2: {'需要记忆' if need_mem else '不需要记忆'}")

# ================================================================
# 总结
# ================================================================
section("测试总结")

total = PASS + FAIL
print(f"\n  通过: {PASS}/{total}")
print(f"  失败: {FAIL}/{total}")
if total > 0:
    print(f"  通过率: {PASS/total*100:.1f}%")

if FAIL > 0:
    print("\n  [WARN] 存在失败项，请检查上述 [FAIL] 标记。")
else:
    print("\n  [OK] 所有测试通过！")

print()
