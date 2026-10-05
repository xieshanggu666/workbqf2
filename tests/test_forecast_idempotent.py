"""预报运行与处置记录的幂等关联测试。

覆盖：重复执行幂等 / 并发触发归并 / 异常重试接续 / 人工处置状态保留 /
历史遗留记录（run_id=NULL）共存。
"""
import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models import (EvacuationRecord, FloodZone, ForecastRun, ForecastSeries,
                        RainfallEvent, RiverNode, RiverReach, SubBasin,
                        WaterStation, WarningRecord)
from app.services import forecast as forecast_svc
from app.services.forecast import run_forecast


def _seed(db):
    """最小自洽流域：1 子流域 → 1 河段 → 出口站（阈值必触发红色预警与强制转移）。"""
    db.add_all([
        RiverNode(id=1, name="源头", kind="headwater"),
        RiverNode(id=2, name="出口", kind="outlet"),
        RiverReach(id=1, name="河段", from_node_id=1, to_node_id=2,
                   k_hr=1.0, x_coef=0.2),
        SubBasin(id=1, name="子流域", area_km2=120.0, cn=88.0, lag_hr=1.0,
                 outlet_node_id=1),
        WaterStation(id=1, name="出口水位站", node_id=2,
                     thresholds={"base_level": 10.0, "blue": 11.0, "yellow": 12.0,
                                 "orange": 13.0, "red": 14.0,
                                 "rating": [[0, 10.0], [10, 11.0], [50, 13.0], [100, 15.0]]}),
        FloodZone(id=1, name="沿岸村", node_id=2, population=500,
                  low_level=11.0, high_level=13.0),
        RainfallEvent(id=1, name="测试暴雨", duration_h=6, total_mm=300.0,
                      hyetograph=[50.0] * 6),
    ])
    db.commit()


@pytest.fixture()
def session_factory(tmp_path):
    """文件型 SQLite（tmp_path）：多连接并发行为与生产库一致。"""
    engine = create_engine(f"sqlite:///{tmp_path}/test.db",
                           connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    _seed(db)
    db.close()
    yield factory
    engine.dispose()


def _counts(db):
    return (db.query(ForecastRun).count(),
            db.query(WarningRecord).count(),
            db.query(EvacuationRecord).count(),
            db.query(ForecastSeries).count())


def test_repeat_execution_is_idempotent(session_factory):
    db = session_factory()
    r1 = run_forecast(db, db.get(RainfallEvent, 1), "natural")
    assert r1["run_status"] == "done"
    base = _counts(db)
    assert base[0] == 1 and base[1] == 1 and base[2] == 1 and base[3] > 0

    # 重复执行同一预报：运行归并、台账与过程线数量不变
    for _ in range(3):
        r = run_forecast(db, db.get(RainfallEvent, 1), "natural")
        assert r["run_id"] == r1["run_id"]
    assert _counts(db) == base
    assert db.get(ForecastRun, r1["run_id"]).status == "done"
    db.close()


def test_different_modes_are_separate_runs(session_factory):
    db = session_factory()
    r_nat = run_forecast(db, db.get(RainfallEvent, 1), "natural")
    r_opt = run_forecast(db, db.get(RainfallEvent, 1), "optimized")
    assert r_nat["run_id"] != r_opt["run_id"]
    assert db.query(ForecastRun).count() == 2
    # 各次运行独立持有自己的处置记录
    assert db.query(WarningRecord).count() == 2
    run_ids = {w.run_id for w in db.query(WarningRecord).all()}
    assert run_ids == {r_nat["run_id"], r_opt["run_id"]}
    db.close()


def test_persist_false_writes_nothing(session_factory):
    db = session_factory()
    r = run_forecast(db, db.get(RainfallEvent, 1), "natural", persist=False)
    assert r["run_id"] == 0
    assert _counts(db) == (0, 0, 0, 0)
    db.close()


def test_concurrent_triggers_merge_into_single_run(session_factory):
    errors = []

    def worker():
        db = session_factory()
        try:
            run_forecast(db, db.get(RainfallEvent, 1), "natural")
        except Exception as exc:  # noqa: BLE001 - 收集线程内异常统一断言
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    db = session_factory()
    assert db.query(ForecastRun).count() == 1
    assert db.query(WarningRecord).count() == 1
    assert db.query(EvacuationRecord).count() == 1
    db.close()


def test_concurrent_get_or_create_race_resolved_by_unique_key(session_factory):
    """绕开进程内锁直接并发争抢幂等键：唯一约束兜底，全部归并到同一运行。"""
    barrier = threading.Barrier(6)
    run_ids = []
    errors = []

    def worker():
        db = session_factory()
        try:
            barrier.wait(timeout=10)
            run = forecast_svc._get_or_create_run(db, 1, "natural")
            run_ids.append(run.id)
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
    assert len(set(run_ids)) == 1
    db = session_factory()
    assert db.query(ForecastRun).count() == 1
    db.close()


def test_retry_after_failure_heals_without_duplicates(session_factory, monkeypatch):
    db = session_factory()
    original = forecast_svc._upsert_evacuation

    def boom(*args, **kwargs):
        raise RuntimeError("模拟处置写库失败")

    monkeypatch.setattr(forecast_svc, "_upsert_evacuation", boom)
    with pytest.raises(RuntimeError):
        run_forecast(db, db.get(RainfallEvent, 1), "natural")

    # 失败运行已标记；派生数据整体回滚，无半截台账残留
    run = db.query(ForecastRun).one()
    assert run.status == "failed"
    assert db.query(WarningRecord).count() == 0
    assert db.query(EvacuationRecord).count() == 0

    # 异常重试：归并到同一运行并补齐全部记录
    monkeypatch.setattr(forecast_svc, "_upsert_evacuation", original)
    r = run_forecast(db, db.get(RainfallEvent, 1), "natural")
    assert r["run_id"] == run.id
    assert db.get(ForecastRun, run.id).status == "done"
    assert _counts(db)[0] == 1
    assert db.query(WarningRecord).count() == 1
    assert db.query(EvacuationRecord).count() == 1
    assert db.query(ForecastSeries).count() > 0
    db.close()


def test_manual_disposition_status_preserved_on_rerun(session_factory):
    db = session_factory()
    run_forecast(db, db.get(RainfallEvent, 1), "natural")
    warn = db.query(WarningRecord).one()
    evac = db.query(EvacuationRecord).one()
    warn.status = "cleared"      # 人工销警
    evac.status = "safe"         # 人工确认转移完成
    db.commit()

    run_forecast(db, db.get(RainfallEvent, 1), "natural")
    db.expire_all()
    assert db.query(WarningRecord).one().status == "cleared"
    assert db.query(EvacuationRecord).one().status == "safe"
    assert db.query(WarningRecord).count() == 1
    assert db.query(EvacuationRecord).count() == 1
    db.close()


def test_legacy_records_without_run_id_coexist(session_factory):
    db = session_factory()
    # 历史遗留记录：无 run_id，含人工处置状态
    db.add(WarningRecord(run_id=None, target_type="station", target_id=1,
                         target_name="出口水位站", kind="water_level", level="red",
                         value=15.0, threshold=14.0, message="历史遗留预警",
                         status="cleared"))
    db.add(EvacuationRecord(run_id=None, zone_id=1, zone_name="沿岸村",
                            triggered_by="强制转移", people=500, status="moving"))
    db.commit()

    run_forecast(db, db.get(RainfallEvent, 1), "natural")
    run_forecast(db, db.get(RainfallEvent, 1), "natural")

    # 历史记录原样保留（NULL 不参与唯一约束），新记录不重复
    assert db.query(WarningRecord).count() == 2
    assert db.query(EvacuationRecord).count() == 2
    legacy_warn = db.query(WarningRecord).filter(WarningRecord.run_id.is_(None)).one()
    legacy_evac = db.query(EvacuationRecord).filter(EvacuationRecord.run_id.is_(None)).one()
    assert legacy_warn.status == "cleared"
    assert legacy_evac.status == "moving"
    # 新记录均挂到同一次运行
    run = db.query(ForecastRun).one()
    new_warn = db.query(WarningRecord).filter(WarningRecord.run_id.isnot(None)).one()
    new_evac = db.query(EvacuationRecord).filter(EvacuationRecord.run_id.isnot(None)).one()
    assert new_warn.run_id == run.id == new_evac.run_id
    db.close()
