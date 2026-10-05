"""初始化演示数据：青岚江流域（子流域/河网/水库/站点/风险区）+ 历史降雨情景。

在项目根目录运行：python scripts/init_db.py
会删除旧库并重建，注入一套可运行自洽的流域水文数据。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import Base, engine, SessionLocal
from app.models import (FloodZone, RainStation, RainfallEvent, Reservoir, RiverNode,
                        RiverReach, Shelter, SubBasin, Supply, Vehicle, WaterStation)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "flood.db")


def scale_hydro(mm_h, factor):
    return [round(x * factor, 1) for x in mm_h]


def main():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    # ---------- 河网节点 ----------
    nodes = [
        RiverNode(id=1, name="青岚源", kind="headwater", desc="主河源头", x=180, y=90),
        RiverNode(id=2, name="东溪", kind="headwater", desc="支流源头", x=420, y=80),
        RiverNode(id=3, name="青峰溪汇合", kind="junction", desc="主支汇合", x=300, y=210),
        RiverNode(id=4, name="青峰水库", kind="reservoir", desc="龙头水库", x=150, y=360),
        RiverNode(id=5, name="白水渡", kind="control", desc="控制断面", x=330, y=420),
        RiverNode(id=6, name="龙潭水库", kind="reservoir", desc="下游水库", x=520, y=430),
        RiverNode(id=7, name="古窑", kind="control", desc="控制断面", x=420, y=560),
        RiverNode(id=8, name="青岚河河口", kind="outlet", desc="河口", x=700, y=580),
    ]
    db.add_all(nodes)

    # ---------- 河段 ----------
    reaches = [
        RiverReach(id=1, name="青岚源→汇合", from_node_id=1, to_node_id=3, length_km=9, slope=0.012, k_hr=1.4, x_coef=0.20),
        RiverReach(id=2, name="东溪→汇合", from_node_id=2, to_node_id=3, length_km=7, slope=0.015, k_hr=1.1, x_coef=0.22),
        RiverReach(id=3, name="汇合→青峰", from_node_id=3, to_node_id=4, length_km=5, slope=0.010, k_hr=0.9, x_coef=0.20),
        RiverReach(id=4, name="青峰→白水渡", from_node_id=4, to_node_id=5, length_km=4, slope=0.008, k_hr=0.7, x_coef=0.20),
        RiverReach(id=5, name="白水渡→龙潭", from_node_id=5, to_node_id=6, length_km=8, slope=0.006, k_hr=1.2, x_coef=0.22),
        RiverReach(id=6, name="龙潭→古窑", from_node_id=6, to_node_id=7, length_km=6, slope=0.005, k_hr=1.0, x_coef=0.20),
        RiverReach(id=7, name="古窑→河口", from_node_id=7, to_node_id=8, length_km=3, slope=0.004, k_hr=0.6, x_coef=0.20),
    ]
    db.add_all(reaches)

    # ---------- 子流域（产流单元）----------
    # 面积经水文规模校准：上游集水区约 4425 km²（×15），青峰—白水渡区间取 40%
    subs = [
        SubBasin(id=1, name="青岚源流域", area_km2=1800, cn=82, lag_hr=2.5, outlet_node_id=1, x=120, y=60),
        SubBasin(id=2, name="东溪流域", area_km2=1425, cn=78, lag_hr=2.1, outlet_node_id=2, x=470, y=50),
        SubBasin(id=3, name="汇合区间", area_km2=1200, cn=85, lag_hr=2.0, outlet_node_id=3, x=300, y=160),
        SubBasin(id=4, name="白水渡区间", area_km2=660, cn=80, lag_hr=2.4, outlet_node_id=5, x=330, y=330),
    ]
    db.add_all(subs)

    # ---------- 雨量站 ----------
    rain_stations = [
        RainStation(id=1, name="青岚源雨量站", node_id=1, x=120, y=60),
        RainStation(id=2, name="东溪雨量站", node_id=2, x=470, y=50),
        RainStation(id=3, name="白水渡雨量站", node_id=5, x=330, y=330),
    ]
    db.add_all(rain_stations)

    # ---------- 水位站（含预警阈值与 rating 水位流量曲线）----------
    stations = [
        WaterStation(id=1, name="青峰水库出口站", node_id=4, x=150, y=360,
                     thresholds={"base_level": 78.0, "blue": 84.0, "yellow": 86.0,
                                 "orange": 88.0, "red": 90.0,
                                 "rating": [[0, 78.0], [800, 84.0], [1400, 88.0], [2000, 91.0]]}),
        WaterStation(id=2, name="白水渡水位站", node_id=5, x=330, y=420,
                     thresholds={"base_level": 25.0, "blue": 28.5, "yellow": 30.0,
                                 "orange": 31.5, "red": 33.0,
                                 "rating": [[0, 25.0], [600, 28.5], [1200, 31.0], [1800, 33.5]]}),
        WaterStation(id=3, name="古窑水位站", node_id=7, x=420, y=560,
                     thresholds={"base_level": 12.0, "blue": 15.0, "yellow": 16.5,
                                 "orange": 18.0, "red": 19.5,
                                 "rating": [[0, 12.0], [700, 15.0], [1500, 17.5], [2400, 20.0]]}),
        WaterStation(id=4, name="龙潭水位站", node_id=6, x=520, y=430,
                     thresholds={"base_level": 9.0, "blue": 12.5, "yellow": 14.0,
                                 "orange": 15.0, "red": 17.0,
                                 "rating": [[0, 9.0], [700, 12.5], [1400, 15.0], [2000, 17.5]]}),
    ]
    db.add_all(stations)

    # ---------- 水库 ----------
    qing_start = 78.0
    reservoirs = [
        Reservoir(id=1, name="青峰水库", node_id=4, normal_level=74.0, flood_level=80.0,
                  crest_level=100.0,
                  storage_curve=[[60, 3000], [70, 6200], [80, 11000], [90, 17200], [100, 24000], [110, 30000]],
                  discharge_curve=[[80, 0], [85, 600], [90, 1600], [95, 3200], [100, 5000], [105, 7200]],
                  gate_max=2500.0, current_level=qing_start,
                  current_storage=None, active=1, x=150, y=360),
        Reservoir(id=2, name="龙潭水库", node_id=6, normal_level=9.0, flood_level=12.0,
                  crest_level=21.0,
                  storage_curve=[[5, 800], [10, 2400], [15, 5200], [20, 9200], [25, 14000]],
                  discharge_curve=[[12, 0], [16, 1200], [18, 2600], [21, 4600]],
                  gate_max=3200.0, current_level=11.0, active=1, x=520, y=430),
    ]
    db.add_all(reservoirs)

    # ---------- 淹没风险区 ----------
    zones = [
        FloodZone(id=1, name="白水渡城区", node_id=5, population=32000, risk_level="high",
                  low_level=30.0, high_level=31.5, x=330, y=470,
                  route=[[330, 470, "白水高中"], [400, 470, "绕城路"], [470, 390, "避险点"]]),
        FloodZone(id=2, name="龙潭镇", node_id=6, population=18000, risk_level="medium",
                  low_level=15.0, high_level=17.0, x=520, y=500,
                  route=[[520, 500, "龙潭小学"], [560, 500, "镇道"], [650, 470, "避险点"]]),
    ]
    db.add_all(zones)

    # ---------- 应急避难点 ----------
    shelters = [
        Shelter(id=1, name="白水渡第一避难点（白水高中体育馆）", address="白水渡镇绕城路 88 号",
                capacity=18000, contact="周校长 138-0001", x=470, y=390),
        Shelter(id=2, name="白水渡第二避难点（镇文化中心）", address="白水渡镇府前路 12 号",
                capacity=16000, contact="吴主任 138-0002", x=380, y=380),
        Shelter(id=3, name="龙潭避难点（龙潭中学）", address="龙潭镇育才路 6 号",
                capacity=20000, contact="郑校长 138-0003", x=650, y=470),
    ]
    db.add_all(shelters)

    # ---------- 应急车辆 ----------
    vehicles = [
        Vehicle(id=1, plate="赣A·K1001", kind="bus", seats=45, team="县客运一队", status="standby"),
        Vehicle(id=2, plate="赣A·K1002", kind="bus", seats=45, team="县客运一队", status="standby"),
        Vehicle(id=3, plate="赣A·K2001", kind="bus", seats=45, team="县客运二队", status="standby"),
        Vehicle(id=4, plate="赣A·K2002", kind="bus", seats=45, team="县客运二队", status="standby"),
        Vehicle(id=5, plate="赣A·H3001", kind="truck", seats=5, team="县应急物资车队", status="standby"),
        Vehicle(id=6, plate="赣A·H3002", kind="truck", seats=5, team="县应急物资车队", status="standby"),
        Vehicle(id=7, plate="赣A·J9001", kind="ambulance", seats=6, team="县急救中心", status="standby"),
    ]
    db.add_all(vehicles)

    # ---------- 应急物资 ----------
    supplies = [
        Supply(id=1, name="瓶装饮用水", unit="箱", stock=1200, safety_stock=200),
        Supply(id=2, name="方便食品", unit="箱", stock=900, safety_stock=150),
        Supply(id=3, name="棉被", unit="床", stock=6000, safety_stock=800),
        Supply(id=4, name="急救药箱", unit="个", stock=300, safety_stock=60),
        Supply(id=5, name="编织袋", unit="条", stock=20000, safety_stock=3000),
    ]
    db.add_all(supplies)

    # ---------- 降雨情景 ----------
    mm64 = [2, 3, 4, 5, 6, 8, 10, 12, 14, 16, 17, 16, 14, 12, 10, 8, 6, 5, 4, 3, 3, 2, 2, 2]
    mm100 = [3, 4, 5, 7, 9, 12, 16, 20, 24, 26, 28, 26, 22, 18, 14, 10, 8, 6, 5, 4, 3, 3, 2, 2]
    events = [
        RainfallEvent(id=1, name="2024·梅汛暴雨", return_period="5年一遇", duration_h=24,
                      total_mm=round(sum(mm64), 1), hyetograph=mm64,
                      note="短历时强降雨，山区易发山洪"),
        RainfallEvent(id=2, name="1998·特大暴雨", return_period="100年一遇", duration_h=24,
                      total_mm=round(sum(mm100), 1), hyetograph=mm100,
                      note="流域大范围超标准降雨"),
        RainfallEvent(id=3, name="锦江暴雨还原", return_period="10年一遇", duration_h=24,
                      total_mm=round(sum(scale_hydro(mm64, 0.80)), 1),
                      hyetograph=scale_hydro(mm64, 0.80),
                      note="中等强度连续降雨情景"),
    ]
    db.add_all(events)

    # 计算青峰水库当前库容（由水位经库容曲线换算）
    for r in reservoirs:
        r.current_storage = r.storage_at(r.current_level)

    db.commit()
    db.close()
    print("数据库初始化完成：青岚江流域（4子流域、2水库调度、3降雨情景、2风险区、3避难点、7车辆、5类物资）")


if __name__ == "__main__":
    main()