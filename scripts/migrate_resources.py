"""存量库迁移：应急资源与避难点协同调度。

在处置协同四态闭环基础上再做四件事（脚本幂等，可重复执行）：

1. 建立应急资源表 shelters / vehicles / supplies 及三张协同分配表
   shelter_assignments / vehicle_dispatches / supply_allocations
   （Base.metadata.create_all 自动补建，不动任何历史数据）；
2. disposal_orders 增加 resourced_by / resourced_at 列
   （历史处置单保持 NULL，状态沿用 initiated/approved/executed/completed，
   approved 与 resourced 均可启动执行，历史流转不受影响）；
3. evacuation_records 增加 shelter_id / shelter_name 回写列
   （历史转移记录保持 NULL，原样展示）；supply_allocations 增加
   issued_quantity 已出库数量列（历史分配视为未出库）；
4. 资源台账为空时补入演示用避难点/车辆/物资（已有资源数据则跳过）。

用法：python scripts/migrate_resources.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.core.database import Base, engine, SessionLocal
from app.models import Shelter, Supply, Vehicle  # noqa: F401  导入即注册到 Base.metadata

IMPORTED = True  # noqa: F841
from app import models  # noqa: E402,F401


def _columns(conn, table):
    return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}


def _add_column_if_missing(conn, table, column, ddl):
    if column not in _columns(conn, table):
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
        print(f"  + {table}.{column} 已添加")
    else:
        print(f"  = {table}.{column} 已存在，跳过")


def _seed_demo_resources():
    """资源台账为空时补入演示资源（用户已录入资源则不覆盖）。"""
    db = SessionLocal()
    try:
        if db.query(Shelter).count() == 0 and db.query(Vehicle).count() == 0 \
                and db.query(Supply).count() == 0:
            db.add_all([
                Shelter(id=1, name="白水渡第一避难点（白水高中体育馆）",
                        address="白水渡镇绕城路 88 号", capacity=18000,
                        contact="周校长 138-0001", x=470, y=390),
                Shelter(id=2, name="白水渡第二避难点（镇文化中心）",
                        address="白水渡镇府前路 12 号", capacity=16000,
                        contact="吴主任 138-0002", x=380, y=380),
                Shelter(id=3, name="龙潭避难点（龙潭中学）",
                        address="龙潭镇育才路 6 号", capacity=20000,
                        contact="郑校长 138-0003", x=650, y=470),
                Vehicle(id=1, plate="赣A·K1001", kind="bus", seats=45,
                        team="县客运一队", status="standby"),
                Vehicle(id=2, plate="赣A·K1002", kind="bus", seats=45,
                        team="县客运一队", status="standby"),
                Vehicle(id=3, plate="赣A·K2001", kind="bus", seats=45,
                        team="县客运二队", status="standby"),
                Vehicle(id=4, plate="赣A·K2002", kind="bus", seats=45,
                        team="县客运二队", status="standby"),
                Vehicle(id=5, plate="赣A·H3001", kind="truck", seats=5,
                        team="县应急物资车队", status="standby"),
                Vehicle(id=6, plate="赣A·H3002", kind="truck", seats=5,
                        team="县应急物资车队", status="standby"),
                Vehicle(id=7, plate="赣A·J9001", kind="ambulance", seats=6,
                        team="县急救中心", status="standby"),
                Supply(id=1, name="瓶装饮用水", unit="箱", stock=1200, safety_stock=200),
                Supply(id=2, name="方便食品", unit="箱", stock=900, safety_stock=150),
                Supply(id=3, name="棉被", unit="床", stock=6000, safety_stock=800),
                Supply(id=4, name="急救药箱", unit="个", stock=300, safety_stock=60),
                Supply(id=5, name="编织袋", unit="条", stock=20000, safety_stock=3000),
            ])
            db.commit()
            print("  + 已补入演示资源：3 避难点 / 7 车辆 / 5 类物资")
        else:
            print("  = 资源台账已有数据，跳过演示资源补入")
    finally:
        db.close()


def main():
    # 1. 补建资源表与协同分配表（新表，不影响存量数据）
    Base.metadata.create_all(bind=engine)
    print("[1/4] 应急资源与协同分配表已就绪")

    with engine.begin() as conn:
        print("[2/4] 补充处置单资源调度列（历史处置单保持 NULL）...")
        _add_column_if_missing(conn, "disposal_orders", "resourced_by",
                               "resourced_by VARCHAR(64)")
        _add_column_if_missing(conn, "disposal_orders", "resourced_at",
                               "resourced_at DATETIME")

        print("[3/4] 补充转移台账避难点回写列（历史记录保持 NULL）...")
        _add_column_if_missing(conn, "evacuation_records", "shelter_id",
                               "shelter_id INTEGER")
        _add_column_if_missing(conn, "evacuation_records", "shelter_name",
                               "shelter_name VARCHAR(64)")
        _add_column_if_missing(conn, "supply_allocations", "issued_quantity",
                               "issued_quantity INTEGER DEFAULT 0")

        print("[4/4] 建立协同分配唯一索引（幂等键由数据库兜底）...")
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_shelter_assign "
            "ON shelter_assignments (disposal_id, evacuation_id, shelter_id)"))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_vehicle_dispatch "
            "ON vehicle_dispatches (disposal_id, vehicle_id)"))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_supply_alloc "
            "ON supply_allocations (disposal_id, evacuation_id, supply_id)"))
        print("  + uq_shelter_assign / uq_vehicle_dispatch / uq_supply_alloc")

    _seed_demo_resources()
    print("迁移完成：应急资源与避难点协同调度（避难容量/车辆/物资 → 调度令回写）已启用。")


if __name__ == "__main__":
    main()
