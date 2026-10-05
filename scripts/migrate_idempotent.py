"""存量库幂等化迁移：建立预报运行与处置记录的幂等关联。

背景：早期版本重复执行同一洪水预报会反复新增预警与转移台账，
且处置记录与预报运行无关联。本脚本对既有 SQLite 库做四件事：

1. forecast_runs 按 (event_id, mode) 去重（同一情景×工况保留最近一次运行，
   其余运行及其过程线、调度方案一并清理）；
2. warning_records / evacuation_records 增加 run_id 列
   （历史记录保持 NULL：NULL 不参与唯一约束，历史台账原样保留展示）；
3. 历史处置记录按业务键去重（预警: target_type+target_id+kind；
   转移: zone_id+triggered_by），保留最早一条；
4. 建立与模型同名的唯一索引，从数据库层杜绝再次重复。

脚本幂等，可重复执行。用法：python scripts/migrate_idempotent.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.core.database import engine

# 与 app.models 中各 __table_args__ 唯一约束保持一致
UNIQUE_INDEXES = [
    ("uq_forecast_run_event_mode", "forecast_runs", "event_id, mode"),
    ("uq_series_run_node_kind", "forecast_series", "run_id, node_id, kind, name"),
    ("uq_warning_run_target", "warning_records", "run_id, target_type, target_id, kind"),
    ("uq_evacuation_run_zone", "evacuation_records", "run_id, zone_id"),
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
    with engine.begin() as conn:
        print("[1/4] 补充 run_id 关联列（历史记录保持 NULL，原样保留）...")
        _add_column_if_missing(conn, "warning_records", "run_id", "run_id INTEGER")
        _add_column_if_missing(conn, "evacuation_records", "run_id", "run_id INTEGER")

        print("[2/4] 历史预报运行去重（同一情景×工况保留最近一次）...")
        stale = ("SELECT id FROM forecast_runs WHERE id NOT IN "
                 "(SELECT MAX(id) FROM forecast_runs GROUP BY event_id, mode)")
        n = _exec(conn, f"DELETE FROM forecast_series WHERE run_id IN ({stale})")
        n += _exec(conn, f"DELETE FROM operation_plans WHERE run_id IN ({stale})")
        n += _exec(conn, f"DELETE FROM forecast_runs WHERE id IN ({stale})")
        # 同一运行内可能残留重复过程线（按业务键保留最早一条）
        n += _exec(conn, """
            DELETE FROM forecast_series WHERE id NOT IN (
                SELECT MIN(id) FROM forecast_series
                GROUP BY run_id, node_id, kind, name)""")
        print(f"  - 清理冗余运行/过程线 {n} 行")

        print("[3/4] 历史处置记录去重（保留最早一条）...")
        n = _exec(conn, """
            DELETE FROM warning_records WHERE run_id IS NULL AND id NOT IN (
                SELECT MIN(id) FROM warning_records WHERE run_id IS NULL
                GROUP BY target_type, target_id, kind)""")
        print(f"  - 重复预警记录 {n} 行")
        n = _exec(conn, """
            DELETE FROM evacuation_records WHERE run_id IS NULL AND id NOT IN (
                SELECT MIN(id) FROM evacuation_records WHERE run_id IS NULL
                GROUP BY zone_id, triggered_by)""")
        print(f"  - 重复转移记录 {n} 行")

        print("[4/4] 建立唯一索引（幂等键由数据库兜底）...")
        for name, table, cols in UNIQUE_INDEXES:
            conn.execute(text(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table} ({cols})"))
            print(f"  + {name} ON {table}({cols})")

    print("迁移完成：预报运行与处置记录已建立幂等关联。")


if __name__ == "__main__":
    main()
