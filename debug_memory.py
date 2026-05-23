#!/usr/bin/env python3
"""
调试记忆库：检查数据库内容和搜索功能
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from memory_ai import MemoryDB, parse_time_keywords

def main():
    print("=" * 60)
    print("记忆库调试工具")
    print("=" * 60)
    
    # 1. 连接数据库
    db = MemoryDB()
    print("数据库连接成功\n")
    
    # 2. 查看数据库中有多少条消息
    with db._lock:
        import sqlite3
        conn = sqlite3.connect(db.db_path)
        cur = conn.cursor()
        
        cur.execute("SELECT COUNT(*) FROM messages")
        total_msgs = cur.fetchone()[0]
        print(f"总消息数: {total_msgs} 条")
        
        cur.execute("SELECT COUNT(*) FROM keywords")
        total_kw = cur.fetchone()[0]
        print(f"总关键词数: {total_kw} 条\n")
        
        # 3. 查看最近的 10 条消息
        print("最近 10 条消息:")
        print("-" * 60)
        cur.execute("SELECT id, content, user_name, timestamp FROM messages ORDER BY timestamp DESC LIMIT 10")
        recent_msgs = cur.fetchall()
        
        for i, (mid, content, user, ts) in enumerate(recent_msgs, 1):
            preview = content[:50] if len(content) > 50 else content
            print(f"  {i}. [{ts}] {user}: {preview}")
        
        # 4. 查看关键词表
        print("\n关键词样本:")
        print("-" * 60)
        cur.execute("SELECT DISTINCT keyword FROM keywords LIMIT 15")
        kw_samples = [r[0] for r in cur.fetchall()]
        print(", ".join(kw_samples))
        
        conn.close()
    
    # 5. 测试时间范围识别
    print("\n时间范围识别测试:")
    test_cases = [
        "昨天聊了些什么",
        "昨天上午在聊什么",
        "上周有什么事",
        "上个月的消息",
        "三月有什么",
    ]
    
    for test in test_cases:
        time_range = parse_time_keywords(test)
        if time_range:
            print(f"  '{test}' -> {time_range}")
        else:
            print(f"  '{test}' -> 未识别")
    
    # 6. 测试搜索
    print("\n搜索功能测试:")
    print("-" * 60)
    
    # 测试关键词搜索
    if kw_samples:
        test_kw = kw_samples[0]
        result = db.search_by_keyword([test_kw], limit=3)
        print(f"关键词搜索 '{test_kw}' -> {len(result)} 条结果")
    
    # 测试时间搜索
    from datetime import datetime, timedelta
    now = datetime.now()
    yesterday = now - timedelta(days=1)
    start = yesterday.replace(hour=0, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S")
    end = yesterday.replace(hour=23, minute=59, second=59).strftime("%Y-%m-%d %H:%M:%S")
    
    result = db.search_by_time_range(start, end, limit=5)
    print(f"时间范围搜索 {start} ~ {end} -> {len(result)} 条结果")
    
    print("\n" + "=" * 60)
    print("调试完成")
    print("=" * 60)

if __name__ == "__main__":
    main()
