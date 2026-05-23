#!/usr/bin/env python3
"""
清理记忆库中的垃圾数据
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import re

def main():
    print("=" * 60)
    print("记忆库清理工具")
    print("=" * 60)

    import sqlite3
    conn = sqlite3.connect("memory.db")
    cur = conn.cursor()

    # 1. 找到垃圾消息
    cur.execute("SELECT id, content FROM messages WHERE content LIKE 'Thinking Process%' OR content LIKE '%**Analyze%'")
    trash_msgs = cur.fetchall()
    print(f"找到 {len(trash_msgs)} 条垃圾消息")

    trash_ids = [mid for mid, _ in trash_msgs]

    # 2. 删除垃圾消息关联的关键词
    if trash_ids:
        placeholders = ",".join("?" for _ in trash_ids)
        cur.execute(f"DELETE FROM keywords WHERE msg_id IN ({placeholders})", trash_ids)
        cur.execute(f"DELETE FROM events WHERE msg_id IN ({placeholders})", trash_ids)
        cur.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", trash_ids)
        print(f"删除了 {len(trash_ids)} 条垃圾消息")
        conn.commit()

    # 3. 清理带引号的关键词
    cur.execute("SELECT id, keyword FROM keywords WHERE keyword LIKE '\"%\"' OR keyword LIKE '%*' OR keyword LIKE '%**%' OR keyword LIKE '%Task%' OR keyword LIKE '%Thinking%'")
    trash_kws = cur.fetchall()
    print(f"找到 {len(trash_kws)} 条垃圾关键词")

    trash_kws_ids = [kid for kid, _ in trash_kws]
    if trash_kws_ids:
        placeholders = ",".join("?" for _ in trash_kws_ids)
        cur.execute(f"DELETE FROM keywords WHERE id IN ({placeholders})", trash_kws_ids)
        print(f"删除了 {len(trash_kws_ids)} 条垃圾关键词")
        conn.commit()

    # 4. 清理纯英文关键词（看起来像思考过程的）
    cur.execute("SELECT id, keyword FROM keywords")
    all_kw = cur.fetchall()
    bad_kw_ids = []
    common_short_en = {'is','are','in','on','at','by','the','a','an','go','do','no','yes','hi','ok'}
    for kid, kw in all_kw:
        if kw in common_short_en:
            continue
        if re.search(r'^[a-zA-Z]+$', kw) or any(w in kw for w in ['Is','Task','Role','Condition','Request','Thinking','Analyze','Final','Polish','Option']):
            bad_kw_ids.append(kid)
    if bad_kw_ids:
        placeholders = ",".join("?" for _ in bad_kw_ids)
        cur.execute(f"DELETE FROM keywords WHERE id IN ({placeholders})", bad_kw_ids)
        print(f"删除了 {len(bad_kw_ids)} 条英文垃圾关键词")
        conn.commit()

    print("\n清理后状态:")
    cur.execute("SELECT COUNT(*) FROM messages")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM keywords")
    total_kw = cur.fetchone()[0]
    print(f"消息数: {total}, 关键词数: {total_kw}")

    conn.close()

    print("\n" + "=" * 60)
    print("清理完成")
    print("=" * 60)

if __name__ == '__main__':
    main()
