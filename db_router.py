"""
db_router.py — 记忆库路径路由（零依赖，可被任何模块安全导入）
"""
import os


def get_db_path(chat_key: str) -> str:
    """
    根据聊天标识生成独立的记忆库路径。

    chat_key 格式:
        "group_123456789"   → memory_group_123456789.db
        "private_987654321" → memory_private_987654321.db
        "" / "default"      → memory.db
    
    返回: 数据库文件的绝对路径
    """
    base = os.path.dirname(os.path.abspath(__file__))
    if not chat_key or chat_key == "default":
        return os.path.join(base, "memory.db")
    safe = "".join(c for c in chat_key if c.isalnum() or c == '_')
    if not safe:
        return os.path.join(base, "memory.db")
    return os.path.join(base, f"memory_{safe}.db")