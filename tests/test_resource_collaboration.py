"""应急资源与避难点协同调度测试。

覆盖：
- 转移负责人分配避难容量（越权 403 / 超分 409 / 同点幂等更新 / 释放）
- 物资管理员分配车辆（跨处置单互斥、趟次运力）与物资（库存预占、超分）
- 指挥员确认调度令（容量/运力未覆盖 409、物资出库、转移进度回写、预警处置中）
- 执行发车 / 完成归队 / 闭环释放资源占用
- 跳过资源协同直接执行（兼容历史四态流转与历史遗留台账）
- 跨处置单容量与运力统一占用校验
"""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models import (DisposalOrder, EvacuationRecord, FloodZone, RainfallEvent,
                        Reservoir, Shelter, Supply, Vehicle, WarningRecord)
from app.services import disposal, resources
from app.services.forecast import run_forecast


def _seed(db):
    """两个风险区 + 避难点/车辆/物资的自洽流域（沿用处置协同测试的水文骨架）。"""
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
                                 "rating": [[0, 5.0], [30, 7.0], [60, 9.0], [100, 11.0]]}),
        FloodZone(id=1, name="沿岸村", node_id=3, population=500,
                  low_level=6.0, high_level=8.0),
        RainfallEvent(id=1, name="测试暴雨", duration_h=6, total_mm=300.0,
                      hyetograph=[50.0] * 6),
        Shelter(id=1, name="一中避难点", capacity=600),
        Shelter(id=2, name="二中避难点", capacity=200),
        Vehicle(id=1, plate="K001", kind="bus", seats=45, status="standby"),
        Vehicle(id=2, plate="K002", kind="bus", seats=45, status="standby"),
        Vehicle(id=3, plate="H001", kind="truck", seats=5, status="standby"),
        Supply(id=1, name="饮用水", unit="箱", stock=100, safety_stock=10),
        Supply(id=2, name="棉被", unit="床", stock=400, safety_stock=50),
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


def _approved_order(fac, mode="natural"):
    """预报 → 发起 → 审核，返回 (order_id, run_id)。"""
    db = fac()
    r = run_forecast(db, db.get(RainfallEvent, 1), mode)
    rid = r["run_id"]
    o = disposal.initiate_order(db, rid, "张调度", "dispatcher")
    o = disposal.review_order(db, o["id"], "李值守", "duty")
    oid = o["id"]
    db.close()
    return oid, rid


def _evac_id(fac, oid, zone_id=1):
    db = fac()
    ev = db.query(EvacuationRecord).filter(EvacuationRecord.disposal_id == oid,
                                           EvacuationRecord.zone_id == zone_id).one()
    eid = ev.id
    db.close()
    return eid


# ---------------- 角色与基础校验 ----------------
def test_only_transfer_lead_assigns_shelter(factory):
    oid, _ = _approved_order(factory)
    eid = _evac_id(factory, oid)
    db = factory()
    for bad in ("supply_manager", "commander", "duty"):
        with pytest.raises(HTTPException) as ei:
            resources.assign_shelter(db, oid, {"evacuation_id": eid, "shelter_id": 1,
                                               "people": 100, "role": bad})
        assert ei.value.status_code == 403
    db.close()


def test_only_supply_manager_assigns_vehicle_and_supply(factory):
    oid, _ = _approved_order(factory)
    eid = _evac_id(factory, oid)
    db = factory()
    with pytest.raises(HTTPException) as ei:
        resources.assign_vehicle(db, oid, {"vehicle_id": 1, "evacuation_id": eid,
                                           "role": "transfer_lead"})
    assert ei.value.status_code == 403
    with pytest.raises(HTTPException) as ei:
        resources.assign_supply(db, oid, {"supply_id": 1, "quantity": 10,
                                          "evacuation_id": eid,
                                          "role": "commander"})
    assert ei.value.status_code == 403
    db.close()


def test_resources_only_after_approval(factory):
    db = factory()
    r = run_forecast(db, db.get(RainfallEvent, 1), "natural")
    o = disposal.initiate_order(db, r["run_id"], "张调度", "dispatcher")
    ev = db.query(EvacuationRecord).filter(EvacuationRecord.run_id == r["run_id"]).one()
    with pytest.raises(HTTPException) as ei:
        resources.assign_shelter(db, o["id"], {"evacuation_id": ev.id, "shelter_id": 1,
                                               "people": 10, "role": "transfer_lead"})
    assert ei.value.status_code == 409
    db.close()


# ---------------- 避难点容量 ----------------
def test_shelter_over_capacity_rejected_and_idempotent_update(factory):
    oid, _ = _approved_order(factory)
    eid = _evac_id(factory, oid)
    db = factory()
    # 避难点容量 600，首次分 600 成功
    plan = resources.assign_shelter(db, oid, {"evacuation_id": eid, "shelter_id": 1,
                                              "people": 600, "role": "transfer_lead"})
    assert plan["coverage"]["shelter_seats"] == 600
    assert db.get(EvacuationRecord, eid).shelter_id is None  # 未确认调度令前不回写
    # 同点同区重复分配幂等归并为更新（下调人数）
    plan = resources.assign_shelter(db, oid, {"evacuation_id": eid, "shelter_id": 1,
                                              "people": 500, "role": "transfer_lead"})
    assert plan["coverage"]["shelter_seats"] == 500
    assert len(plan["shelters"]) == 1
    # 已占 500 后再上调到 601 → 超分
    with pytest.raises(HTTPException) as ei:
        resources.assign_shelter(db, oid, {"evacuation_id": eid, "shelter_id": 1,
                                           "people": 601, "role": "transfer_lead"})
    assert ei.value.status_code == 409
    db.close()


def test_shelter_capacity_shared_across_orders(factory):
    """两个处置单（不同运行）共享避难点容量，跨单超分拒绝。"""
    oid1, _ = _approved_order(factory, "natural")
    oid2, _ = _approved_order(factory, "rule")
    eid1 = _evac_id(factory, oid1)
    eid2 = _evac_id(factory, oid2)
    db = factory()
    resources.assign_shelter(db, oid1, {"evacuation_id": eid1, "shelter_id": 2,
                                        "people": 200, "role": "transfer_lead"})
    with pytest.raises(HTTPException) as ei:
        resources.assign_shelter(db, oid2, {"evacuation_id": eid2, "shelter_id": 2,
                                            "people": 10, "role": "transfer_lead"})
    assert ei.value.status_code == 409
    db.close()


# ---------------- 车辆与物资 ----------------
def test_vehicle_cross_order_exclusive_and_shuttles(factory):
    oid1, _ = _approved_order(factory, "natural")
    oid2, _ = _approved_order(factory, "rule")
    eid1 = _evac_id(factory, oid1)
    eid2 = _evac_id(factory, oid2)
    db = factory()
    resources.assign_vehicle(db, oid1, {"vehicle_id": 1, "evacuation_id": eid1,
                                        "shuttles": 2, "role": "supply_manager"})
    # 同一辆车不能再派给第二单
    with pytest.raises(HTTPException) as ei:
        resources.assign_vehicle(db, oid2, {"vehicle_id": 1,
                                            "evacuation_id": eid2,
                                            "role": "supply_manager"})
    assert ei.value.status_code == 409
    plan = resources.get_order_resources(db, db.get(DisposalOrder, oid1))
    assert plan["vehicles"][0]["capacity"] == 90  # 45 座 × 2 趟
    assert resources.list_vehicles(db)[0]["available"] is False
    db.close()


def test_supply_over_stock_rejected(factory):
    oid, _ = _approved_order(factory)
    eid = _evac_id(factory, oid)
    db = factory()
    resources.assign_supply(db, oid, {"supply_id": 1, "quantity": 80,
                                      "evacuation_id": eid, "role": "supply_manager"})
    # 库存 100，已预占 80，再分 30 → 超分
    with pytest.raises(HTTPException) as ei:
        resources.assign_supply(db, oid, {"supply_id": 1, "quantity": 30,
                                          "evacuation_id": None,
                                          "role": "supply_manager"})
    assert ei.value.status_code == 409
    # 可用量口径：100 - 80 = 20
    assert resources.list_supplies(db)[0]["available"] == 20
    db.close()


# ---------------- 指挥员确认调度令 ----------------
def test_confirm_resources_requires_full_coverage(factory):
    oid, _ = _approved_order(factory)
    eid = _evac_id(factory, oid)
    db = factory()
    # 容量只分 400/500，运力未配 → 容量缺口先报 409
    resources.assign_shelter(db, oid, {"evacuation_id": eid, "shelter_id": 1,
                                       "people": 400, "role": "transfer_lead"})
    with pytest.raises(HTTPException) as ei:
        resources.confirm_resources(db, oid, "赵指挥", "commander")
    assert ei.value.status_code == 409 and "避难点容量" in ei.value.detail

    resources.assign_shelter(db, oid, {"evacuation_id": eid, "shelter_id": 1,
                                       "people": 500, "role": "transfer_lead"})
    # 容量齐了但运力 0 → 运力缺口 409
    with pytest.raises(HTTPException) as ei:
        resources.confirm_resources(db, oid, "赵指挥", "commander")
    assert ei.value.status_code == 409 and "车辆运力" in ei.value.detail
    db.close()


def test_only_commander_confirms(factory):
    oid, _ = _approved_order(factory)
    db = factory()
    with pytest.raises(HTTPException) as ei:
        resources.confirm_resources(db, oid, "王转移", "transfer_lead")
    assert ei.value.status_code == 403
    db.close()


def test_confirm_writes_back_progress_warning_and_stock(factory):
    oid, rid = _approved_order(factory)
    eid = _evac_id(factory, oid)
    db = factory()
    resources.assign_shelter(db, oid, {"evacuation_id": eid, "shelter_id": 1,
                                       "people": 500, "role": "transfer_lead",
                                       "operator": "王转移"})
    # 两辆车各 6 趟 = 540 座，覆盖 500 人
    resources.assign_vehicle(db, oid, {"vehicle_id": 1, "evacuation_id": eid,
                                       "shuttles": 6, "role": "supply_manager"})
    resources.assign_vehicle(db, oid, {"vehicle_id": 2, "evacuation_id": eid,
                                       "shuttles": 6, "role": "supply_manager"})
    resources.assign_supply(db, oid, {"supply_id": 1, "quantity": 80,
                                      "evacuation_id": eid, "role": "supply_manager"})
    plan = resources.confirm_resources(db, oid, "赵指挥", "commander",
                                       order_text="按令转移，物资随车下发")
    assert plan["status"] == "resourced"

    db.expire_all()
    order = db.get(DisposalOrder, oid)
    assert order.status == "resourced" and order.resourced_by == "赵指挥"
    # 转移进度回写避难点
    evac = db.get(EvacuationRecord, eid)
    assert evac.shelter_id == 1 and evac.shelter_name == "一中避难点"
    # 风险预警进入处置中
    warns = db.query(WarningRecord).filter(WarningRecord.run_id == rid).all()
    assert warns and all(w.status == "handling" for w in warns)
    # 物资实际出库：100 - 80 = 20
    assert db.get(Supply, 1).stock == 20
    # 车辆标记已派出
    assert db.get(Vehicle, 1).status == "dispatched"
    db.close()


def test_confirm_resources_idempotent_no_double_stock_deduction(factory):
    oid, _ = _approved_order(factory)
    eid = _evac_id(factory, oid)
    db = factory()
    resources.assign_shelter(db, oid, {"evacuation_id": eid, "shelter_id": 1,
                                       "people": 500, "role": "transfer_lead"})
    resources.assign_vehicle(db, oid, {"vehicle_id": 1, "evacuation_id": eid,
                                       "shuttles": 12, "role": "supply_manager"})
    resources.assign_supply(db, oid, {"supply_id": 1, "quantity": 80,
                                      "evacuation_id": eid, "role": "supply_manager"})
    resources.confirm_resources(db, oid, "赵指挥", "commander")
    # 重复确认（同状态 resourced）幂等，不重复扣库存
    resources.confirm_resources(db, oid, "赵指挥", "commander")
    assert db.get(Supply, 1).stock == 20

    # 调度令后追加分配物资2：未出库（issued=0）；物资1 追加只出增量
    resources.assign_supply(db, oid, {"supply_id": 2, "quantity": 100,
                                      "evacuation_id": None,
                                      "role": "supply_manager"})
    plan = resources.get_order_resources(db, db.get(DisposalOrder, oid))
    s2 = [x for x in plan["supplies"] if x["supply_id"] == 2][0]
    assert s2["issued_quantity"] == 0 and s2["issued"] is False
    # 物资1 再追加 10（已出库 80）：等效只需再出 10
    resources.assign_supply(db, oid, {"supply_id": 1, "quantity": 90,
                                      "evacuation_id": eid, "role": "supply_manager"})
    resources.confirm_resources(db, oid, "赵指挥", "commander")
    assert db.get(Supply, 1).stock == 10  # 20 - 10 增量
    assert resources.list_supplies(db)[0]["committed"] == 0
    assert resources.list_supplies(db)[0]["available"] == 10
    db.close()


# ---------------- 执行 / 完成联动 ----------------
def test_execute_departs_vehicles_and_complete_returns(factory):
    oid, rid = _approved_order(factory)
    eid = _evac_id(factory, oid)
    db = factory()
    resources.assign_shelter(db, oid, {"evacuation_id": eid, "shelter_id": 1,
                                       "people": 500, "role": "transfer_lead"})
    resources.assign_vehicle(db, oid, {"vehicle_id": 1, "evacuation_id": eid,
                                       "shuttles": 12, "role": "supply_manager"})
    resources.confirm_resources(db, oid, "赵指挥", "commander")
    o = disposal.execute_order(db, oid, "王转移", "transfer_lead")
    assert o["status"] == "executed"
    assert db.get(Vehicle, 1).status == "departed"
    assert db.get(EvacuationRecord, eid).status == "moving"

    o = disposal.complete_order(db, oid, "王转移", "transfer_lead")
    assert o["status"] == "completed"
    assert db.get(Vehicle, 1).status == "returned"
    assert all(w.status == "cleared"
               for w in db.query(WarningRecord).filter(WarningRecord.run_id == rid))
    db.close()


def test_completed_order_releases_resource_occupancy(factory):
    """闭环后避难点/车辆占用释放，可被新处置单再次使用。"""
    oid1, _ = _approved_order(factory, "natural")
    oid2, _ = _approved_order(factory, "rule")
    eid1 = _evac_id(factory, oid1)
    eid2 = _evac_id(factory, oid2)
    db = factory()
    resources.assign_shelter(db, oid1, {"evacuation_id": eid1, "shelter_id": 1,
                                        "people": 500, "role": "transfer_lead"})
    resources.assign_vehicle(db, oid1, {"vehicle_id": 1, "evacuation_id": eid1,
                                        "shuttles": 12, "role": "supply_manager"})
    resources.confirm_resources(db, oid1, "赵指挥", "commander")
    disposal.execute_order(db, oid1, "王转移", "transfer_lead")
    disposal.complete_order(db, oid1, "王转移", "transfer_lead")

    # 容量/运力释放：第二单可复用避难点 1 与车辆 1
    plan = resources.assign_shelter(db, oid2, {"evacuation_id": eid2, "shelter_id": 1,
                                               "people": 500, "role": "transfer_lead"})
    assert plan["coverage"]["shelter_seats"] == 500
    resources.assign_vehicle(db, oid2, {"vehicle_id": 1, "evacuation_id": eid2,
                                        "shuttles": 12, "role": "supply_manager"})
    db.close()


def test_skip_resource_collaboration_keeps_legacy_flow(factory):
    """approved 直接执行（无资源分配/无调度令）仍可闭环：兼容历史四态流转。"""
    oid, rid = _approved_order(factory)
    db = factory()
    o = disposal.execute_order(db, oid, "王转移", "transfer_lead")
    assert o["status"] == "executed"
    assert o["resource_summary"]["shelter_seats"] == 0
    evac = db.query(EvacuationRecord).filter(EvacuationRecord.run_id == rid).one()
    assert evac.shelter_id is None and evac.status == "moving"
    warn = db.query(WarningRecord).filter(WarningRecord.run_id == rid).one()
    assert warn.status == "active"  # 未经指挥员调度令，预警不进入处置中
    disposal.complete_order(db, oid, "王转移", "transfer_lead")
    db.close()


def test_warning_handling_not_reset_by_forecast_rerun(factory):
    """预报重跑不覆盖资源协同写入的人工/联动处置状态（handling 保持）。"""
    oid, rid = _approved_order(factory)
    eid = _evac_id(factory, oid)
    db = factory()
    resources.assign_shelter(db, oid, {"evacuation_id": eid, "shelter_id": 1,
                                       "people": 500, "role": "transfer_lead"})
    resources.assign_vehicle(db, oid, {"vehicle_id": 1, "evacuation_id": eid,
                                       "shuttles": 12, "role": "supply_manager"})
    resources.confirm_resources(db, oid, "赵指挥", "commander")
    db.close()

    db = factory()
    run_forecast(db, db.get(RainfallEvent, 1), "natural")
    w = db.query(WarningRecord).filter(WarningRecord.run_id == rid).one()
    e = db.query(EvacuationRecord).filter(EvacuationRecord.run_id == rid).one()
    assert w.status == "handling" and w.disposal_id == oid
    assert e.shelter_id == 1 and e.shelter_name == "一中避难点"
    db.close()
