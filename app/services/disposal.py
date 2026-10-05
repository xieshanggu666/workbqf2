"""联合防汛处置协同：围绕预报运行的多态闭环 + 审核方案回写。

角色与状态机
    调度员 initiate        → initiated（待审核）
    预警值守 review         → approved（审核通过，同步回写水库工况/预警/转移台账）
    转移负责人分配避难点 / 物资管理员分配车辆物资 / 指挥员确认资源调度令
                           → resourced（资源已调度，回写转移进度与风险预警；可选环节）
    转移负责人 execute      → executed（执行中，联动转移台账进入转移中、车辆发车）
    转移负责人 complete     → completed（闭环：转移全部到位、预警销警、车辆归队）

每次预报运行 (run_id) 至多发起一单；重复发起返回已存在的处置单。
资源协同为可选环节：approved 与 resourced 均可启动执行，兼容历史四态流转。
历史预报运行（早期库无调度方案/水库过程线）在发起时自动以
write_ledgers=False 重算补齐方案快照所需数据，不改动任何历史台账，
run_id 为 NULL 的历史遗留预警/转移记录原样保留。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Dict

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import (DisposalOrder, EvacuationRecord, ForecastRun, ForecastSeries,
                        OperationPlan, RainfallEvent, Reservoir, Vehicle,
                        VehicleDispatch, WarningRecord)
from app.services import resources as resource_svc
from app.services.forecast import run_forecast

# 状态 → 下一状态、可操作角色、操作人字段、落库时间字段
TRANSITIONS = {
    "review": {"from": "initiated", "to": "approved", "roles": ("duty",),
               "actor": "reviewed_by", "at": "reviewed_at"},
    "execute": {"from": ("approved", "resourced"), "to": "executed",
                "roles": ("transfer_lead",),
                "actor": "executed_by", "at": "executed_at"},
    "complete": {"from": "executed", "to": "completed", "roles": ("transfer_lead",),
                 "actor": "completed_by", "at": "completed_at"},
}
STATUS_TEXT = {"initiated": "待审核", "approved": "待执行",
               "resourced": "资源已调度", "executed": "执行中", "completed": "已完成"}
ROLE_TEXT = {"dispatcher": "调度员", "duty": "预警值守", "transfer_lead": "转移负责人",
             "supply_manager": "物资管理员", "commander": "指挥员"}
MODE_TEXT = {"natural": "天然过流", "rule": "规则调度", "optimized": "联合优化调度"}

_order_locks_guard = threading.Lock()
_order_locks: Dict[int, threading.Lock] = {}


def _order_lock(run_id: int) -> threading.Lock:
    """同一预报运行的处置操作串行化，避免并发审核/执行造成回写错乱。"""
    with _order_locks_guard:
        return _order_locks.setdefault(run_id, threading.Lock())


def _ensure_artifacts(db: Session, run: ForecastRun) -> tuple:
    """确保历史运行具备审核所需的方案与过程线；缺失则只补算派生数据。

    返回 (event, plan)。补算以 write_ledgers=False 执行，仅重建过程线与
    调度方案，不触碰预警/转移台账及其人工处置状态。
    """
    plan = db.query(OperationPlan).filter(OperationPlan.run_id == run.id).first()
    n_level = (db.query(ForecastSeries)
               .filter(ForecastSeries.run_id == run.id,
                       ForecastSeries.kind == "reslevel").count())
    if plan is not None and n_level > 0:
        event = db.get(RainfallEvent, run.event_id)
        return event, plan

    event = db.get(RainfallEvent, run.event_id)
    if event is None:
        raise HTTPException(409, f"预报运行 #{run.id} 对应的降雨情景已不存在，无法补齐方案")
    # 历史运行补算：复用同一幂等运行，只写过程线 + 方案，不动台账
    run_forecast(db, event, reservoir_rule=run.mode, persist=True, write_ledgers=False)
    db.expire_all()
    plan = db.query(OperationPlan).filter(OperationPlan.run_id == run.id).first()
    return event, plan


def _build_plan_snapshot(db: Session, run: ForecastRun, plan: OperationPlan) -> dict:
    """从方案表与水库过程线组装审核归档快照（审核后回写台账的依据）。"""
    level_rows = (db.query(ForecastSeries)
                  .filter(ForecastSeries.run_id == run.id,
                          ForecastSeries.kind == "reslevel").all())
    reservoirs = []
    for row in level_rows:
        res = db.query(Reservoir).filter(Reservoir.node_id == row.node_id).first()
        oc = (plan.reservoir_outcome or {}).get(str(res.id), {}) if res else {}
        vals = row.values or []
        reservoirs.append({
            "id": res.id if res else 0,
            "name": row.name,
            "current_level": round(res.current_level, 2) if res and res.current_level else None,
            "current_storage": round(res.current_storage, 1) if res and res.current_storage else None,
            "peak_level": oc.get("peak_level", round(max(vals), 2) if vals else 0.0),
            "final_level": oc.get("final_level", round(vals[-1], 2) if vals else 0.0),
            "final_storage": oc.get("final_storage", 0.0),
            "peak_outflow": oc.get("peak_outflow", 0.0),
            "storage_gain": oc.get("storage_gain", 0.0),
        })
    return {
        "mode": run.mode,
        "mode_text": MODE_TEXT.get(run.mode, run.mode),
        "plan_id": plan.id,
        "plan_name": plan.name,
        "objective": plan.objective,
        "peak_flow": plan.peak_flow,
        "peak_ratio": plan.peak_ratio,
        "storage_gain": plan.storage_gain,
        "gate_schedule": plan.gate_schedule or {},
        "reservoirs": reservoirs,
    }


def serialize_order(db: Session, order: DisposalOrder) -> dict:
    """处置单序列化（含运行/情景冗余信息，便于列表与详情直接展示）。"""
    run = db.get(ForecastRun, order.run_id)
    event = db.get(RainfallEvent, run.event_id) if run else None
    linked_warnings = db.query(WarningRecord).filter(
        WarningRecord.disposal_id == order.id).count()
    linked_evacs = db.query(EvacuationRecord).filter(
        EvacuationRecord.disposal_id == order.id).count()
    resources = resource_svc.get_order_resources(db, order)
    cov = resources["coverage"]
    return {
        "id": order.id,
        "run_id": order.run_id,
        "event_id": run.event_id if run else None,
        "event_name": event.name if event else "（情景已删除）",
        "mode": run.mode if run else "",
        "mode_text": MODE_TEXT.get(run.mode, run.mode) if run else "",
        "run_status": run.status if run else "",
        "title": order.title,
        "status": order.status,
        "status_text": STATUS_TEXT.get(order.status, order.status),
        "remark": order.remark,
        "plan": order.plan_snapshot or None,
        "linked_warnings": linked_warnings,
        "linked_evacuations": linked_evacs,
        "resourced_by": order.resourced_by,
        "resourced_at": order.resourced_at.isoformat() if order.resourced_at else None,
        "resources": resources,
        "resource_summary": {
            "shelter_seats": cov["shelter_seats"],
            "vehicle_seats": cov["vehicle_seats"],
            "supply_kinds": cov["supply_kinds"],
            "ready": cov["ready"],
        },
        "initiated_by": order.initiated_by,
        "reviewed_by": order.reviewed_by,
        "executed_by": order.executed_by,
        "completed_by": order.completed_by,
        "initiated_at": order.initiated_at.isoformat() if order.initiated_at else None,
        "reviewed_at": order.reviewed_at.isoformat() if order.reviewed_at else None,
        "executed_at": order.executed_at.isoformat() if order.executed_at else None,
        "completed_at": order.completed_at.isoformat() if order.completed_at else None,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


def initiate_order(db: Session, run_id: int, operator: str, role: str,
                   title: str = "", remark: str = "") -> dict:
    """调度员围绕一次预报运行发起联合防汛处置单（按 run_id 幂等）。"""
    if role != "dispatcher":
        raise HTTPException(403, f"仅调度员可发起处置单（当前角色：{ROLE_TEXT.get(role, role)}）")
    run = db.get(ForecastRun, run_id)
    if run is None:
        raise HTTPException(404, f"预报运行 #{run_id} 不存在")

    with _order_lock(run_id):
        existing = db.query(DisposalOrder).filter(DisposalOrder.run_id == run_id).first()
        if existing is not None:
            return serialize_order(db, existing)  # 重复发起：归并到同一处置单

        # 历史运行可能缺方案/过程线，先补算（不动台账）再组快照
        event, plan = _ensure_artifacts(db, run)
        snapshot = _build_plan_snapshot(db, run, plan)
        name = event.name if event else f"运行#{run_id}"
        order = DisposalOrder(
            run_id=run_id,
            title=title.strip() or f"{name} · {snapshot['mode_text']}联合防汛处置单",
            remark=(remark or "").strip(),
            plan_snapshot=snapshot,
            initiated_by=operator.strip() or "值班调度员",
            status="initiated")
        db.add(order)
        db.commit()
        db.refresh(order)
        return serialize_order(db, order)


def _get_order_for(db: Session, order_id: int, action: str) -> DisposalOrder:
    order = db.get(DisposalOrder, order_id)
    if order is None:
        raise HTTPException(404, f"处置单 #{order_id} 不存在")
    rule = TRANSITIONS[action]
    allowed = rule["from"]
    if isinstance(allowed, str):
        allowed = (allowed,)
    if order.status not in allowed:
        need = "、".join(STATUS_TEXT[s] for s in allowed)
        raise HTTPException(
            409, f"处置单当前为「{STATUS_TEXT.get(order.status, order.status)}」，"
                 f"不能执行该操作（须为「{need}」）")
    return order


def _check_role(role: str, action: str) -> None:
    roles = TRANSITIONS[action]["roles"]
    if role not in roles:
        need = "、".join(ROLE_TEXT[r] for r in roles)
        raise HTTPException(403, f"该操作需由{need}执行（当前角色：{ROLE_TEXT.get(role, role)}）")


def review_order(db: Session, order_id: int, operator: str, role: str,
                 opinion: str = "") -> dict:
    """预警值守审核：通过即把调度方案回写水库工况、预警与转移台账。"""
    _check_role(role, "review")
    order = _get_order_for(db, order_id, "review")

    with _order_lock(order.run_id):
        run = db.get(ForecastRun, order.run_id)
        event, plan = _ensure_artifacts(db, run)
        snapshot = _build_plan_snapshot(db, run, plan)
        order.plan_snapshot = snapshot

        # ---- 回写 1：水库工况更新为方案执行后的水位/库容 ----
        for item in snapshot["reservoirs"]:
            res = db.get(Reservoir, item["id"]) if item["id"] else None
            if res is not None:
                res.current_level = item["final_level"]
                res.current_storage = item["final_storage"]

        # ---- 回写 2：本次运行预警台账挂接到处置单（人工已销警的保持原状）----
        warnings = (db.query(WarningRecord)
                    .filter(WarningRecord.run_id == order.run_id).all())
        for w in warnings:
            w.disposal_id = order.id

        # ---- 回写 3：转移台账挂接到处置单；未开始处置的进入待执行联动 ----
        evacs = (db.query(EvacuationRecord)
                 .filter(EvacuationRecord.run_id == order.run_id).all())
        for ev in evacs:
            ev.disposal_id = order.id

        order.status = "approved"
        order.reviewed_by = operator.strip() or "预警值守员"
        order.reviewed_at = datetime.now()
        if opinion.strip():
            order.remark = (order.remark + f"\n[审核意见] {opinion.strip()}").strip()
        db.commit()
        db.refresh(order)
        return serialize_order(db, order)


def execute_order(db: Session, order_id: int, operator: str, role: str,
                  note: str = "") -> dict:
    """转移负责人启动执行：联动转移台账由待转移转入转移中。"""
    _check_role(role, "execute")
    order = _get_order_for(db, order_id, "execute")

    with _order_lock(order.run_id):
        evacs = (db.query(EvacuationRecord)
                 .filter(EvacuationRecord.disposal_id == order.id).all())
        for ev in evacs:
            if ev.status == "pending":
                ev.status = "moving"

        # 已确认资源调度令时车辆发车（未做资源协同的处置单无车可发）
        if order.status == "resourced":
            dispatches = (db.query(VehicleDispatch)
                          .filter(VehicleDispatch.disposal_id == order.id).all())
            for d in dispatches:
                vehicle = db.get(Vehicle, d.vehicle_id)
                if vehicle is not None and vehicle.status != "departed":
                    vehicle.status = "departed"

        order.status = "executed"
        order.executed_by = operator.strip() or "转移负责人"
        order.executed_at = datetime.now()
        if note.strip():
            order.remark = (order.remark + f"\n[执行说明] {note.strip()}").strip()
        db.commit()
        db.refresh(order)
        return serialize_order(db, order)


def complete_order(db: Session, order_id: int, operator: str, role: str,
                  summary: str = "") -> dict:
    """转移负责人确认闭环：转移全部到位（safe）、关联预警销警（cleared）、车辆归队。"""
    _check_role(role, "complete")
    order = _get_order_for(db, order_id, "complete")

    with _order_lock(order.run_id):
        evacs = (db.query(EvacuationRecord)
                 .filter(EvacuationRecord.disposal_id == order.id).all())
        for ev in evacs:
            ev.status = "safe"
        warnings = (db.query(WarningRecord)
                    .filter(WarningRecord.disposal_id == order.id).all())
        for w in warnings:
            w.status = "cleared"

        # 车辆归队（处置单占用随之释放，可供其它处置单再派）
        dispatches = (db.query(VehicleDispatch)
                      .filter(VehicleDispatch.disposal_id == order.id).all())
        for d in dispatches:
            vehicle = db.get(Vehicle, d.vehicle_id)
            if vehicle is not None:
                vehicle.status = "returned" if vehicle.status == "departed" else "standby"

        order.status = "completed"
        order.completed_by = operator.strip() or "转移负责人"
        order.completed_at = datetime.now()
        if summary.strip():
            order.remark = (order.remark + f"\n[完成小结] {summary.strip()}").strip()
        db.commit()
        db.refresh(order)
        return serialize_order(db, order)


def list_orders(db: Session, limit: int = 50) -> list:
    rows = (db.query(DisposalOrder)
            .order_by(DisposalOrder.id.desc()).limit(limit).all())
    return [serialize_order(db, o) for o in rows]


def get_order(db: Session, order_id: int) -> dict:
    order = db.get(DisposalOrder, order_id)
    if order is None:
        raise HTTPException(404, f"处置单 #{order_id} 不存在")
    return serialize_order(db, order)
