"""
convert_chat_records.py — QQChatExporter JSON → batch_import 格式转换
=====================================================================
读取 chat_records.json（QQChatExporter V5 导出格式），
转换为 batch_import.py 所需的扁平数组格式，输出到 chat_records_flat.json。

过滤规则：
  - 跳过系统消息（system=true）
  - 跳过撤回消息（recalled=true）
  - 跳过纯图片/视频/文件/音频消息（无有效文本）
  - 跳过合并转发消息（type_11，内容为XML无意义）
  - 回复消息（type_3）：提取实际回复文本，去除 "[回复 xxx: xxx]" 前缀
  - 普通文本（type_1）：直接提取 content.text

用法：
  python convert_chat_records.py
  python convert_chat_records.py --input chat_records.json --output chat_records_flat.json
"""

import json
import re
import sys
import os
import argparse
from datetime import datetime


def parse_args():
    parser = argparse.ArgumentParser(description="QQChatExporter JSON → batch_import 格式转换")
    parser.add_argument("--input", default="chat_records.json", help="输入 JSON 文件路径")
    parser.add_argument("--output", default="chat_records_flat.json", help="输出 JSON 文件路径")
    return parser.parse_args()


def extract_text(msg: dict) -> str:
    """
    从 QQChatExporter 消息对象中提取有效文本。
    
    返回:
        str  — 提取的文本；空字符串表示无有效文本（应跳过）。
    """
    msg_type = msg.get("type", "")
    content = msg.get("content", {})
    raw_text = content.get("text", "").strip()

    if not raw_text:
        return ""

    # 纯图片消息：[图片: xxx.jpg]
    if re.match(r"^\[图片:", raw_text):
        return ""

    # 纯视频消息
    if re.match(r"^\[视频:", raw_text):
        return ""

    # 纯文件消息
    if re.match(r"^\[文件:", raw_text):
        return ""

    # 纯音频消息
    if re.match(r"^\[语音:", raw_text) or re.match(r"^\[音频:", raw_text):
        return ""

    # 合并转发消息（type_11）：内容为 XML，无意义
    if msg_type == "type_11" and raw_text.startswith("[合并转发:"):
        return ""

    # 回复消息（type_3）：去除 "[回复 xxx: xxx]\n" 前缀
    if msg_type == "type_3":
        # 匹配 "[回复 uid: 原消息内容]\n实际回复"
        cleaned = re.sub(r"^\[回复\s+[^\]]+\][\r\n]*", "", raw_text)
        cleaned = cleaned.strip()
        if not cleaned:
            return ""
        return cleaned

    # 普通文本消息
    return raw_text


def convert(input_path: str, output_path: str):
    print(f"读取: {input_path}")

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    messages = data.get("messages", [])
    if not messages:
        print("错误: JSON 中没有 messages 数组。")
        return

    total = len(messages)
    print(f"原始消息总数: {total}")

    result = []
    stats = {
        "total": total,
        "converted": 0,
        "skipped_system": 0,
        "skipped_recalled": 0,
        "skipped_no_text": 0,
        "skipped_empty": 0,
    }

    for msg in messages:
        # 跳过系统消息
        if msg.get("system", False):
            stats["skipped_system"] += 1
            continue

        # 跳过撤回消息
        if msg.get("recalled", False):
            stats["skipped_recalled"] += 1
            continue

        # 提取文本
        text = extract_text(msg)
        if not text:
            stats["skipped_no_text"] += 1
            continue

        # 提取发送者信息
        sender = msg.get("sender", {})
        user_id = sender.get("uid", sender.get("uin", ""))
        user_name = sender.get("name", "未知")

        # 时间戳：优先用 time 字段，否则从 timestamp(ms) 转换
        timestamp = msg.get("time", "")
        if not timestamp:
            ts_ms = msg.get("timestamp", 0)
            if ts_ms:
                timestamp = datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")

        result.append({
            "user_id": str(user_id),
            "user_name": str(user_name),
            "content": text,
            "timestamp": timestamp,
        })
        stats["converted"] += 1

    # 写入输出
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # 统计报告
    print()
    print("=" * 50)
    print("  转换完成！")
    print("=" * 50)
    print(f"  原始消息:     {stats['total']}")
    print(f"  成功转换:     {stats['converted']}")
    print(f"  跳过(系统):   {stats['skipped_system']}")
    print(f"  跳过(撤回):   {stats['skipped_recalled']}")
    print(f"  跳过(无文本): {stats['skipped_no_text']}")
    print(f"  输出文件:     {output_path}")
    print("=" * 50)

    # 预览前5条
    if result:
        print()
        print("预览前5条转换结果:")
        for i, item in enumerate(result[:5]):
            print(f"  [{i+1}] {item['user_name']}({item['user_id'][:20]}...) "
                  f"@ {item['timestamp']}: {item['content'][:50]}")


if __name__ == "__main__":
    args = parse_args()
    if not os.path.exists(args.input):
        print(f"错误: 输入文件不存在: {args.input}")
        sys.exit(1)
    convert(args.input, args.output)
