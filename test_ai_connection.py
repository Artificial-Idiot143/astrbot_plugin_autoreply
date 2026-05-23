#!/usr/bin/env python3
"""
测试AI连接和JSON输出
"""
import sys
import os
import json
import re
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import LLM_API_URL, LLM_MODEL_NAME, LLM_API_KEY

print("=" * 60)
print("测试AI连接")
print("=" * 60)
print(f"API地址: {LLM_API_URL}")
print(f"模型名称: {LLM_MODEL_NAME}")
print()

# 测试1: 简单的ping测试
print("测试1: 发送简单请求...")
try:
    payload = {
        "model": LLM_MODEL_NAME,
        "messages": [
            {"role": "system", "content": "你好"},
            {"role": "user", "content": "你好，请回复'测试成功'"}
        ],
        "temperature": 0.7,
        "max_tokens": 8192,
    }
    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"
    
    resp = requests.post(LLM_API_URL, json=payload, headers=headers, timeout=15)
    print(f"状态码: {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        print(f"响应: {json.dumps(data, ensure_ascii=False, indent=2)}")
        content = data["choices"][0]["message"]["content"].strip()
        print(f"AI回复: {content}")
        print("✅ 连接正常！")
    else:
        print(f"❌ 请求失败: {resp.text}")
except Exception as e:
    print(f"❌ 连接异常: {e}")
print()

# 测试2: 测试JSON输出
print("=" * 60)
print("测试2: 测试JSON输出")
print("=" * 60)

test_messages = [
    {"user_name": "张三", "content": "今天天气真好"},
    {"user_name": "李四", "content": "是啊，适合出去走走"},
    {"user_name": "王五", "content": "那我们下午去公园吧"},
    {"user_name": "张三", "content": "好的，几点？"},
    {"user_name": "李四", "content": "三点怎么样？"}
]

text = "\n".join([f"[{m['user_name']}] {m['content']}" for m in test_messages if m['content'].strip()])

system = "你是一个消息合并器。输出严格JSON，不要任何解释。"
user = (
    "合并以下多条群聊消息，输出包含两个字段的JSON：\n"
    "  1. summary: 一句话概括（不超过50字）\n"
    "  2. keywords: 1-3个关键词数组\n"
    "只输出JSON，不要任何其他内容。\n"
    f"消息：\n{text}"
)

print(f"发送消息:\n{text}")
print()
print("等待AI回复...")

try:
    payload = {
        "model": LLM_MODEL_NAME,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user}
        ],
        "temperature": 0.7,
        "max_tokens": 8192,
    }
    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"
    
    resp = requests.post(LLM_API_URL, json=payload, headers=headers, timeout=30)
    print(f"状态码: {resp.status_code}")
    
    if resp.status_code == 200:
        data = resp.json()
        output = data["choices"][0]["message"]["content"].strip()
        print(f"AI原始输出:")
        print(repr(output))
        print()
        
        # 尝试解析JSON
        print("尝试解析JSON...")
        try:
            # 尝试提取JSON
            m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", output, re.DOTALL)
            if m:
                output = m.group(1).strip()
                print(f"提取代码块后: {repr(output)}")
            
            start = output.find("{")
            end = output.rfind("}")
            if start != -1 and end != -1:
                output = output[start:end + 1]
                print(f"提取{{}}部分后: {repr(output)}")
            
            result = json.loads(output)
            print(f"✅ 解析成功: {json.dumps(result, ensure_ascii=False, indent=2)}")
        except Exception as e:
            print(f"❌ 解析失败: {e}")
    else:
        print(f"❌ 请求失败: {resp.text}")
        
except Exception as e:
    print(f"❌ 异常: {e}")
    import traceback
    traceback.print_exc()
