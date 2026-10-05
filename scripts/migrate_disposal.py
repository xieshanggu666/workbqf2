"""存量库迁移：联合防汛处置协同四态闭环。

在既有幂等化基础上再做四件事（脚本幂等，可重复执行）：

1. 建立 disposal_orders 处置单表（Base.metadata.create_all 自动建表，
   老库直接补建，不动任何历史数据）；
2. warning_records / evacuation_records 增加 disposal_id 关联列
   （历史台账保持 NULL，原样展示，不参与唯一约束）；
3. operation_plans 按 run_id 去重并建立唯一索引
   （同一预报运行保留最近一份方案，与新模型的 uq_operation_plan_run 对齐）；
4. 建立 disposal_orders.run_id 唯一索引，数据库层兜住「一次运行一单」。

用法：python scripts/migrate_disposal.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.core.database import Base, engine

# 处置单表通过模型元数据补建（表已存在时 create_all 自动跳过）
IMPORTED = True  # noqa: F841  —— 导入即注册到 Base.metadata
from app import models  # noqa: E402,F401

UNIQUE_INDEXES = [
    ("uq_operation_plan_run", "operation_plans", "run_id"),
    ("uq_disposal_order_run", "disposal_orders", "run_id"),
]


def _columns(conn, table):
    return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}


def _add_column_if_missing(conn, table, column, ddl):
    if column not in _columns(conn, table):
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
        print(f"  + {table}.{column} 已添加")
    else:
        print(f"  = {table}.{column} 已存在，跳过")


def _exec(conn, sql):
    return conn.execute(text(sql)).rowcount


def main():
    # 1. 补建处置单表（新表，不影响存量数据）
    Base.metadata.create_all(bind=engine)
    print("[1/4] 处置单表 disposal_orders 已就绪")

    with engine.begin() as conn:
        print("[2/4] 补充处置单关联列（历史台账保持 NULL，原样保留）...")
        _add_column_if_missing(conn, "warning_records", "disposal_id",
                               "disposal_id INTEGER")
        _add_column_if_missing(conn, "evacuation_records", "disposal_id",
                               "disposal_id INTEGER")

        print("[3/4] 调度方案按 run_id 去重（同一运行保留最近一份）...")
        n = _exec(conn, """
            DELETE FROM operation_plans WHERE id NOT IN (
                SELECT MAX(id) FROM operation_plans GROUP BY run_id)""")
        print(f"  - 清理冗余方案 {n} 行")

        print("[4/4] 建立唯一索引（幂等键由数据库兜底）...")
        for name, table, cols in UNIQUE_INDEXES:
            conn.execute(text(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table} ({cols})"))
            print(f"  + {name} ON {table}({cols})")

    print("迁移完成：联合防汛处置协同（发起/审核/执行/完成）已启用。")


if __name__ == "__main__":
    main()
