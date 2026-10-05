"""联合防汛处置协同四态闭环测试。

覆盖：
- 发起/审核/执行/完成四态闭环与角色权限（越权、跳态 4xx）
- 审核通过回写水库工况、预警与转移台账（挂接 + 状态联动）
- 完成闭环：转移 safe / 预警 cleared，且后续预报重跑不回滚处置结果
- 一次运行一单（重复发起幂等）
- 历史运行缺方案/过程线时发起自动补算，且不污染历史台账
- run_id=NULL 历史遗留台账与处置单共存
"""
import threading

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models import (DisposalOrder, EvacuationRecord, FloodZone, ForecastRun,
                        ForecastSeries, OperationPlan, RainfallEvent, Reservoir,
                        RiverNode, RiverReach, SubBasin, WaterStation, WarningRecord)
from app.services import disposal
from app.services.forecast import run_forecast


def _seed(db):
    """自洽流域：子流域 → 水库 → 出口站（强降雨必触发预警与强制转移）。"""
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


def _full_loop(fac, rid):
    db = fac()
    o = disposal.initiate_order(db, rid, "张调度", "dispatcher")
    assert o["status"] == "initiated"
    o = disposal.review_order(db, o["id"], "李值守", "duty", opinion="同意")
    assert o["status"] == "approved"
    o = disposal.execute_order(db, o["id"], "王转移", "transfer_lead")
    assert o["status"] == "executed"
    o = disposal.complete_order(db, o["id"], "王转移", "transfer_lead")
    assert o["status"] == "completed"
    oid = o["id"]
    db.close()
    return oid


def test_full_four_state_closed_loop(factory):
    rid = _forecast(factory)
    oid = _full_loop(factory, rid)

    db = factory()
    order = db.get(DisposalOrder, oid)
    assert (order.initiated_by, order.reviewed_by, order.executed_by, order.completed_by) == \
           ("张调度", "李值守", "王转移", "王转移")
    assert order.reviewed_at and order.executed_at and order.completed_at
    assert order.plan_snapshot and order.plan_snapshot["reservoirs"]
    # 闭环：转移到位、预警销警
    assert all(e.status == "safe" for e in db.query(EvacuationRecord).all())
    assert all(w.status == "cleared" for w in db.query(WarningRecord).all())
    db.close()


def test_role_enforcement(factory):
    rid = _forecast(factory)
    db = factory()
    # 非调度员不能发起
    with pytest.raises(HTTPException) as ei:
        disposal.initiate_order(db, rid, "x", "duty")
    assert ei.value.status_code == 403
    o = disposal.initiate_order(db, rid, "张调度", "dispatcher")
    # 调度员不能审核；转移负责人不能审核
    for bad_role in ("dispatcher", "transfer_lead"):
        with pytest.raises(HTTPException) as ei:
            disposal.review_order(db, o["id"], "x", bad_role)
        assert ei.value.status_code == 403
    db.close()


def test_illegal_transitions_rejected(factory):
    rid = _forecast(factory)
    db = factory()
    o = disposal.initiate_order(db, rid, "张调度", "dispatcher")
    # 跳态：未审核直接执行 / 直接完成
    with pytest.raises(HTTPException) as ei:
        disposal.execute_order(db, o["id"], "王转移", "transfer_lead")
    assert ei.value.status_code == 409
    with pytest.raises(HTTPException) as ei:
        disposal.complete_order(db, o["id"], "王转移", "transfer_lead")
    assert ei.value.status_code == 409

    disposal.review_order(db, o["id"], "李值守", "duty")
    # 已审核不能重复审核
    with pytest.raises(HTTPException) as ei:
        disposal.review_order(db, o["id"], "李值守", "duty")
    assert ei.value.status_code == 409
    db.close()


def test_review_writes_back_reservoir_conditions(factory):
    db = factory()
    r = run_forecast(db, db.get(RainfallEvent, 1), "rule")
    rid = r["run_id"]
    res_before = db.get(Reservoir, 1)
    level_before = res_before.current_level
    storage_before = res_before.current_storage

    o = disposal.initiate_order(db, rid, "张调度", "dispatcher")
    o = disposal.review_order(db, o["id"], "李值守", "duty")

    db.expire_all()
    res = db.get(Reservoir, 1)
    snap = [x for x in o["plan"]["reservoirs"] if x["id"] == 1][0]
    assert res.current_level == snap["final_level"]
    assert res.current_storage == snap["final_storage"]
    assert (res.current_level, res.current_storage) != (level_before, storage_before)
    db.close()


def test_review_links_warning_and_evacuation_ledgers(factory):
    rid = _forecast(factory)
    db = factory()
    o = disposal.initiate_order(db, rid, "张调度", "dispatcher")
    assert o["linked_warnings"] == 0 and o["linked_evacuations"] == 0
    o = disposal.review_order(db, o["id"], "李值守", "duty")
    assert o["linked_warnings"] >= 1 and o["linked_evacuations"] == 1

    # 执行：pending 转移联动为 moving
    o = disposal.execute_order(db, o["id"], "王转移", "transfer_lead")
    evac = db.query(EvacuationRecord).filter(EvacuationRecord.run_id == rid).one()
    assert evac.status == "moving" and evac.disposal_id == o["id"]
    warn = db.query(WarningRecord).filter(WarningRecord.run_id == rid).one()
    assert warn.disposal_id == o["id"] and warn.status == "active"
    db.close()


def test_completed_disposal_not_rolled_back_by_forecast_rerun(factory):
    rid = _forecast(factory)
    oid = _full_loop(factory, rid)

    # 闭环后重跑同一预报：人工处置状态与处置单挂接均保留
    db = factory()
    run_forecast(db, db.get(RainfallEvent, 1), "natural")
    w = db.query(WarningRecord).filter(WarningRecord.run_id == rid).one()
    e = db.query(EvacuationRecord).filter(EvacuationRecord.run_id == rid).one()
    assert w.status == "cleared" and w.disposal_id == oid
    assert e.status == "safe" and e.disposal_id == oid
    # 方案按 run_id 幂等归档，仍只有一份
    assert db.query(OperationPlan).filter(OperationPlan.run_id == rid).count() == 1
    assert db.query(DisposalOrder).count() == 1
    db.close()


def test_one_order_per_run_repeat_initiate_is_idempotent(factory):
    rid = _forecast(factory)
    db = factory()
    o1 = disposal.initiate_order(db, rid, "张调度", "dispatcher")
    o2 = disposal.initiate_order(db, rid, "张调度", "dispatcher")
    assert o1["id"] == o2["id"] == o2["id"]
    assert db.query(DisposalOrder).count() == 1
    # 闭环后再发起，仍归并到同一闭环单
    disposal.review_order(db, o1["id"], "李值守", "duty")
    disposal.execute_order(db, o1["id"], "王转移", "transfer_lead")
    disposal.complete_order(db, o1["id"], "王转移", "transfer_lead")
    again = disposal.initiate_order(db, rid, "张调度", "dispatcher")
    assert again["id"] == o1["id"] and again["status"] == "completed"
    db.close()


def test_concurrent_initiate_merges_into_single_order(factory):
    rid = _forecast(factory)
    ids, errors = [], []

    def worker():
        db = factory()
        try:
            o = disposal.initiate_order(db, rid, "张调度", "dispatcher")
            ids.append(o["id"])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(set(ids)) == 1
    db = factory()
    assert db.query(DisposalOrder).count() == 1
    db.close()


def test_legacy_run_without_plan_or_series_is_backfilled(factory):
    """历史运行只有 forecast_runs 锚点：发起时补算方案/过程线，不动台账。"""
    db = factory()
    run = ForecastRun(event_id=1, mode="rule", status="done")
    db.add(run)
    db.commit()
    rid = run.id
    db.close()

    db = factory()
    o = disposal.initiate_order(db, rid, "张调度", "dispatcher")
    # 补算产物
    assert db.query(OperationPlan).filter(OperationPlan.run_id == rid).count() == 1
    assert db.query(ForecastSeries).filter(
        ForecastSeries.run_id == rid, ForecastSeries.kind == "reslevel").count() >= 1
    assert o["plan"]["mode"] == "rule" and o["plan"]["reservoirs"]
    # 台账未被补算动作污染
    assert db.query(WarningRecord).count() == 0
    assert db.query(EvacuationRecord).count() == 0
    # 补算后仍可正常走完闭环（无台账可回写也不报错）
    disposal.review_order(db, o["id"], "李值守", "duty")
    disposal.execute_order(db, o["id"], "王转移", "transfer_lead")
    done = disposal.complete_order(db, o["id"], "王转移", "transfer_lead")
    assert done["status"] == "completed"
    db.close()


def test_legacy_null_run_id_records_coexist_with_disposal(factory):
    rid = _forecast(factory)
    db = factory()
    # 历史遗留台账：无 run_id / 无 disposal_id，人工状态 moving
    db.add(WarningRecord(run_id=None, target_type="station", target_id=99,
                         target_name="历史站", kind="water_level", level="blue",
                         status="active"))
    db.add(EvacuationRecord(run_id=None, zone_id=99, zone_name="历史村",
                            triggered_by="预警提示", people=120, status="moving"))
    db.commit()

    oid = _full_loop(factory, rid)

    db.expire_all()
    legacy_w = db.query(WarningRecord).filter(WarningRecord.run_id.is_(None)).one()
    legacy_e = db.query(EvacuationRecord).filter(EvacuationRecord.run_id.is_(None)).one()
    assert legacy_w.disposal_id is None and legacy_w.status == "active"
    assert legacy_e.disposal_id is None and legacy_e.status == "moving"
    # 新台账正常挂接闭环
    new_w = db.query(WarningRecord).filter(WarningRecord.run_id == rid).one()
    new_e = db.query(EvacuationRecord).filter(EvacuationRecord.run_id == rid).one()
    assert new_w.disposal_id == oid and new_w.status == "cleared"
    assert new_e.disposal_id == oid and new_e.status == "safe"
    db.close()


def test_initiate_unknown_run_404(factory):
    db = factory()
    with pytest.raises(HTTPException) as ei:
        disposal.initiate_order(db, 999, "张调度", "dispatcher")
    assert ei.value.status_code == 404
    db.close()
