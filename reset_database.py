
"""
重置 memory.db 数据库
操作：
  1. 备份旧数据库（可选）
  2. 删除旧数据库
  3. 重新初始化新的空数据库
"""
import os
import sys
import time
import shutil
from datetime import datetime


def main():
    db_path = "memory.db"
    print("=" * 60)
    print("  重置 Memory 数据库")
    print("=" * 60)

    if not os.path.exists(db_path):
        print(f"数据库文件不存在: {db_path}")
        print("无需重置，直接初始化即可。")
        return 0

    print(f"找到数据库: {db_path}")
    size = os.path.getsize(db_path) / 1024
    print(f"大小: {size:.1f} KB")
    print()

    # 确认
    choice = input("确定要重置数据库吗？这将删除所有数据！(yes/no): ").strip().lower()
    if choice != "yes" and choice != "y":
        print("操作已取消。")
        return 1

    # 备份
    backup = input("要先备份旧数据库吗？(yes/no): ").strip().lower()
    if backup == "yes" or backup == "y":
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"memory_backup_{timestamp}.db"
        try:
            shutil.copy2(db_path, backup_path)
            print(f"✓ 已备份到: {backup_path}")
        except Exception as e:
            print(f"备份失败: {e}")
            return 1

    # 删除
    try:
        os.remove(db_path)
        for ext in [".db-shm", ".db-wal"]:
            extra = db_path + ext
            if os.path.exists(extra):
                os.remove(extra)
        print("✓ 旧数据库已删除")
    except Exception as e:
        print(f"删除失败: {e}")
        return 1

    # 重新初始化
    print()
    print("正在初始化新数据库...")
    try:
        from memory_ai import MemoryDB
        db = MemoryDB()
        print("✓ 新数据库初始化成功！")
    except Exception as e:
        print(f"初始化失败: {e}")
        return 1

    print()
    print("=" * 60)
    print("  重置完成！")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
