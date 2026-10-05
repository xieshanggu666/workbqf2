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
已审核处置单（approved/resourced/executed）可由调度员滚动接收新一轮预报
(refresh_forecast)：方案快照按轮次演进，预警/转移按目标身份迁移并保留
人工状态，资源占用增量调整、兼容已出库物资与执行中车辆；闭环后不再滚动。
历史预报运行（早期库无调度方案/水库过程线）在发起时自动以
write_ledgers=False 重算补齐方案快照所需数据，不改动任何历史台账，
run_id 为 NULL 的历史遗留预警/转移记录原样保留。
"""
from __future__ import annotations

import threading
from contextlib import ExitStack
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


# 处置单维度串行锁（跨运行的操作，如接收新一轮预报会更换 run_id）
_order_id_locks: Dict[int, threading.Lock] = {}
_order_id_locks_guard = threading.Lock()


def _order_id_lock(order_id: int) -> threading.Lock:
    with _order_id_locks_guard:
        return _order_id_locks.setdefault(order_id, threading.Lock())


# 已审核、可接收新一轮预报的处置单状态（闭环单不再滚动）
REFRESHABLE_STATUSES = ("approved", "resourced", "executed")


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


def _stamp_round(db: Session, order: DisposalOrder, snapshot: dict,
                 run: ForecastRun, rolling: bool = False) -> dict:
    """给方案快照标注预报轮次；接收新一轮预报时追加轮次历史并滚动 run_id。

    快照始终对应当前绑定运行（order.run_id）；rounds 保留各轮关键指标，
    供详情展示方案演进。首次发起/审核为第 1 轮。
    """
    prev = order.plan_snapshot or {}
    round_no = int(prev.get("round", 1) or 1)
    if rolling:
        round_no += 1
        rounds = list(prev.get("rounds") or [])
        rounds.append({
            "round": prev.get("round", 1),
            "run_id": prev.get("run_id", order.run_id),
            "event_name": prev.get("event_name"),
            "mode": prev.get("mode"),
            "peak_flow": prev.get("peak_flow"),
            "peak_ratio": prev.get("peak_ratio"),
            "storage_gain": prev.get("storage_gain"),
        })
        snapshot["rounds"] = rounds
    else:
        snapshot["rounds"] = list(prev.get("rounds") or [])
    snapshot["round"] = round_no
    snapshot["run_id"] = run.id
    event = db.get(RainfallEvent, run.event_id)
    snapshot["event_name"] = event.name if event else prev.get("event_name")
    return snapshot


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
        "round": (order.plan_snapshot or {}).get("round", 1),
        "last_refresh": (order.plan_snapshot or {}).get("last_refresh"),
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
        snapshot = _stamp_round(db, DisposalOrder(run_id=run_id),
                                _build_plan_snapshot(db, run, plan), run)
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
        snapshot = _stamp_round(db, order, _build_plan_snapshot(db, run, plan), run)
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


def refresh_forecast(db: Session, order_id: int, new_run_id: int,
                     operator: str, role: str, note: str = "") -> dict:
    """已审核处置单接收新一轮预报：滚动绑定新运行，方案/预警/转移/资源增量调整。

    语义（全部在单事务内完成）：
      - 仅 approved/resourced/executed 单可接收（initiated 可直接重新发起；
        completed 已闭环不再滚动）；人工状态（处置单状态、预警 active/handling/
        cleared、转移 pending/moving/safe、避难点挂接、电子签名）全部保留；
      - 方案快照滚动为新一轮方案，水库工况仅在尚未实际执行（approved/
        resourced）时按新方案末态回写；executed 执行中只更新方案目标，
        不反演正在运用的库水位/库容；
      - 预警按「站点+类型」身份迁移到新运行（等级/峰值/文案刷新，状态保留），
        新出现的站点预警增量挂接，新一轮不再预警的旧站点保留不删；
      - 转移按「风险区」身份迁移（pending 刷新触发信息与人数，moving/safe
        保持），新增风险区增量挂接；分配记录（避难点/车辆/物资）以转移 id
        为键，迁移后天然沿用，已出库物资不回滚、执行中车辆不动；
      - 新风险区形成新的容量/运力缺口，由各岗位追加分配，指挥员再确认
        调度令时仅出增量（executed 单追加车辆即派即发）。
    """
    if role != "dispatcher":
        raise HTTPException(403, f"仅调度员可下发新一轮预报（当前角色："
                                f"{ROLE_TEXT.get(role, role)}）")
    order = db.get(DisposalOrder, order_id)
    if order is None:
        raise HTTPException(404, f"处置单 #{order_id} 不存在")
    if order.status not in REFRESHABLE_STATUSES:
        raise HTTPException(
            409, f"处置单当前为「{STATUS_TEXT.get(order.status, order.status)}」，"
                 f"不能接收新一轮预报（仅待执行/资源已调度/执行中可滚动；"
                 f"待审核单可重新发起，已闭环单不再滚动）")
    new_run = db.get(ForecastRun, new_run_id)
    if new_run is None:
        raise HTTPException(404, f"预报运行 #{new_run_id} 不存在")
    if new_run.id == order.run_id:
        raise HTTPException(409, f"运行 #{new_run_id} 即当前预报轮次，"
                                f"重跑请直接执行预报推演（幂等不重复建账）")
    if new_run.status != "done":
        raise HTTPException(409, f"预报运行 #{new_run_id} 尚未推演完成"
                                f"（状态：{new_run.status}），请先执行预报")

    # 同时串行化新旧两运行，按 id 定序加锁避免与审核/执行互锁
    old_run_id = order.run_id
    locks = sorted({old_run_id, new_run.id})
    with _order_id_lock(order_id), ExitStack() as stack:
        for rid in locks:
            stack.enter_context(_order_lock(rid))
        # 加锁后复查状态，冲突运行已有处置单则拒绝（一次运行至多一单）
        db.refresh(order)
        if order.status not in REFRESHABLE_STATUSES:
            raise HTTPException(409, "处置单状态已变化，请刷新后重试")
        clash = (db.query(DisposalOrder)
                 .filter(DisposalOrder.run_id == new_run.id,
                         DisposalOrder.id != order.id).first())
        if clash is not None:
            raise HTTPException(
                409, f"预报运行 #{new_run.id} 已挂接处置单 #{clash.id}，"
                     f"不能并入本单（一次预报运行至多一单）")

        # 新一轮运行的方案/过程线可能缺失（历史运行），只补算派生数据
        new_event, new_plan = _ensure_artifacts(db, new_run)

        old_run = db.get(ForecastRun, old_run_id)
        old_event = db.get(RainfallEvent, old_run.event_id) if old_run else None
        delta = {
            "from_run_id": old_run_id,
            "to_run_id": new_run.id,
            "from_event": old_event.name if old_event else f"运行#{old_run_id}",
            "to_event": new_event.name if new_event else f"运行#{new_run.id}",
            "mode": new_run.mode,
        }

        # ---- 增量 1：预警台账按「目标身份」迁移 ----
        # 两阶段：先确定身份对应并删除新运行占位记录（flush 落库），再把旧
        # 台账迁移到新运行——避免同事务 update 早于 delete 触发唯一约束冲突。
        new_warnings = (db.query(WarningRecord)
                        .filter(WarningRecord.run_id == new_run.id).all())
        match_w = []
        added_w = 0
        linked_old_warnings = (db.query(WarningRecord)
                               .filter(WarningRecord.run_id == old_run_id,
                                       WarningRecord.disposal_id == order.id).all())
        old_w_index = {(w.target_type, w.target_id, w.kind): w
                       for w in linked_old_warnings}
        for nw in new_warnings:
            old = old_w_index.get((nw.target_type, nw.target_id, nw.kind))
            if old is None:
                # 新一轮新增预警（或仅历史遗留记录对应的站点）：挂接本单
                nw.disposal_id = order.id
                added_w += 1
            else:
                match_w.append((old, nw))
                db.delete(nw)
        db.flush()
        carried_w = escalated_w = 0
        for old, nw in match_w:
            if (old.level or "") != (nw.level or ""):
                escalated_w += 1
            # 身份迁移：旧台账挪到新运行并刷新派生字段；
            # 人工状态/处置单挂接/首次触发时间原样保留
            old.run_id = new_run.id
            old.target_name = nw.target_name
            old.level = nw.level
            old.value = nw.value
            old.threshold = nw.threshold
            old.message = nw.message
            carried_w += 1
        db.flush()
        # 上一轮挂接本单、但新一轮不再预警的站点：保留台账不抹除
        # （人工销警/处置中记录继续闭环）
        retired_w = (db.query(WarningRecord)
                     .filter(WarningRecord.run_id == old_run_id,
                             WarningRecord.disposal_id == order.id).count())
        delta["warnings"] = {"carried": carried_w, "added": added_w,
                             "escalated": escalated_w, "retired": retired_w}

        # ---- 增量 2：转移台账按「风险区」身份迁移（同样两阶段）----
        new_evacs = (db.query(EvacuationRecord)
                     .filter(EvacuationRecord.run_id == new_run.id).all())
        match_e = []
        newzone_e = 0
        linked_old_evacs = (db.query(EvacuationRecord)
                            .filter(EvacuationRecord.run_id == old_run_id,
                                    EvacuationRecord.disposal_id == order.id).all())
        old_e_index = {e.zone_id: e for e in linked_old_evacs}
        for ne in new_evacs:
            old = old_e_index.get(ne.zone_id)
            if old is None:
                ne.disposal_id = order.id
                # 执行中单新增风险区：转移即刻联动为转移中（处置正在进行）
                if order.status == "executed":
                    ne.status = "moving"
                newzone_e += 1
            else:
                match_e.append((old, ne))
                db.delete(ne)
        db.flush()
        carried_e = 0
        for old, ne in match_e:
            old.run_id = new_run.id
            if old.status == "pending":
                # 尚未人工处置才刷新触发信息与受威胁人数
                old.zone_name = ne.zone_name
                old.triggered_by = ne.triggered_by
                old.people = ne.people
            carried_e += 1
        db.flush()
        # 分配记录（避难点/车辆/物资）以 evacuation_id 为键：迁移后记录 id
        # 不变、天然沿用；已出库份额不回滚、执行中车辆状态不动。
        # 新一轮不再触发转移的旧风险区：保留台账（moving/safe 人工状态不抹除）
        retired_e = (db.query(EvacuationRecord)
                     .filter(EvacuationRecord.run_id == old_run_id,
                             EvacuationRecord.disposal_id == order.id).count())
        delta["evacuations"] = {"carried": carried_e, "added": newzone_e,
                                "retired": retired_e}

        # ---- 增量 3：方案快照滚动（保留轮次历史）----
        snapshot = _stamp_round(db, order, _build_plan_snapshot(db, new_run, new_plan),
                                new_run, rolling=True)

        # ---- 增量 4：水库工况 ----
        reservoir_note = ""
        if order.status in ("approved", "resourced"):
            # 尚未实际执行：按新方案末态回写水库工况
            for item in snapshot["reservoirs"]:
                res = db.get(Reservoir, item["id"]) if item["id"] else None
                if res is not None:
                    res.current_level = item["final_level"]
                    res.current_storage = item["final_storage"]
            reservoir_note = "水库工况已按新方案末水位/末库容更新；"
        else:
            # 执行中：只滚动方案目标，不反演正在运用的实际库水位/库容
            reservoir_note = "执行中，水库维持当前实际运用工况，仅更新方案目标；"

        # ---- 增量 5：执行中单的追加资源即刻联动 ----
        if order.status == "executed":
            # 已追加派车（物资管理员在执行中补配）即刻发车；物资增量出库
            # 由指挥员再确认调度令完成，已 departed 车辆状态不动
            dispatches = (db.query(VehicleDispatch)
                          .filter(VehicleDispatch.disposal_id == order.id).all())
            for d in dispatches:
                v = db.get(Vehicle, d.vehicle_id)
                if v is not None and v.status in ("standby", "dispatched"):
                    v.status = "departed"

        # ---- 滚动绑定 ----
        order.run_id = new_run.id
        round_no = snapshot["round"]
        snapshot["last_refresh"] = {
            "at": datetime.now().isoformat(), "by": operator.strip() or "值班调度员",
            "from_run_id": old_run_id, "to_run_id": new_run.id,
            "delta": delta,
        }
        order.plan_snapshot = snapshot
        sig = operator.strip() or "值班调度员"
        line = (f"[新一轮预报·第{round_no}轮] {sig} 接收「{delta['to_event']}·"
                f"{MODE_TEXT.get(new_run.mode, new_run.mode)}」"
                f"（由运行#{old_run_id}「{delta['from_event']}」滚动）："
                f"预警延续{carried_w}（等级变化{escalated_w}）、新增{delta['warnings']['added']}、"
                f"解除{retired_w}；转移延续{carried_e}、新增{newzone_e}、解除{retired_e}；"
                f"{reservoir_note}已出库物资与执行中车辆保持不变")
        if note.strip():
            line += f"。说明：{note.strip()}"
        order.remark = (order.remark + "\n" + line).strip()
        db.commit()
        db.refresh(order)
        result = serialize_order(db, order)
        result["refresh_delta"] = delta
        return result


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
