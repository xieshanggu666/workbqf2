"""已审核处置单接收新一轮预报（增量调整）测试。

覆盖：
- 角色/状态校验（非调度员 403；待审核/已闭环 409）
- 已审核单接收新一轮预报：方案快照与水库工况增量调整、预警/转移台账增量挂接
- 人工状态保留（销警/处置中/转移中不被重跑覆盖）
- 资源占用兼容：已出库物资不重复扣减、执行中车辆保持发车，既有分配不丢
- 执行中订单新台账联动（新预警处置中、新转移直接进入转移中）
- 无新变化时重复接收幂等；历史运行（无台账）接收后补造并挂接台账
"""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models import (DisposalOrder, EvacuationRecord, FloodZone, ForecastRun,
                        OperationPlan, RainfallEvent, Reservoir, RiverNode,
                        RiverReach, Shelter, SubBasin, Supply, Vehicle,
                        WarningRecord, WaterStation)
from app.services import disposal, resources
from app.services.forecast import run_forecast

# 第二轮暴雨（雨量上调）：推动新一轮预报触发新的风险区与站点预警
ROUND2_HYETOGRAPH = [120.0] * 8


def _seed(db):
    """自洽流域：子流域 → 水库 → 出口站；两个风险区与两个水位站。

    出口站 rating 曲线不外延封顶，第一轮暴雨（300mm）只触发风险区1/主站，
    第二轮（960mm）才触发高风险区2与备用站，模拟"新一轮预报新增威胁"。
    """
    db.add_all([
        RiverNode(id=1, name="源头", kind="headwater"),
        RiverNode(id=2, name="库址", kind="reservoir"),
        RiverNode(id=3, name="出口", kind="outlet"),
        RiverReach(id=1, name="源→库", from_node_id=1, to_node_id=2,
                   k_hr=1.0, x_coef=0.2),
        RiverReach(id=2, name="库→出口", from_node_id=2, to_node_id=3,
                   k_hr=1.0, x_coef=0.2),
        SubBasin(id=1, name="子流域", area_km2=120.0, cn=88.0, lag_hr=1.0,
                 outlet_node_id=1),
        Reservoir(id=1, name="测试水库", node_id=2, normal_level=12.0,
                  flood_level=13.0, crest_level=16.0,
                  storage_curve=[[10, 100], [12, 300], [14, 600], [16, 1000], [18, 1500]],
                  discharge_curve=[[14, 0], [16, 200], [18, 600]],
                  gate_max=120.0, current_level=11.5, current_storage=300.0),
        WaterStation(id=1, name="出口水位站", node_id=3,
                     thresholds={"base_level": 5.0, "blue": 6.0, "yellow": 7.0,
                                 "orange": 8.0, "red": 9.0,
                                 "rating": [[0, 5.0], [30, 7.0], [60, 9.0], [100, 11.0],
                                            [300, 13.0], [1000, 15.0], [3000, 17.0]]}),
        WaterStation(id=2, name="备用水位站", node_id=3,
                     thresholds={"base_level": 5.0, "blue": 15.0, "yellow": 16.0,
                                 "orange": 16.5, "red": 17.0,
                                 "rating": [[0, 5.0], [30, 7.0], [60, 9.0], [100, 11.0],
                                            [300, 13.0], [1000, 15.0], [3000, 17.0]]}),
        FloodZone(id=1, name="沿岸村", node_id=3, population=500,
                  low_level=6.0, high_level=8.0),
        FloodZone(id=2, name="高地村", node_id=3, population=300,
                  low_level=15.0, high_level=16.0),
        RainfallEvent(id=1, name="测试暴雨", duration_h=6, total_mm=300.0,
                      hyetograph=[50.0] * 6),
        Shelter(id=1, name="一中避难点", capacity=900),
        Vehicle(id=1, plate="K001", kind="bus", seats=45, status="standby"),
        Vehicle(id=2, plate="K002", kind="bus", seats=45, status="standby"),
        Supply(id=1, name="饮用水", unit="箱", stock=100, safety_stock=10),
    ])
    db.commit()


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db",
                           connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    fac = sessionmaker(bind=engine)
    db = fac()
    _seed(db)
    db.close()
    yield fac
    engine.dispose()


def _forecast(fac, mode="natural"):
    db = fac()
    r = run_forecast(db, db.get(RainfallEvent, 1), mode)
    db.close()
    return r["run_id"]


def _approved_order(fac, mode="natural"):
    db = fac()
    rid = _forecast(fac, mode)
    o = disposal.initiate_order(db, rid, "张调度", "dispatcher")
    o = disposal.review_order(db, o["id"], "李值守", "duty")
    oid = o["id"]
    db.close()
    return oid, rid


def _second_round(fac):
    """雨量上调，形成新一轮预报输入。"""
    db = fac()
    ev = db.get(RainfallEvent, 1)
    ev.hyetograph = ROUND2_HYETOGRAPH
    ev.duration_h = len(ROUND2_HYETOGRAPH)
    ev.total_mm = sum(ROUND2_HYETOGRAPH)
    db.commit()
    db.close()


def _refresh(fac, oid, **kw):
    db = fac()
    o = disposal.refresh_order_forecast(db, oid, kw.pop("operator", "张调度"),
                                        kw.pop("role", "dispatcher"), **kw)
    db.close()
    return o


# ---------------- 角色与状态校验 ----------------
def test_refresh_requires_dispatcher_role(factory):
    oid, _ = _approved_order(factory)
    db = factory()
    for bad in ("duty", "transfer_lead", "supply_manager", "commander"):
        with pytest.raises(HTTPException) as ei:
            disposal.refresh_order_forecast(db, oid, "x", bad)
        assert ei.value.status_code == 403
    db.close()


def test_refresh_rejected_for_initiated_and_completed(factory):
    rid = _forecast(factory)
    db = factory()
    o = disposal.initiate_order(db, rid, "张调度", "dispatcher")
    # 待审核：尚未回写台账，不能接收
    with pytest.raises(HTTPException) as ei:
        disposal.refresh_order_forecast(db, o["id"], "张调度", "dispatcher")
    assert ei.value.status_code == 409
    # 已闭环：处置冻结，不能接收
    disposal.review_order(db, o["id"], "李值守", "duty")
    disposal.execute_order(db, o["id"], "王转移", "transfer_lead")
    disposal.complete_order(db, o["id"], "王转移", "transfer_lead")
    with pytest.raises(HTTPException) as ei:
        disposal.refresh_order_forecast(db, o["id"], "张调度", "dispatcher")
    assert ei.value.status_code == 409
    db.close()


def test_refresh_unknown_order_404(factory):
    db = factory()
    with pytest.raises(HTTPException) as ei:
        disposal.refresh_order_forecast(db, 999, "张调度", "dispatcher")
    assert ei.value.status_code == 404
    db.close()


# ---------------- 已审核单接收新一轮预报 ----------------
def test_approved_order_receives_new_forecast_round(factory):
    oid, rid = _approved_order(factory, "natural")

    # 人工处置痕迹：主站预警人工销警、风险区1 转移人工置为转移中
    db = factory()
    w1 = db.query(WarningRecord).filter(WarningRecord.run_id == rid,
                                        WarningRecord.target_id == 1).one()
    w1.status = "cleared"
    e1 = db.query(EvacuationRecord).filter(EvacuationRecord.run_id == rid,
                                           EvacuationRecord.zone_id == 1).one()
    e1.status = "moving"
    value_before = w1.value
    db.commit()
    db.close()

    _second_round(factory)
    o = _refresh(factory, oid, note="雨量上调至 960mm")

    # 状态机不变，处置记录留痕
    assert o["status"] == "approved"
    assert any("[新一轮预报]" in line and "960mm" in line
               for line in o["remark"].split("\n"))

    db = factory()
    # 新一轮预警增量挂接：备用站新预警入册并挂接本单
    warns = db.query(WarningRecord).filter(WarningRecord.run_id == rid).all()
    assert len(warns) == 2 and all(w.disposal_id == oid for w in warns)
    w1 = [w for w in warns if w.target_id == 1][0]
    w2 = [w for w in warns if w.target_id == 2][0]
    assert w1.status == "cleared"            # 人工销警保留
    assert w1.value > value_before           # 派生峰值随新轮刷新
    assert w2.status == "active"             # 未过调度令，新预警不进入处置中
    # 新一轮转移增量挂接：高地村新台账 pending 挂接，沿岸村人工状态保留
    evacs = db.query(EvacuationRecord).filter(EvacuationRecord.run_id == rid).all()
    assert len(evacs) == 2 and all(e.disposal_id == oid for e in evacs)
    e1 = [e for e in evacs if e.zone_id == 1][0]
    e2 = [e for e in evacs if e.zone_id == 2][0]
    assert e1.status == "moving" and e1.people == 500
    assert e2.status == "pending" and e2.people == 300
    # 运行与方案仍按幂等键归并，不重复造单
    assert db.query(ForecastRun).count() == 1
    assert db.query(OperationPlan).filter(OperationPlan.run_id == rid).count() == 1
    assert db.query(DisposalOrder).count() == 1
    db.close()


def test_refresh_adjusts_reservoir_plan_incrementally(factory):
    oid, rid = _approved_order(factory, "rule")
    db = factory()
    res = db.get(Reservoir, 1)
    finals1 = (res.current_level, res.current_storage)
    assert finals1 != (11.5, 300.0)  # 审核已按第一轮方案回写工况
    db.close()

    _second_round(factory)
    o = _refresh(factory, oid)

    db = factory()
    res = db.get(Reservoir, 1)
    snap = [x for x in o["plan"]["reservoirs"] if x["id"] == 1][0]
    # 水库工况按新一轮方案末态增量调整，且与快照一致
    assert (res.current_level, res.current_storage) != finals1
    assert res.current_level == snap["final_level"]
    assert res.current_storage == snap["final_storage"]
    # 快照更新为新一轮方案（峰值随雨量上调）
    order = db.get(DisposalOrder, oid)
    assert order.plan_snapshot["peak_flow"] == o["plan"]["peak_flow"]
    db.close()


# ---------------- 资源占用兼容：已出库物资 / 执行中车辆 ----------------
def _executed_order_with_resources(fac):
    """预报 → 审核 → 资源调度令（物资出库）→ 启动执行（车辆发车）。"""
    oid, rid = _approved_order(fac, "natural")
    db = fac()
    evac = db.query(EvacuationRecord).filter(EvacuationRecord.disposal_id == oid).one()
    resources.assign_shelter(db, oid, {"evacuation_id": evac.id, "shelter_id": 1,
                                       "people": 500, "role": "transfer_lead"})
    resources.assign_vehicle(db, oid, {"vehicle_id": 1, "evacuation_id": evac.id,
                                       "shuttles": 12, "role": "supply_manager"})
    resources.assign_supply(db, oid, {"supply_id": 1, "quantity": 80,
                                      "evacuation_id": evac.id, "role": "supply_manager"})
    resources.confirm_resources(db, oid, "赵指挥", "commander")
    disposal.execute_order(db, oid, "王转移", "transfer_lead")
    db.close()
    return oid, rid


def test_executed_order_refresh_keeps_issued_supplies_and_departed_vehicles(factory):
    oid, rid = _executed_order_with_resources(factory)
    db = factory()
    assert db.get(Supply, 1).stock == 20          # 80 箱已出库
    assert db.get(Vehicle, 1).status == "departed"
    db.close()

    _second_round(factory)
    o = _refresh(factory, oid)
    assert o["status"] == "executed"

    db = factory()
    # 已出库物资：库存不重复扣减、出库数量不回滚
    assert db.get(Supply, 1).stock == 20
    alloc = o["resources"]["supplies"][0]
    assert alloc["quantity"] == 80 and alloc["issued_quantity"] == 80
    # 执行中车辆：保持发车，既有分配不丢
    assert db.get(Vehicle, 1).status == "departed"
    assert len(o["resources"]["vehicles"]) == 1
    # 既有避难容量分配保留；新增需求使覆盖出现缺口
    assert o["resources"]["coverage"]["people"] == 800
    assert o["resources"]["coverage"]["shelter_seats"] == 500
    assert o["resources"]["coverage"]["shelter_ready"] is False
    # 人工/联动状态保留 + 新台账按执行中口径联动
    warns = db.query(WarningRecord).filter(WarningRecord.run_id == rid).all()
    assert {w.target_id: w.status for w in warns} == {1: "handling", 2: "handling"}
    evacs = db.query(EvacuationRecord).filter(EvacuationRecord.run_id == rid).all()
    assert {e.zone_id: e.status for e in evacs} == {1: "moving", 2: "moving"}
    assert all(e.disposal_id == oid for e in evacs)
    db.close()

    # 闭环仍可用：转移到位、预警销警、车辆归队
    db = factory()
    done = disposal.complete_order(db, oid, "王转移", "transfer_lead")
    assert done["status"] == "completed"
    assert db.get(Vehicle, 1).status == "returned"
    assert all(w.status == "cleared" for w in db.query(WarningRecord).all())
    db.close()


def test_resourced_order_refresh_then_incremental_confirm(factory):
    """资源已调度单接收新预报后，追加分配并再次确认调度令只出增量。"""
    oid, rid = _approved_order(factory, "natural")
    db = factory()
    evac = db.query(EvacuationRecord).filter(EvacuationRecord.disposal_id == oid).one()
    resources.assign_shelter(db, oid, {"evacuation_id": evac.id, "shelter_id": 1,
                                       "people": 500, "role": "transfer_lead"})
    resources.assign_vehicle(db, oid, {"vehicle_id": 1, "evacuation_id": evac.id,
                                       "shuttles": 12, "role": "supply_manager"})
    resources.assign_supply(db, oid, {"supply_id": 1, "quantity": 80,
                                      "evacuation_id": evac.id, "role": "supply_manager"})
    resources.confirm_resources(db, oid, "赵指挥", "commander")
    assert db.get(Supply, 1).stock == 20
    db.close()

    _second_round(factory)
    o = _refresh(factory, oid)
    assert o["status"] == "resourced"
    # 新转移台账 pending（未启动执行），新预警已进入处置中
    db = factory()
    e2 = db.query(EvacuationRecord).filter(EvacuationRecord.zone_id == 2).one()
    assert e2.status == "pending" and e2.disposal_id == oid
    w2 = db.query(WarningRecord).filter(WarningRecord.target_id == 2).one()
    assert w2.status == "handling"

    # 追加分配：高地村避难容量 + 车辆运力 + 物资，再次确认调度令只出库增量
    resources.assign_shelter(db, oid, {"evacuation_id": e2.id, "shelter_id": 1,
                                       "people": 300, "role": "transfer_lead"})
    resources.assign_vehicle(db, oid, {"vehicle_id": 2, "evacuation_id": e2.id,
                                       "shuttles": 7, "role": "supply_manager"})
    resources.assign_supply(db, oid, {"supply_id": 1, "quantity": 20,
                                      "evacuation_id": e2.id, "role": "supply_manager"})
    plan = resources.confirm_resources(db, oid, "赵指挥", "commander")
    assert db.get(Supply, 1).stock == 0           # 20 - 20 增量出库
    assert plan["coverage"]["shelter_seats"] == 800
    assert plan["coverage"]["shelter_ready"] is True
    assert plan["coverage"]["vehicle_ready"] is True
    db.close()


# ---------------- 幂等与历史兼容 ----------------
def test_refresh_is_idempotent_when_forecast_unchanged(factory):
    oid, rid = _approved_order(factory, "natural")
    o1 = _refresh(factory, oid)
    db = factory()
    counts = (db.query(WarningRecord).count(), db.query(EvacuationRecord).count(),
              db.query(OperationPlan).count(), db.get(Reservoir, 1).current_level)
    db.close()
    o2 = _refresh(factory, oid)
    db = factory()
    assert (db.query(WarningRecord).count(), db.query(EvacuationRecord).count(),
            db.query(OperationPlan).count(),
            db.get(Reservoir, 1).current_level) == counts
    # 快照一致，处置记录各留一行
    assert o1["plan"]["peak_flow"] == o2["plan"]["peak_flow"]
    assert o2["remark"].count("[新一轮预报]") == 2
    db.close()


def test_legacy_approved_order_backfills_ledgers_on_refresh(factory):
    """历史运行（无方案/台账）审核通过后接收新一轮预报：补造台账并挂接。"""
    db = factory()
    run = ForecastRun(event_id=1, mode="natural", status="done")
    db.add(run)
    db.commit()
    rid = run.id
    db.close()

    db = factory()
    o = disposal.initiate_order(db, rid, "张调度", "dispatcher")
    o = disposal.review_order(db, o["id"], "李值守", "duty")
    assert o["linked_warnings"] == 0 and o["linked_evacuations"] == 0
    oid = o["id"]
    db.close()

    o = _refresh(factory, oid)
    assert o["status"] == "approved"
    assert o["linked_warnings"] >= 1 and o["linked_evacuations"] == 1
    db = factory()
    assert all(w.disposal_id == oid for w in db.query(WarningRecord).all())
    assert all(e.disposal_id == oid for e in db.query(EvacuationRecord).all())
    db.close()
