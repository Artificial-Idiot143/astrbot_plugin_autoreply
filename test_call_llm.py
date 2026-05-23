#!/usr/bin/env python3
"""
直接测试 _call_llm_api
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 导入并调用
from memory_ai import _call_llm_api

print("=" * 60)
print("测试 _call_llm_api")
print("=" * 60)
print()

system_prompt = "你是一个消息合并器。直接输出结果，不要任何思考过程。"
user_prompt = (
    "合并以下多条群聊消息，直接输出包含两个字段的JSON：\n"
    "  1. summary: 一句话概括（不超过50字）\n"
    "  2. keywords: 1-3个关键词数组\n"
    "不要思考过程，直接输出JSON。\n"
    "消息：\n"
    "[张三] 今天天气真好\n"
    "[李四] 是啊，适合出去走走\n"
    "[王五] 那我们下午去公园吧\n"
    "[张三] 好的，几点？\n"
    "[李四] 三点怎么样？"
)

print(f"System prompt: {system_prompt}")
print(f"User prompt: {user_prompt[:100]}...")
print()
print("Calling _call_llm_api...")
print()

try:
    output = _call_llm_api(system_prompt, user_prompt)
    print("Output received!")
    print("=" * 60)
    print(repr(output))
    print("=" * 60)
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
