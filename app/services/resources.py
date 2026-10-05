"""应急资源与避难点协同调度。

围绕审核通过的预警处置单（DisposalOrder），三类岗位协同分配应急资源：

    转移负责人 transfer_lead  → 分配避难点容量（ShelterAssignment）
    物资管理员 supply_manager → 分配车辆与物资（VehicleDispatch / SupplyAllocation）
    指挥员     commander       → 确认资源调度令（approved → resourced）

容量/运力/库存跨处置单统一校验（其它处置单已占份额计入占用），超分返回 409；
指挥员确认调度令时实际扣减物资库存，并把避难点分配回写转移进度
(evacuation_records.shelter_id/shelter_name)，同时风险预警进入「处置中」。
跳过资源协同直接启动执行也允许（兼容既有四态流转与历史处置记录）；
执行发车、完成归队/入库，闭环后资源份额从「在途占用」释放。
处置单执行中（executed，如已滚动接收新一轮预报、新增风险区）支持资源
增量追加：可再分容量/车辆/物资并由指挥员再确认调度令（物资只出增量、
追加车辆即派即发），但已占用容量/执行中车辆/已出库物资不可撤回。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import (DisposalOrder, EvacuationRecord, Shelter,
                        ShelterAssignment, Supply, SupplyAllocation, Vehicle,
                        VehicleDispatch, WarningRecord)

ROLE_TEXT = {"dispatcher": "调度员", "duty": "预警值守",
             "transfer_lead": "转移负责人", "supply_manager": "物资管理员",
             "commander": "指挥员"}

# 资源份额仍计入占用的处置单状态（initiated 无分配；闭环后容量/运力释放）
VEHICLE_KIND_TEXT = {"bus": "大巴", "truck": "货车", "ambulance": "救护车"}
VEHICLE_STATUS_TEXT = {"standby": "待命", "dispatched": "已派出",
                       "departed": "执行中", "returned": "已归队"}


# ---------------- 基础资源台账（带实时可用量） ----------------
def list_shelters(db: Session) -> list:
    rows = db.query(Shelter).filter(Shelter.active == 1).order_by(Shelter.id).all()
    used = _shelter_used(db)
    return [{
        "id": s.id, "name": s.name, "address": s.address,
        "capacity": s.capacity, "contact": s.contact,
        "x": s.x, "y": s.y,
        "used": used.get(s.id, 0),
        "available": s.capacity - used.get(s.id, 0),
    } for s in rows]


def list_vehicles(db: Session) -> list:
    rows = db.query(Vehicle).filter(Vehicle.active == 1).order_by(Vehicle.id).all()
    busy = _busy_vehicle_ids(db)
    result = []
    for v in rows:
        # 本车已被其它进行中处置单占用，或处于执行中状态，则不可再派
        available = v.id not in busy and v.status != "departed"
        result.append({
            "id": v.id, "plate": v.plate,
            "kind": v.kind, "kind_text": VEHICLE_KIND_TEXT.get(v.kind, v.kind),
            "seats": v.seats, "team": v.team,
            "status": v.status, "status_text": VEHICLE_STATUS_TEXT.get(v.status, v.status),
            "available": available,
        })
    return result


def list_supplies(db: Session) -> list:
    rows = db.query(Supply).filter(Supply.active == 1).order_by(Supply.id).all()
    committed = _supply_committed(db)
    result = []
    for s in rows:
        used = committed.get(s.id, 0)
        result.append({
            "id": s.id, "name": s.name, "unit": s.unit,
            "stock": s.stock, "safety_stock": s.safety_stock,
            "committed": used,
            "available": s.stock - used,
            "low": s.stock - used <= s.safety_stock,
        })
    return result


# ---------------- 占用量计算（跨处置单统一口径） ----------------
def _active_order_ids(db: Session, exclude_id: int | None = None) -> set:
    """资源份额仍在占用的处置单：审核通过后（approved 起）至闭环前。

    initiated 单尚无资源分配，无需计入；completed 单容量/运力已释放。
    """
    q = db.query(DisposalOrder.id).filter(
        DisposalOrder.status.in_(("approved", "resourced", "executed")))
    if exclude_id is not None:
        q = q.filter(DisposalOrder.id != exclude_id)
    return {row[0] for row in q.all()}


def _shelter_used(db: Session, exclude_order: int | None = None) -> dict:
    """各避难点被进行中处置单占用的容量（闭环单不再占用）。"""
    ids = _active_order_ids(db, exclude_order)
    if not ids:
        return {}
    used: dict = {}
    rows = (db.query(ShelterAssignment.shelter_id, ShelterAssignment.people)
            .filter(ShelterAssignment.disposal_id.in_(ids)).all())
    for sid, people in rows:
        used[sid] = used.get(sid, 0) + (people or 0)
    return used


def _busy_vehicle_ids(db: Session, exclude_order: int | None = None) -> set:
    """已被其它进行中处置单分配的车辆集合。"""
    ids = _active_order_ids(db, exclude_order)
    if not ids:
        return set()
    rows = (db.query(VehicleDispatch.vehicle_id)
            .filter(VehicleDispatch.disposal_id.in_(ids)).all())
    return {row[0] for row in rows}


def _supply_committed(db: Session, exclude_order: int | None = None) -> dict:
    """各物资被审核通过但尚未确认调度令的处置单预占的数量。

    approved 单的分配为库存预占；resourced/executed 单已实际出库
    (supply.stock 已扣减)，不再计入预占；completed 单同理。
    """
    q = db.query(DisposalOrder.id).filter(DisposalOrder.status == "approved")
    if exclude_order is not None:
        q = q.filter(DisposalOrder.id != exclude_order)
    ids = {row[0] for row in q.all()}
    if not ids:
        return {}
    committed: dict = {}
    rows = (db.query(SupplyAllocation.supply_id,
                     SupplyAllocation.quantity - SupplyAllocation.issued_quantity)
            .filter(SupplyAllocation.disposal_id.in_(ids)).all())
    for sid, pending in rows:
        committed[sid] = committed.get(sid, 0) + (pending or 0)
    return committed


# ---------------- 处置单资源方案（协同看板数据） ----------------
def get_order_resources(db: Session, order: DisposalOrder) -> dict:
    """汇总处置单的避难点/车辆/物资分配、覆盖情况与各岗位操作状态。"""
    shelter_rows = db.query(ShelterAssignment).filter(
        ShelterAssignment.disposal_id == order.id).all()
    vehicle_rows = db.query(VehicleDispatch).filter(
        VehicleDispatch.disposal_id == order.id).all()
    supply_rows = db.query(SupplyAllocation).filter(
        SupplyAllocation.disposal_id == order.id).all()
    evacs = db.query(EvacuationRecord).filter(
        EvacuationRecord.disposal_id == order.id).all()
    warnings = db.query(WarningRecord).filter(
        WarningRecord.disposal_id == order.id).all()

    shelter_by_id = {s.id: s for s in db.query(Shelter).all()}
    vehicle_by_id = {v.id: v for v in db.query(Vehicle).all()}
    supply_by_id = {s.id: s for s in db.query(Supply).all()}

    need_by_evac = {e.id: e.people for e in evacs}
    shelter_seats: dict = {eid: 0 for eid in need_by_evac}
    for r in shelter_rows:
        shelter_seats[r.evacuation_id] = shelter_seats.get(r.evacuation_id, 0) + r.people
    vehicle_seats: dict = {}
    for r in vehicle_rows:
        v = vehicle_by_id.get(r.vehicle_id)
        cap = (v.seats if v else 0) * max(r.shuttles or 1, 1)
        if r.evacuation_id:
            vehicle_seats[r.evacuation_id] = vehicle_seats.get(r.evacuation_id, 0) + cap
        else:
            vehicle_seats[0] = vehicle_seats.get(0, 0) + cap  # 机动运力池

    total_people = sum(need_by_evac.values())
    total_shelter = sum(r.people or 0 for r in shelter_rows)
    total_vehicle = sum((vehicle_by_id.get(r.vehicle_id).seats if vehicle_by_id.get(r.vehicle_id) else 0)
                        * max(r.shuttles or 1, 1) for r in vehicle_rows)
    total_supply_qty = sum(r.quantity or 0 for r in supply_rows)

    def _evac_brief(e: EvacuationRecord) -> dict:
        need = e.people
        sh = shelter_seats.get(e.id, 0)
        seat = vehicle_seats.get(e.id, 0)
        pool = vehicle_seats.get(0, 0)
        return {
            "evacuation_id": e.id, "zone_id": e.zone_id, "zone_name": e.zone_name,
            "people": need, "status": e.status,
            "shelter_seats": sh, "vehicle_seats": seat, "pool_seats": pool,
            "shelter_ready": sh >= need,
            "vehicle_ready": seat + pool >= need,
            "shelter_name": e.shelter_name or "",
        }

    shelters = [{
        "id": r.id, "evacuation_id": r.evacuation_id,
        "shelter_id": r.shelter_id,
        "shelter_name": shelter_by_id[r.shelter_id].name if shelter_by_id.get(r.shelter_id) else "（已删除）",
        "people": r.people, "note": r.note,
        "created_by": r.created_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in shelter_rows]
    vehicles = [{
        "id": r.id, "vehicle_id": r.vehicle_id,
        "plate": vehicle_by_id[r.vehicle_id].plate if vehicle_by_id.get(r.vehicle_id) else "（已删除）",
        "kind": vehicle_by_id[r.vehicle_id].kind if vehicle_by_id.get(r.vehicle_id) else "",
        "seats": vehicle_by_id[r.vehicle_id].seats if vehicle_by_id.get(r.vehicle_id) else 0,
        "evacuation_id": r.evacuation_id, "shuttles": r.shuttles,
        "capacity": (vehicle_by_id[r.vehicle_id].seats if vehicle_by_id.get(r.vehicle_id) else 0)
                    * max(r.shuttles or 1, 1),
        "note": r.note, "created_by": r.created_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in vehicle_rows]
    supplies = [{
        "id": r.id, "supply_id": r.supply_id,
        "supply_name": supply_by_id[r.supply_id].name if supply_by_id.get(r.supply_id) else "（已删除）",
        "unit": supply_by_id[r.supply_id].unit if supply_by_id.get(r.supply_id) else "",
        "evacuation_id": r.evacuation_id, "quantity": r.quantity,
        "issued_quantity": r.issued_quantity or 0,
        "issued": (r.issued_quantity or 0) >= (r.quantity or 0),
        "note": r.note, "created_by": r.created_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in supply_rows]

    pool = vehicle_seats.get(0, 0)
    shelter_ready = all(need_by_evac[eid] <= shelter_seats.get(eid, 0) for eid in need_by_evac)
    vehicle_ready = all(need_by_evac[eid] <= vehicle_seats.get(eid, 0) + pool for eid in need_by_evac)
    return {
        "status": order.status,
        "resourced_by": order.resourced_by,
        "resourced_at": order.resourced_at.isoformat() if order.resourced_at else None,
        "evacuations": [_evac_brief(e) for e in evacs],
        "warning_count": len(warnings),
        "handling_warnings": sum(1 for w in warnings if w.status == "handling"),
        "shelters": shelters, "vehicles": vehicles, "supplies": supplies,
        "coverage": {
            "zones": len(evacs), "people": total_people,
            "shelter_seats": total_shelter,
            "vehicle_seats": total_vehicle,
            "supply_kinds": len({r.supply_id for r in supply_rows}),
            "supply_quantity": total_supply_qty,
            "shelter_ready": shelter_ready,
            "vehicle_ready": vehicle_ready,
            "ready": shelter_ready and vehicle_ready,
        },
    }


# ---------------- 通用校验 ----------------
# 可进行资源分配/调度令操作的处置单状态：
# approved/resourced 为常规协同阶段；executed 执行中允许「增量追加」
# （新一轮预报新增风险区时补配容量/运力/物资，已出库物资与执行中车辆不动）。
RESOURCE_OPEN_STATUSES = ("approved", "resourced", "executed")


def _get_order_resources_ready(db: Session, order_id: int) -> DisposalOrder:
    order = db.get(DisposalOrder, order_id)
    if order is None:
        raise HTTPException(404, f"处置单 #{order_id} 不存在")
    if order.status not in RESOURCE_OPEN_STATUSES:
        raise HTTPException(
            409, "应急资源分配需在预警值守审核通过后进行（处置单须为"
                 "「待执行/资源已调度/执行中」）")
    return order


def _get_evac(db: Session, order: DisposalOrder, evac_id: int) -> EvacuationRecord:
    evac = db.get(EvacuationRecord, evac_id)
    if evac is None or evac.disposal_id != order.id:
        raise HTTPException(404, "转移行动记录不存在或不属于该处置单")
    return evac


# ---------------- 转移负责人：避难点容量分配 ----------------
def assign_shelter(db: Session, order_id: int, body: dict) -> dict:
    if body.get("role") != "transfer_lead":
        raise HTTPException(403, f"避难点容量由转移负责人分配（当前角色："
                                f"{ROLE_TEXT.get(body.get('role'), body.get('role'))}）")
    order = _get_order_resources_ready(db, order_id)
    evac = _get_evac(db, order, int(body.get("evacuation_id") or 0))
    shelter = db.get(Shelter, int(body.get("shelter_id") or 0))
    if shelter is None or not shelter.active:
        raise HTTPException(404, "避难点不存在或已停用")
    people = int(body.get("people") or 0)
    if people <= 0:
        raise HTTPException(422, "安置人数须大于 0")

    # 同点同区重复分配：幂等归并为更新（便于调整人数）。
    # 先查询再构造新对象，避免未 flush 的 pending 对象进入容量统计查询。
    rec = (db.query(ShelterAssignment)
           .filter(ShelterAssignment.disposal_id == order.id,
                   ShelterAssignment.evacuation_id == evac.id,
                   ShelterAssignment.shelter_id == shelter.id).first())
    other_used = _shelter_used(db, exclude_order=order.id)
    # 本单在该避难点的占用：同点其它记录之和（更新时当前记录按新值替换）
    own_rows = db.query(ShelterAssignment).filter(
        ShelterAssignment.disposal_id == order.id,
        ShelterAssignment.shelter_id == shelter.id).all()
    own_used = sum(r.people or 0 for r in own_rows if rec is None or r.id != rec.id)
    if other_used.get(shelter.id, 0) + own_used + people > shelter.capacity:
        raise HTTPException(
            409, f"避难点「{shelter.name}」容量不足：总容量 {shelter.capacity} 人，"
                 f"其它处置单占用 {other_used.get(shelter.id, 0)} 人，本单已分 {own_used} 人，"
                 f"本次再分 {people} 人将超分")

    if rec is None:
        rec = ShelterAssignment(disposal_id=order.id, evacuation_id=evac.id,
                                shelter_id=shelter.id)
        db.add(rec)
    rec.people = people
    rec.note = (body.get("note") or "").strip()
    rec.created_by = (body.get("operator") or "").strip() or "转移负责人"
    db.commit()
    return get_order_resources(db, order)


def release_shelter(db: Session, order_id: int, assignment_id: int, role: str) -> dict:
    if role != "transfer_lead":
        raise HTTPException(403, "仅转移负责人可调整避难点分配")
    order = _get_order_resources_ready(db, order_id)
    if order.status == "executed":
        raise HTTPException(409, "处置已启动执行：占用中的避难容量不可撤回，"
                                "新一轮预报只需追加分配")
    rec = db.get(ShelterAssignment, assignment_id)
    if rec is None or rec.disposal_id != order.id:
        raise HTTPException(404, "避难点分配记录不存在或不属于该处置单")
    db.delete(rec)
    db.commit()
    return get_order_resources(db, order)


# ---------------- 物资管理员：车辆分配 ----------------
def assign_vehicle(db: Session, order_id: int, body: dict) -> dict:
    if body.get("role") != "supply_manager":
        raise HTTPException(403, f"车辆由物资管理员分配（当前角色："
                                f"{ROLE_TEXT.get(body.get('role'), body.get('role'))}）")
    order = _get_order_resources_ready(db, order_id)
    vehicle = db.get(Vehicle, int(body.get("vehicle_id") or 0))
    if vehicle is None or not vehicle.active:
        raise HTTPException(404, "车辆不存在或已停用")
    shuttles = int(body.get("shuttles") or 1)
    if shuttles <= 0:
        raise HTTPException(422, "计划趟次须大于 0")

    evac_id = body.get("evacuation_id")
    evac = None
    if evac_id:
        evac = _get_evac(db, order, int(evac_id))

    busy = _busy_vehicle_ids(db, exclude_order=order.id)
    if vehicle.id in busy:
        raise HTTPException(409, f"车辆 {vehicle.plate} 已被其它处置单占用")
    if vehicle.status == "departed":
        raise HTTPException(409, f"车辆 {vehicle.plate} 正在执行任务，无法派出")

    # 同一处置单 × 车辆幂等：重复派车归并为更新趟次/服务区
    rec = (db.query(VehicleDispatch)
           .filter(VehicleDispatch.disposal_id == order.id,
                   VehicleDispatch.vehicle_id == vehicle.id).first())
    if rec is None:
        rec = VehicleDispatch(disposal_id=order.id, vehicle_id=vehicle.id)
        db.add(rec)
    rec.evacuation_id = evac.id if evac else None
    rec.shuttles = shuttles
    rec.note = (body.get("note") or "").strip()
    rec.created_by = (body.get("operator") or "").strip() or "物资管理员"
    if order.status == "executed":
        # 执行中追加派车：即派即发；正在执行（departed）的车辆状态不动
        if vehicle.status in ("standby", "dispatched", "returned"):
            vehicle.status = "departed"
    elif vehicle.status == "standby":
        vehicle.status = "dispatched"
    db.commit()
    return get_order_resources(db, order)


def release_vehicle(db: Session, order_id: int, dispatch_id: int, role: str) -> dict:
    if role != "supply_manager":
        raise HTTPException(403, "仅物资管理员可调整车辆分配")
    order = _get_order_resources_ready(db, order_id)
    if order.status == "executed":
        raise HTTPException(409, "处置已启动执行：车辆正在执行任务，不可撤回；"
                                "新一轮预报如需更多运力请追加派车")
    rec = db.get(VehicleDispatch, dispatch_id)
    if rec is None or rec.disposal_id != order.id:
        raise HTTPException(404, "车辆分配记录不存在或不属于该处置单")
    vehicle = db.get(Vehicle, rec.vehicle_id)
    db.delete(rec)
    db.flush()
    # 本单不再使用该车且未在执行中：恢复待命
    if vehicle is not None and vehicle.status == "dispatched":
        still = db.query(VehicleDispatch).filter(
            VehicleDispatch.disposal_id == order.id,
            VehicleDispatch.vehicle_id == vehicle.id).count()
        if not still:
            vehicle.status = "standby"
    db.commit()
    return get_order_resources(db, order)


# ---------------- 物资管理员：物资分配 ----------------
def assign_supply(db: Session, order_id: int, body: dict) -> dict:
    if body.get("role") != "supply_manager":
        raise HTTPException(403, f"物资由物资管理员分配（当前角色："
                                f"{ROLE_TEXT.get(body.get('role'), body.get('role'))}）")
    order = _get_order_resources_ready(db, order_id)
    supply = db.get(Supply, int(body.get("supply_id") or 0))
    if supply is None or not supply.active:
        raise HTTPException(404, "物资不存在或已停用")
    quantity = int(body.get("quantity") or 0)
    if quantity <= 0:
        raise HTTPException(422, "分配数量须大于 0")

    evac_id = body.get("evacuation_id")
    evac = None
    if evac_id:
        evac = _get_evac(db, order, int(evac_id))

    rec_q = (db.query(SupplyAllocation)
             .filter(SupplyAllocation.disposal_id == order.id,
                     SupplyAllocation.supply_id == supply.id,
                     SupplyAllocation.evacuation_id.is_(evac.id if evac else None)))
    rec = rec_q.first()
    other_committed = _supply_committed(db, exclude_order=order.id)
    own_q = (db.query(SupplyAllocation)
             .filter(SupplyAllocation.disposal_id == order.id,
                     SupplyAllocation.supply_id == supply.id).all())
    # 本单预占口径只计尚未出库的增量（已出库部分已扣减 stock，不再占可用量）
    own_total = sum((r.quantity or 0) - (r.issued_quantity or 0)
                    for r in own_q if rec is None or r.id != rec.id)
    # 更新已部分出库的分配时，已出库份额仍占用新数量的一部分，给予等额额度
    capacity = supply.stock + (rec.issued_quantity if rec else 0)
    if other_committed.get(supply.id, 0) + own_total + quantity > capacity:
        raise HTTPException(
            409, f"物资「{supply.name}」可用库存不足：可出库 {supply.stock}{supply.unit}，"
                 f"其它处置单已预占 {other_committed.get(supply.id, 0)}{supply.unit}，"
                 f"本单待出库 {own_total}{supply.unit}，本次再分 {quantity}{supply.unit} 将超分")

    if rec is None:
        rec = SupplyAllocation(disposal_id=order.id, supply_id=supply.id,
                               evacuation_id=evac.id if evac else None)
        db.add(rec)
    rec.quantity = quantity
    rec.note = (body.get("note") or "").strip()
    rec.created_by = (body.get("operator") or "").strip() or "物资管理员"
    db.commit()
    return get_order_resources(db, order)


def release_supply(db: Session, order_id: int, allocation_id: int, role: str) -> dict:
    if role != "supply_manager":
        raise HTTPException(403, "仅物资管理员可调整物资分配")
    order = _get_order_resources_ready(db, order_id)
    rec = db.get(SupplyAllocation, allocation_id)
    if rec is None or rec.disposal_id != order.id:
        raise HTTPException(404, "物资分配记录不存在或不属于该处置单")
    if order.status == "executed" or (rec.issued_quantity or 0) > 0:
        raise HTTPException(409, "已出库物资不可撤回（执行中仅支持增量追加分配）")
    db.delete(rec)
    db.commit()
    return get_order_resources(db, order)


# ---------------- 指挥员：确认资源调度令 ----------------
def confirm_resources(db: Session, order_id: int, operator: str, role: str,
                      order_text: str = "") -> dict:
    """指挥员确认资源调度令：校验避难容量与运力覆盖，物资出库，回写转移进度与风险预警。"""
    if role != "commander":
        raise HTTPException(403, f"资源调度令由指挥员确认（当前角色："
                                f"{ROLE_TEXT.get(role, role)}）")
    order = db.get(DisposalOrder, order_id)
    if order is None:
        raise HTTPException(404, f"处置单 #{order_id} 不存在")
    if order.status not in RESOURCE_OPEN_STATUSES:
        raise HTTPException(409, "须审核通过后才能确认资源调度令")

    plan = get_order_resources(db, order)
    evacs = plan["evacuations"]
    if not evacs:
        raise HTTPException(409, "本次处置单无关联转移台账，无需资源调度，可直接启动转移执行")

    gaps, seat_gaps = [], []
    for e in evacs:
        if not e["shelter_ready"]:
            gaps.append(f"{e['zone_name']} 需 {e['people']} 人，"
                        f"避难容量仅 {e['shelter_seats']} 人")
        if not e["vehicle_ready"]:
            seat_gaps.append(f"{e['zone_name']} 需 {e['people']} 人，"
                             f"车辆运力（含机动）{e['vehicle_seats'] + e['pool_seats']} 人")
    if gaps:
        raise HTTPException(409, "避难点容量尚未覆盖全部转移人口：" + "；".join(gaps))
    if seat_gaps:
        raise HTTPException(409, "车辆运力尚未覆盖全部转移人口：" + "；".join(seat_gaps))

    # 物资出库：按各分配「数量 - 已出库」的增量扣减库存，重复确认只出增量
    alloc_rows = db.query(SupplyAllocation).filter(
        SupplyAllocation.disposal_id == order.id).all()
    for r in alloc_rows:
        supply = db.get(Supply, r.supply_id)
        if supply is None:
            continue
        delta = (r.quantity or 0) - (r.issued_quantity or 0)
        if delta <= 0:
            continue
        if delta > supply.stock:
            raise HTTPException(
                409, f"物资「{supply.name}」当前库存 {supply.stock}{supply.unit}，"
                     f"不足以出库增量 {delta}{supply.unit}")
        supply.stock -= delta
        r.issued_quantity = r.quantity

    # 回写转移进度：主避难点（每个区分配人数最多的点）挂到转移台账
    assignments = db.query(ShelterAssignment).filter(
        ShelterAssignment.disposal_id == order.id).all()
    best: dict = {}
    for r in assignments:
        cur = best.get(r.evacuation_id)
        if cur is None or r.people > cur.people:
            best[r.evacuation_id] = r
    shelter_by_id = {s.id: s for s in db.query(Shelter).all()}
    for evac_id, r in best.items():
        evac = db.get(EvacuationRecord, evac_id)
        if evac is not None:
            evac.shelter_id = r.shelter_id
            evac.shelter_name = shelter_by_id[r.shelter_id].name \
                if shelter_by_id.get(r.shelter_id) else ""

    # 风险预警联动：active → handling（已销警/处置中保持原状）
    warnings = db.query(WarningRecord).filter(
        WarningRecord.disposal_id == order.id).all()
    for w in warnings:
        if w.status == "active":
            w.status = "handling"

    # 车辆：未执行时标记已派出；执行中追加车辆即派即发，已发车的不动
    dispatches = db.query(VehicleDispatch).filter(
        VehicleDispatch.disposal_id == order.id).all()
    for d in dispatches:
        v = db.get(Vehicle, d.vehicle_id)
        if v is None:
            continue
        if order.status == "executed":
            if v.status in ("standby", "dispatched", "returned"):
                v.status = "departed"
        elif v.status in ("standby", "dispatched"):
            v.status = "dispatched"

    # 执行中接收新一轮预报后的增量调度令：处置单状态保持 executed，
    # 仅追加出库/发车/回写；常规确认则 approved → resourced
    tag = "[增量资源调度令]" if order.status == "executed" else "[资源调度令]"
    if order.status != "executed":
        order.status = "resourced"
    order.resourced_by = (operator or "").strip() or "值班指挥员"
    order.resourced_at = datetime.now()
    if order_text.strip():
        order.remark = (order.remark + f"\n{tag} {order_text.strip()}").strip()
    db.commit()
    return get_order_resources(db, order)
