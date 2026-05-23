#!/usr/bin/env python3
"""
test_reply.py — auto_reply 交互式测试
=====================================
在终端输入文本，机器人模拟回答。
显示每一步的判断过程。
"""

import sys
import os
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

from auto_reply import (
    handle_message,
    step1_should_reply,
    step2_need_memory,
    BOT_NAME,
)


def main():
    print("=" * 60)
    print(f"  {BOT_NAME} 交互式测试")
    print("=" * 60)
    print("  · 输入文本与机器人聊天")
    print("  · 输入 'quit' 或 'exit' 退出")
    print("=" * 60)

    test_user_id = "test_user_123"
    test_user_name = "测试用户"

    while True:
        try:
            user_input = input("\n> 你说: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\n" + "=" * 60)
            print("  再见！")
            print("=" * 60)
            break

        if not user_input:
            continue

        if user_input.lower() in ("quit", "exit", "退出"):
            print("\n" + "=" * 60)
            print("  再见！")
            print("=" * 60)
            break

        print(f"  [Step1] 判断是否回复...")
        should_reply, reason = step1_should_reply(user_input, test_user_name)
        print(f"  [Step1] 结果: {reason} → {'回复' if should_reply else '不回复'}")

        if not should_reply:
            print(f"> {BOT_NAME}: （不回复）")
            continue

        print(f"  [Step2] 判断是否需要记忆库...")
        need_memory = step2_need_memory(user_input)
        print(f"  [Step2] 结果: {'需要记忆库' if need_memory else '不需要记忆库'}")

        print(f"  [Step3] 生成回复...")
        reply = handle_message(
            user_id=test_user_id,
            user_name=test_user_name,
            content=user_input,
            store_in_memory=False,
        )

        if reply:
            print(f"> {BOT_NAME}: {reply}")
        else:
            print(f"> {BOT_NAME}: （无回复）")


if __name__ == "__main__":
    main()
