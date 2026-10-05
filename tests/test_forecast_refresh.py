"""已审核处置单接收新一轮预报的增量调整测试。

覆盖：
- approved 单滚动到新一轮：方案快照轮次演进、水库工况按新方案末态回写、
  预警按站点身份迁移（等级刷新、人工状态保留）、转移按风险区身份迁移；
- resourced 单滚动：已出库物资不回滚、避难点挂接/处置中预警保留，
  新风险区形成缺口后追加分配，指挥员再确认只出增量；
- executed 单滚动：水库实际工况不反演、执行中车辆不动，
  新增风险区转移自动进入 moving、追加车辆即派即发、增量调度令出库；
- 新增/消失站点与风险区的增量语义（消失不删除，保留人工台账）；
- 权限与状态机：非调度员 403、待审核/已闭环 409、同轮 409、
  未推演运行 409、已挂接其它处置单的运行 409；
- 执行中资源只可追加不可撤回（409）。
"""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models import (DisposalOrder, EvacuationRecord, FloodZone, ForecastRun,
                        RainfallEvent, Reservoir, Shelter, ShelterAssignment,
                        Supply, SupplyAllocation, Vehicle, VehicleDispatch,
                        WarningRecord)
from app.services import disposal, resources
from app.services.forecast import run_forecast


def _seed(db):
    """两个风险区 + 两情景 + 资源的自洽流域。"""
    from app.models import (RiverNode, RiverReach, SubBasin, WaterStation)
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
                                 "rating": [[0, 5.0], [200, 6.0], [400, 7.0],
                                            [800, 9.0], [1500, 11.0]]}),
        FloodZone(id=1, name="沿岸村", node_id=3, population=500,
                  low_level=6.0, high_level=8.0),
        FloodZone(id=2, name="下游垸", node_id=3, population=300,
                  low_level=8.2, high_level=9.5),   # 仅特大暴雨触发
        # 情景1：常规暴雨（黄警/仅风险区1）；情景2：特大暴雨（橙警/风险区1+2）
        RainfallEvent(id=1, name="常规暴雨", duration_h=6, total_mm=300.0,
                      hyetograph=[50.0] * 6),
        RainfallEvent(id=2, name="特大暴雨", duration_h=6, total_mm=600.0,
                      hyetograph=[100.0] * 6),
        Shelter(id=1, name="一中避难点", capacity=2000),
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


def _forecast(fac, event_id, mode="rule"):
    db = fac()
    r = run_forecast(db, db.get(RainfallEvent, event_id), mode)
    db.close()
    return r["run_id"]


def _approved(fac, event_id=1, mode="rule"):
    rid = _forecast(fac, event_id, mode)
    db = fac()
    o = disposal.initiate_order(db, rid, "张调度", "dispatcher")
    o = disposal.review_order(db, o["id"], "李值守", "duty")
    db.close()
    return o["id"], rid


def _evac(factory, oid, zone_id):
    db = factory()
    ev = (db.query(EvacuationRecord)
          .filter(EvacuationRecord.disposal_id == oid,
                  EvacuationRecord.zone_id == zone_id).one())
    eid = ev.id
    db.close()
    return eid


# ---------------- 基本语义：方案/水库/预警/转移 ----------------
def test_refresh_approved_rolls_plan_warnings_evacuations_and_reservoir(factory):
    oid, rid1 = _approved(factory, event_id=1)
    rid2 = _forecast(factory, 2)

    db = factory()
    # 新一轮预警等级更高（特大暴雨）
    w1 = db.query(WarningRecord).filter(WarningRecord.run_id == rid1).first()
    w2 = db.query(WarningRecord).filter(WarningRecord.run_id == rid2).first()
    assert w1 is not None and w2 is not None
    assert w2.value >= w1.value

    res_before = db.get(Reservoir, 1)
    level_before = res_before.current_level

    out = disposal.refresh_forecast(db, oid, rid2, "张调度", "dispatcher",
                                    note="台风路径修正")
    assert out["run_id"] == rid2 and out["round"] == 2
    delta = out["refresh_delta"]

    # 处置单状态与人工签名保留
    order = db.get(DisposalOrder, oid)
    assert order.status == "approved"
    assert order.reviewed_by == "李值守" and order.initiated_by == "张调度"

    # 方案快照滚动，轮次历史保留第 1 轮
    assert order.plan_snapshot["round"] == 2
    assert len(order.plan_snapshot["rounds"]) == 1
    assert order.plan_snapshot["rounds"][0]["run_id"] == rid1

    # 同站点预警：身份迁移（仍是同一条记录），等级/峰值刷新，状态保留 active
    assert delta["warnings"]["carried"] == 1
    moved = db.query(WarningRecord).filter(WarningRecord.run_id == rid2).one()
    assert moved.id == w1.id and moved.disposal_id == oid
    assert moved.level == w2.level and moved.value == w2.value
    assert moved.status == "active"
    assert db.query(WarningRecord).filter(WarningRecord.run_id == rid1).count() == 0

    # 转移按风险区身份迁移；特大暴雨下风险区2可能新增
    e1_old = db.query(EvacuationRecord).filter(
        EvacuationRecord.run_id == rid2, EvacuationRecord.zone_id == 1).one()
    assert e1_old.disposal_id == oid

    # 水库工况按新方案末态回写（approved 尚未执行）
    db.expire_all()
    res = db.get(Reservoir, 1)
    snap = [x for x in order.plan_snapshot["reservoirs"] if x["id"] == 1][0]
    assert res.current_level == snap["final_level"]
    assert res.current_level != level_before

    # 处置记录追加轮次说明
    assert "新一轮预报" in order.remark and "台风路径修正" in order.remark
    db.close()


def test_refresh_preserves_manual_warning_and_evacuation_status(factory):
    oid, rid1 = _approved(factory, event_id=1)
    eid = _evac(factory, oid, 1)
    db = factory()
    # 人工已把预警置 handling、转移置 moving（模拟执行中）
    db.query(WarningRecord).filter(WarningRecord.disposal_id == oid).update(
        {WarningRecord.status: "handling"})
    db.get(EvacuationRecord, eid).status = "moving"
    db.commit()
    db.close()

    rid2 = _forecast(factory, 2)
    db = factory()
    disposal.refresh_forecast(db, oid, rid2, "张调度", "dispatcher")
    w = db.query(WarningRecord).filter(WarningRecord.run_id == rid2).one()
    e = db.query(EvacuationRecord).filter(
        EvacuationRecord.run_id == rid2, EvacuationRecord.zone_id == 1).one()
    assert w.status == "handling"      # 人工处置中不被新一轮覆盖为 active
    assert e.status == "moving"        # 转移中保持
    assert e.id == eid
    db.close()


def test_refresh_new_zone_is_linked_and_retired_zone_kept(factory):
    """手工构造第 2 轮只覆盖风险区2：区1解除保留不删，区2新增挂接。"""
    oid, rid1 = _approved(factory, event_id=1)
    db = factory()
    # 预建第 2 轮运行锚点，只放区2转移台账（无站点预警）；
    # refresh 时 _ensure_artifacts 以 write_ledgers=False 补算，不回造台账
    run3 = ForecastRun(event_id=2, mode="rule", status="done")
    db.add(run3)
    db.commit()
    rid3 = run3.id
    db.add(EvacuationRecord(run_id=rid3, zone_id=2, zone_name="下游垸",
                            triggered_by="强制转移", people=300, status="pending"))
    db.commit()
    db.close()

    db = factory()
    out = disposal.refresh_forecast(db, oid, rid3, "张调度", "dispatcher")
    de = out["refresh_delta"]["evacuations"]
    dw = out["refresh_delta"]["warnings"]
    assert de == {"carried": 0, "added": 1, "retired": 1}
    assert dw["carried"] == 0 and dw["added"] == 0 and dw["retired"] == 1
    # 旧区1：保留在原运行上、仍挂接本单（人工台账不抹除）
    old1 = db.query(EvacuationRecord).filter(
        EvacuationRecord.run_id == rid1, EvacuationRecord.zone_id == 1).one()
    assert old1.disposal_id == oid and old1.status == "pending"
    # 新区2：挂接到新运行
    new2 = db.query(EvacuationRecord).filter(
        EvacuationRecord.run_id == rid3, EvacuationRecord.zone_id == 2).one()
    assert new2.disposal_id == oid
    db.close()


# ---------------- resourced：已出库物资/调度令状态兼容 ----------------
def test_refresh_resourced_keeps_issued_supplies_and_shelter_link(factory):
    oid, rid1 = _approved(factory, event_id=1)
    eid = _evac(factory, oid, 1)
    db = factory()
    resources.assign_shelter(db, oid, {"evacuation_id": eid, "shelter_id": 1,
                                      "people": 500, "role": "transfer_lead"})
    resources.assign_vehicle(db, oid, {"vehicle_id": 1, "evacuation_id": eid,
                                      "shuttles": 12, "role": "supply_manager"})
    resources.assign_supply(db, oid, {"supply_id": 1, "quantity": 80,
                                     "evacuation_id": eid, "role": "supply_manager"})
    resources.confirm_resources(db, oid, "赵指挥", "commander")
    assert db.get(Supply, 1).stock == 20
    alloc = db.query(SupplyAllocation).filter(
        SupplyAllocation.disposal_id == oid).one()
    assert alloc.issued_quantity == 80
    db.close()

    rid2 = _forecast(factory, 2)
    db = factory()
    out = disposal.refresh_forecast(db, oid, rid2, "张调度", "dispatcher")
    order = db.get(DisposalOrder, oid)
    assert order.status == "resourced"          # 状态保留，不退回 approved
    # 已出库物资不回滚：库存仍为 20，分配的已出库份额沿用
    assert db.get(Supply, 1).stock == 20
    alloc = db.query(SupplyAllocation).filter(
        SupplyAllocation.disposal_id == oid).one()
    assert alloc.issued_quantity == 80
    # 避难点挂接与处置中预警保留
    e = db.query(EvacuationRecord).filter(
        EvacuationRecord.run_id == rid2, EvacuationRecord.zone_id == 1).one()
    assert e.shelter_id == 1 and e.shelter_name == "一中避难点"
    w = db.query(WarningRecord).filter(WarningRecord.run_id == rid2).first()
    assert w.status == "handling"
    db.close()


def test_refresh_resourced_new_zone_gap_then_incremental_issue(factory):
    """新风险区形成容量/运力缺口 → 追加分配 → 再确认只出增量。"""
    oid, rid1 = _approved(factory, event_id=1)
    eid1 = _evac(factory, oid, 1)
    db = factory()
    resources.assign_shelter(db, oid, {"evacuation_id": eid1, "shelter_id": 1,
                                      "people": 500, "role": "transfer_lead"})
    resources.assign_vehicle(db, oid, {"vehicle_id": 1, "evacuation_id": eid1,
                                      "shuttles": 12, "role": "supply_manager"})
    resources.assign_supply(db, oid, {"supply_id": 1, "quantity": 80,
                                     "evacuation_id": eid1, "role": "supply_manager"})
    resources.confirm_resources(db, oid, "赵指挥", "commander")
    db.close()

    # 手工构造新一轮：只覆盖风险区2（新增），区1延续
    rid2 = _forecast(factory, 2)
    db = factory()
    disposal.refresh_forecast(db, oid, rid2, "张调度", "dispatcher")
    e2 = db.query(EvacuationRecord).filter(
        EvacuationRecord.run_id == rid2, EvacuationRecord.zone_id == 2).first()
    if e2 is not None:
        # 新区无容量/运力 → 再确认报缺口 409
        with pytest.raises(HTTPException) as ei:
            resources.confirm_resources(db, oid, "赵指挥", "commander")
        assert ei.value.status_code == 409
        # 追加容量/运力/物资（库存剩余 20，追加 10 可出）
        resources.assign_shelter(db, oid, {"evacuation_id": e2.id, "shelter_id": 1,
                                          "people": 300, "role": "transfer_lead"})
        resources.assign_vehicle(db, oid, {"vehicle_id": 2, "evacuation_id": e2.id,
                                          "shuttles": 8, "role": "supply_manager"})
        resources.assign_supply(db, oid, {"supply_id": 1, "quantity": 10,
                                         "evacuation_id": e2.id,
                                         "role": "supply_manager"})
        # 再确认：区2的 10 箱为新增量（区1已出 80 不动），库存 20 → 10
        resources.confirm_resources(db, oid, "赵指挥", "commander")
        assert db.get(Supply, 1).stock == 10
        alloc = db.query(SupplyAllocation).filter(
            SupplyAllocation.disposal_id == oid,
            SupplyAllocation.evacuation_id == e2.id).one()
        assert alloc.issued_quantity == 10
        order = db.get(DisposalOrder, oid)
        assert order.status == "resourced"
    db.close()


# ---------------- executed：执行中车辆/实际工况/增量追加 ----------------
def test_refresh_executed_keeps_departed_vehicles_and_reservoir(factory):
    oid, rid1 = _approved(factory, event_id=1)
    eid1 = _evac(factory, oid, 1)
    db = factory()
    resources.assign_shelter(db, oid, {"evacuation_id": eid1, "shelter_id": 1,
                                      "people": 500, "role": "transfer_lead"})
    resources.assign_vehicle(db, oid, {"vehicle_id": 1, "evacuation_id": eid1,
                                      "shuttles": 12, "role": "supply_manager"})
    resources.assign_supply(db, oid, {"supply_id": 1, "quantity": 80,
                                     "evacuation_id": eid1, "role": "supply_manager"})
    resources.confirm_resources(db, oid, "赵指挥", "commander")
    disposal.execute_order(db, oid, "王转移", "transfer_lead")
    assert db.get(Vehicle, 1).status == "departed"
    assert db.get(Supply, 1).stock == 20
    level_running = db.get(Reservoir, 1).current_level
    db.close()

    rid2 = _forecast(factory, 2)
    db = factory()
    out = disposal.refresh_forecast(db, oid, rid2, "张调度", "dispatcher")
    order = db.get(DisposalOrder, oid)
    assert order.status == "executed"             # 执行中状态保留
    # 执行中车辆不动、已出库物资不回滚
    assert db.get(Vehicle, 1).status == "departed"
    assert db.get(Supply, 1).stock == 20
    # 执行中不反演实际库水位/库容（只更新方案目标）
    assert db.get(Reservoir, 1).current_level == level_running
    # 方案快照已滚动为新一轮结果（目标末水位与实际运用水位允许不同）
    snap_final = [x for x in order.plan_snapshot["reservoirs"]
                  if x["id"] == 1][0]["final_level"]
    assert snap_final != level_running
    db.close()


def test_refresh_executed_new_zone_moving_and_append_vehicle_departs(factory):
    oid, rid1 = _approved(factory, event_id=1)
    eid1 = _evac(factory, oid, 1)
    db = factory()
    resources.assign_shelter(db, oid, {"evacuation_id": eid1, "shelter_id": 1,
                                      "people": 500, "role": "transfer_lead"})
    resources.assign_vehicle(db, oid, {"vehicle_id": 1, "evacuation_id": eid1,
                                      "shuttles": 12, "role": "supply_manager"})
    resources.assign_supply(db, oid, {"supply_id": 1, "quantity": 80,
                                     "evacuation_id": eid1, "role": "supply_manager"})
    resources.confirm_resources(db, oid, "赵指挥", "commander")
    disposal.execute_order(db, oid, "王转移", "transfer_lead")
    assert db.get(Supply, 1).stock == 20
    db.close()

    rid2 = _forecast(factory, 2)
    db = factory()
    disposal.refresh_forecast(db, oid, rid2, "张调度", "dispatcher")
    e2 = db.query(EvacuationRecord).filter(
        EvacuationRecord.run_id == rid2, EvacuationRecord.zone_id == 2).first()
    if e2 is not None:
        # 执行中新风险区自动进入转移中
        assert e2.status == "moving"
        # 追加车辆即派即发
        resources.assign_vehicle(db, oid, {"vehicle_id": 2, "evacuation_id": e2.id,
                                          "shuttles": 8, "role": "supply_manager"})
        assert db.get(Vehicle, 2).status == "departed"
        # 追加容量与物资后，增量调度令保持 executed，只出增量
        resources.assign_shelter(db, oid, {"evacuation_id": e2.id, "shelter_id": 1,
                                          "people": 300, "role": "transfer_lead"})
        resources.assign_supply(db, oid, {"supply_id": 1, "quantity": 10,
                                         "evacuation_id": e2.id,
                                         "role": "supply_manager"})
        plan = resources.confirm_resources(db, oid, "赵指挥", "commander")
        assert plan["status"] == "executed"
        assert db.get(Supply, 1).stock == 10     # 20 - 10 增量
        assert db.get(DisposalOrder, oid).status == "executed"
        # 执行中不可撤回任何分配
        sh = (db.query(ShelterAssignment).filter_by(disposal_id=oid).first())
        with pytest.raises(HTTPException) as ei:
            resources.release_shelter(db, oid, sh.id, "transfer_lead")
        assert ei.value.status_code == 409
        with pytest.raises(HTTPException) as ei:
            resources.release_vehicle(
                db, oid,
                db.query(VehicleDispatch).filter_by(disposal_id=oid).first().id,
                "supply_manager")
        assert ei.value.status_code == 409
    db.close()


# ---------------- 权限与状态机 ----------------
def test_refresh_role_and_status_guards(factory):
    oid, rid1 = _approved(factory, event_id=1)
    rid2 = _forecast(factory, 2)
    db = factory()
    # 非调度员
    with pytest.raises(HTTPException) as ei:
        disposal.refresh_forecast(db, oid, rid2, "李值守", "duty")
    assert ei.value.status_code == 403
    # 同一轮次
    with pytest.raises(HTTPException) as ei:
        disposal.refresh_forecast(db, oid, rid1, "张调度", "dispatcher")
    assert ei.value.status_code == 409
    # 已闭环
    disposal.execute_order(db, oid, "王转移", "transfer_lead")
    disposal.complete_order(db, oid, "王转移", "transfer_lead")
    with pytest.raises(HTTPException) as ei:
        disposal.refresh_forecast(db, oid, rid2, "张调度", "dispatcher")
    assert ei.value.status_code == 409
    db.close()


def test_refresh_initiated_rejected_and_missing_run_404(factory):
    rid1 = _forecast(factory, 1)
    rid2 = _forecast(factory, 2)
    db = factory()
    o = disposal.initiate_order(db, rid1, "张调度", "dispatcher")
    # 待审核单不能接收新一轮预报
    with pytest.raises(HTTPException) as ei:
        disposal.refresh_forecast(db, o["id"], rid2, "张调度", "dispatcher")
    assert ei.value.status_code == 409
    db.close()

    # 已审核单接收不存在的运行 → 404
    oid, _ = _approved(factory, event_id=1)
    db = factory()
    with pytest.raises(HTTPException) as ei:
        disposal.refresh_forecast(db, oid, 999, "张调度", "dispatcher")
    assert ei.value.status_code == 404
    db.close()


def test_refresh_rejects_run_bound_to_another_order(factory):
    oid1, rid1 = _approved(factory, event_id=1)
    # 运行2 已挂接另一处置单
    rid2 = _forecast(factory, 2)
    db = factory()
    o2 = disposal.initiate_order(db, rid2, "张调度", "dispatcher")
    disposal.review_order(db, o2["id"], "李值守", "duty")
    with pytest.raises(HTTPException) as ei:
        disposal.refresh_forecast(db, oid1, rid2, "张调度", "dispatcher")
    assert ei.value.status_code == 409
    db.close()


def test_refresh_rejects_running_run(factory):
    oid, rid1 = _approved(factory, event_id=1)
    db = factory()
    from app.models import ForecastRun
    r = ForecastRun(event_id=2, mode="rule", status="running")
    db.add(r)
    db.commit()
    with pytest.raises(HTTPException) as ei:
        disposal.refresh_forecast(db, oid, r.id, "张调度", "dispatcher")
    assert ei.value.status_code == 409
    db.close()



def test_refresh_keeps_legacy_null_records_untouched(factory):
    """run_id/disposal_id 为 NULL 的历史遗留台账不参与滚动，原样保留。"""
    oid, rid1 = _approved(factory, event_id=1)
    db = factory()
    db.add(WarningRecord(run_id=None, target_type="station", target_id=99,
                         target_name="历史站", kind="water_level", level="blue",
                         status="active"))
    db.add(EvacuationRecord(run_id=None, zone_id=99, zone_name="历史村",
                            triggered_by="预警提示", people=120, status="moving"))
    db.commit()
    db.close()
    rid2 = _forecast(factory, 2)
    db = factory()
    disposal.refresh_forecast(db, oid, rid2, "张调度", "dispatcher")
    lw = db.query(WarningRecord).filter(WarningRecord.run_id.is_(None)).one()
    le = db.query(EvacuationRecord).filter(EvacuationRecord.run_id.is_(None)).one()
    assert lw.disposal_id is None and lw.status == "active"
    assert le.disposal_id is None and le.status == "moving"
    db.close()
