"""洪水预报编排：降雨 → 子流域产汇流 → 河网叠加演算 → 水库调度 → 出口断面过程。

将事件过程组装为一次"预报运行"，并按 natural / rule / optimized 三种调度
工况分别推演，得到各节点（断面/水库/出口）的流量与水位序列，供前端对比展示，
同时据此进行预警等级判定与风险区/转移触发。

幂等约定：同一 (event_id, mode) 归并为同一次 ForecastRun（业务幂等键 +
数据库唯一约束），重复执行 / 并发触发 / 异常重试都复用该运行，预警与转移
台账按 run_id 幂等 upsert，不会重复新增；运行状态 running → done / failed
可支撑失败重试接续。
"""
from __future__ import annotations

import threading
from contextlib import nullcontext
from typing import Dict, List

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (EvacuationRecord, FloodZone, ForecastRun, ForecastSeries,
                        OperationPlan, RainStation, RainfallEvent, Reservoir,
                        RiverNode, RiverReach, SubBasin, WaterStation, WarningRecord)
from app.services.hydrology import (BasinTopology, compute_sub_basin, muskingum)
from app.services.reservoir import run_scenarios

DT_H = 1.0  # 模拟时间步长（小时）

_LEVEL_TEXT = {"blue": "蓝色预警", "yellow": "黄色预警", "orange": "橙色预警", "red": "红色预警"}

# 同一 (event_id, mode) 的预报在进程内串行执行，避免并发触发时对同一幂等键
# 重复计算与写库冲突；数据库唯一约束是跨进程场景的最终防线。
_run_locks: Dict[tuple, threading.Lock] = {}
_run_locks_guard = threading.Lock()


def _run_lock(key: tuple) -> threading.Lock:
    with _run_locks_guard:
        return _run_locks.setdefault(key, threading.Lock())


def _get_or_create_run(db: Session, event_id: int, mode: str) -> ForecastRun:
    """按业务幂等键 (event_id, mode) 获取或创建预报运行。

    重复触发 / 异常重试都归并到同一次运行；并发下由唯一约束兜底，
    插入冲突时回滚并改读已存在的运行记录。
    """
    run = (db.query(ForecastRun)
           .filter(ForecastRun.event_id == event_id, ForecastRun.mode == mode)
           .first())
    if run:
        return run
    run = ForecastRun(event_id=event_id, mode=mode, status="running")
    db.add(run)
    try:
        db.commit()  # 立即提交：运行记录成为后续处置记录的幂等锚点
    except IntegrityError:
        db.rollback()
        run = (db.query(ForecastRun)
               .filter(ForecastRun.event_id == event_id, ForecastRun.mode == mode)
               .one())
    return run


def _mark_run_failed(db: Session, run_id: int) -> None:
    """尽力把运行标记为失败（供后续重试识别），不影响原异常继续抛出。"""
    try:
        run = db.get(ForecastRun, run_id)
        if run:
            run.status = "failed"
            db.commit()
    except Exception:
        db.rollback()


def _replace_series(db: Session, run_id: int, rows: list) -> None:
    """重建某次运行的全部过程线：纯派生数据，同事务先删后插，重复执行结果一致。"""
    (db.query(ForecastSeries).filter(ForecastSeries.run_id == run_id)
     .delete(synchronize_session=False))
    for node_id, kind, name, values in rows:
        ForecastSeries.save(db, run_id, node_id, kind, name, values)


def _upsert_warning(db: Session, run_id: int, data: dict) -> None:
    """按 (run_id, target_type, target_id, kind) 幂等写入预警。

    已存在时只刷新派生字段（等级/峰值/阈值/文案），保留人工处置状态
    (status)、处置单挂接 (disposal_id) 与首次触发时间 (created_at)，
    重跑不会重置已销警或已挂接处置单的记录。
    """
    rec = (db.query(WarningRecord)
           .filter(WarningRecord.run_id == run_id,
                   WarningRecord.target_type == data["target_type"],
                   WarningRecord.target_id == data["target_id"],
                   WarningRecord.kind == data["kind"])
           .first())
    if rec is None:
        db.add(WarningRecord(run_id=run_id, **data))
        return
    for field in ("target_name", "level", "value", "threshold", "message"):
        setattr(rec, field, data[field])


def _upsert_evacuation(db: Session, run_id: int, data: dict) -> None:
    """按 (run_id, zone_id) 幂等写入转移台账。

    已存在则不重复新增；仅在尚未人工处置 (pending) 时刷新触发信息，
    进行中/已完成的转移状态 (moving/safe) 不被重跑覆盖。
    """
    rec = (db.query(EvacuationRecord)
           .filter(EvacuationRecord.run_id == run_id,
                   EvacuationRecord.zone_id == data["zone_id"])
           .first())
    if rec is None:
        db.add(EvacuationRecord(run_id=run_id, status="pending", **data))
        return
    if rec.status == "pending":
        rec.zone_name = data["zone_name"]
        rec.triggered_by = data["triggered_by"]
        rec.people = data["people"]


def save_operation_plan(db: Session, run_id: int, mode: str, scenarios: dict,
                        res_list: list, res_inflow: dict, res_outflow: dict,
                        res_level: dict, res_storage: dict) -> OperationPlan:
    """按 run_id 幂等归档本次运行所选工况的联合调度方案。

    同一次预报运行重复推演只更新同一份方案（先删后插），方案中的各库
    终态水位/库容是处置单审核通过后回写水库工况的依据。
    """
    db.query(OperationPlan).filter(OperationPlan.run_id == run_id).delete(
        synchronize_session=False)
    chosen = scenarios[mode]
    natural = scenarios["natural"]
    peak_flow = chosen["objective"]
    nat_peak = natural["objective"]
    peak_ratio = round((nat_peak - peak_flow) / nat_peak * 100, 1) if nat_peak else 0.0

    storage_gain = 0.0
    outcome = {}
    gate_schedule = {}
    for r in res_list:
        stor = res_storage[r.id]
        gain = (stor[-1] - r.storage_at(r.current_level or r.normal_level)) if stor else 0.0
        storage_gain += gain
        gate = chosen["gate"].get(r.id, [])
        gate_schedule[str(r.id)] = {str(t): round(g, 3) for t, g in enumerate(gate)}
        lvl = res_level[r.id]
        out = res_outflow[r.id]
        outcome[str(r.id)] = {
            "name": r.name,
            "peak_level": round(max(lvl), 2) if lvl else 0.0,
            "final_level": round(lvl[-1], 2) if lvl else 0.0,
            "final_storage": round(stor[-1], 1) if stor else 0.0,
            "peak_outflow": round(max(out), 1) if out else 0.0,
            "storage_gain": round(gain, 1),
        }

    mode_name = {"natural": "天然过流", "rule": "规则调度",
                 "optimized": "联合优化调度"}.get(mode, mode)
    plan = OperationPlan(
        run_id=run_id, name=f"{mode_name}方案（运行#{run_id}）",
        objective=f"下游控制断面峰值压降至 {peak_flow:.0f} m³/s，削峰率 {peak_ratio}%",
        peak_flow=round(peak_flow, 1), peak_ratio=peak_ratio,
        storage_gain=round(storage_gain, 1),
        gate_schedule=gate_schedule, reservoir_outcome=outcome)
    db.add(plan)
    return plan


def _rainfall_at(db: Session, event: RainfallEvent, station: RainStation, total_steps: int) -> List[float]:
    """将情景面雨量过程线按站点放大系数还原为站点降雨（演示用泰森权重=1）。"""
    base = list(event.hyetograph or [])
    if len(base) < total_steps:
        base = base + [0.0] * (total_steps - len(base))
    return base[:total_steps]


def _level_from_flow(station: WaterStation, flow: float) -> float:
    """由断面流量经站点 rating 曲线推算水位；无曲线则用阈值锚点近似。"""
    th = station.thresholds or {}
    rating = th.get("rating")
    if isinstance(rating, list) and rating:
        if flow <= rating[0][0]:
            return rating[0][1]
        if flow >= rating[-1][0]:
            return rating[-1][1]
        for i in range(len(rating) - 1):
            q0, l0 = rating[i]
            q1, l1 = rating[i + 1]
            if q0 <= flow <= q1:
                return l0 + (l1 - l0) * (flow - q0) / (q1 - q0)
        return rating[-1][1]
    base = th.get("base_level", 0.0)
    anchor_flow = 100.0
    anchor_level = th.get("blue", base + 2.0) or (base + 2.0)
    return base + (flow / anchor_flow) * (anchor_level - base)


def _warning_level_for(station: WaterStation, flow: float, level: float) -> str:
    """按站阈值判定预警等级：red>orange>yellow>blue>''。"""
    th = station.thresholds or {}
    if th.get("red") and level >= th["red"]:
        return "red"
    if th.get("orange") and level >= th["orange"]:
        return "orange"
    if th.get("yellow") and level >= th["yellow"]:
        return "yellow"
    if th.get("blue") and level >= th["blue"]:
        return "blue"
    return ""


def run_forecast(db: Session, event: RainfallEvent, reservoir_rule: str = "optimized",
                 persist: bool = True, write_ledgers: bool = True) -> dict:
    """执行一次完整洪水预报。

    persist=True 时按 (event_id, mode) 幂等落库：重复执行 / 并发触发 /
    异常重试都复用同一次 ForecastRun，预警与转移台账不会重复新增。
    write_ledgers=False 时只重建过程线与调度方案（处置单为历史运行补齐
    审核依据用），不触碰预警/转移台账及其人工处置状态。
    """
    dt = DT_H
    subs = db.query(SubBasin).all()
    nodes = db.query(RiverNode).all()
    reaches = db.query(RiverReach).all()
    res_list = db.query(Reservoir).filter(Reservoir.active == 1).all()
    stations = db.query(WaterStation).all()

    if reservoir_rule in ("natural", "rule", "optimized"):
        mode = reservoir_rule
    else:
        mode = "optimized"

    total_steps = max(event.duration_h + 12, 36)

    topo = BasinTopology(db)
    sub_by_node: Dict[int, list] = {}
    for sub in subs:
        sub_by_node.setdefault(sub.outlet_node_id, []).append(sub)
    res_by_node = {r.node_id: r for r in res_list}

    # 同一幂等键的预报串行执行；不同情景/工况可并行
    ctx = _run_lock((event.id, mode)) if persist else nullcontext()
    with ctx:
        run = _get_or_create_run(db, event.id, mode) if persist else None
        try:
            # ---- 1. 各节点入库（子流域出流 + 上游河段到来），并做马斯京根叠加演算 ----
            node_flow: Dict[int, List[float]] = {n.id: [0.0] * total_steps for n in nodes}
            for sub in subs:
                node_flow[sub.outlet_node_id] = _add_series(
                    node_flow[sub.outlet_node_id],
                    compute_sub_basin(event.hyetograph or [0.0] * total_steps, sub, dt, total_steps))

            # 正向演算：按上游顺序把已算好的节点流量平移到下游。
            # 水库节点先记录"天然入库"，并继续透传到下游 —— 这样下游水库的入库
            # 已包含上游水库的天然来水；调度出流将在阶段 C 覆盖该节点流量。
            res_inflow: Dict[int, List[float]] = {r.id: [0.0] * total_steps for r in res_list}
            for nid in topo.upstream_order:
                q = node_flow.get(nid, [0.0] * total_steps)
                if nid in res_by_node:
                    res_inflow[res_by_node[nid].id] = list(q)
                for rch in topo.out_edges.get(nid, []):
                    shifted = muskingum(q, rch.k_hr, rch.x_coef, dt)
                    node_flow[rch.to_node_id] = _add_series(node_flow[rch.to_node_id], shifted)

            # ---- 2. 各工况水库调度演算，得到水库出流并叠加到下游 ----
            horizon = total_steps
            scenarios = run_scenarios(db, res_list, res_inflow, dt, horizon,
                                      downstream_weights={r.id: 1.0 for r in res_list})
            chosen = scenarios[mode]

            # 阶段 C：用各水库调度出流覆盖节点流量，并沿河段继续演算到下游节点。
            # 到达下游水库时，其调度出流替换该节点流量后继续传播（级联）。
            res_outflow: Dict[int, List[float]] = {}
            res_level: Dict[int, List[float]] = {}
            res_storage: Dict[int, List[float]] = {}
            for r in res_list:
                res_outflow[r.id] = chosen["sim"][r.id]["outflow"]
                res_level[r.id] = chosen["sim"][r.id]["level"]
                res_storage[r.id] = chosen["sim"][r.id]["storage"]

            node_flow = {n.id: [0.0] * total_steps for n in nodes}  # 重置，重新以出流为准传播
            # 先注入各子流域出流（与调度无关的部分，重新叠加）
            for sub in subs:
                node_flow[sub.outlet_node_id] = _add_series(
                    node_flow[sub.outlet_node_id],
                    compute_sub_basin(event.hyetograph or [0.0] * total_steps, sub, dt, total_steps))

            from collections import deque
            queue = deque(nid for nid in topo.upstream_order if nid in res_by_node)
            visited: set = set()
            while queue:
                nid = queue.popleft()
                if nid in visited:
                    continue
                visited.add(nid)
                res = res_by_node[nid]
                node_flow[nid] = list(res_outflow[res.id])
                for rch in topo.out_edges.get(nid, []):
                    shifted = muskingum(res_outflow[res.id], rch.k_hr, rch.x_coef, dt)
                    node_flow[rch.to_node_id] = _add_series(node_flow[rch.to_node_id], shifted)
                    if rch.to_node_id in res_by_node:
                        queue.append(rch.to_node_id)

            # 水库出流继续沿下游非水库节点演算（子流域区间入流已在上面叠加）
            # 由于上面只处理了水库链，还需把水库出流后的河段继续传到河口：
            # 直接对整个拓扑再跑一遍正向传播，但水库节点流量已固定为调度出流，
            # 以水库为终点的河段不再叠加（避免天然来水污染调度出流）。
            for nid in topo.upstream_order:
                if nid in visited:
                    continue
                q = node_flow.get(nid, [0.0] * total_steps)
                for rch in topo.out_edges.get(nid, []):
                    if rch.to_node_id in res_by_node:
                        continue
                    shifted = muskingum(q, rch.k_hr, rch.x_coef, dt)
                    node_flow[rch.to_node_id] = _add_series(node_flow[rch.to_node_id], shifted)

            # ---- 3. 水位序列与预警 / 风险区 / 转移 ----
            station_flow = {}
            station_level = {}
            station_warn = {}
            for st in stations:
                f = node_flow.get(st.node_id, [0.0] * total_steps)
                lv = [_level_from_flow(st, v) for v in f]
                station_flow[st.id] = f
                station_level[st.id] = lv
                wl = [_warning_level_for(st, f[i], lv[i]) for i in range(total_steps)]
                station_warn[st.id] = wl

            warnings = []
            evacuations = []
            zones = db.query(FloodZone).all()

            # 站点峰值预警 → 待写入预警台账（persist 时按 run 幂等落库）
            pending_warnings = []
            for st in stations:
                th = st.thresholds or {}
                peak = max(station_level[st.id]) if station_level[st.id] else 0.0
                lv = _warning_level_for(st, station_flow[st.id][station_level[st.id].index(peak)] if station_level[st.id] else 0.0, peak) if station_level[st.id] else ""
                if lv:
                    thr = th.get(lv, 0.0)
                    pending_warnings.append({
                        "target_type": "station", "target_id": st.id,
                        "target_name": st.name, "kind": "water_level", "level": lv,
                        "value": round(peak, 2), "threshold": thr,
                        "message": f"{st.name}峰值水位{peak:.2f}m，达到{_LEVEL_TEXT[lv]}，注意防范"})

            # 风险区触发 → 响应展示 + 待写入转移台账
            pending_evacuations = []
            for z in zones:
                st_node = next((st for st in stations if st.node_id == z.node_id), None)
                if st_node:
                    lv = station_level[st_node.id]
                    if any(l >= z.high_level for l in lv):
                        trig = "强制转移"
                        lev = "red"
                    elif any(l >= z.low_level for l in lv):
                        trig = "预警提示"
                        lev = "orange"
                    else:
                        trig = ""
                        lev = ""
                    if trig:
                        warnings.append({
                            "target_type": "zone", "target_id": z.id, "name": z.name,
                            "level": lev, "trigger": trig,
                            "peak_level": round(max(lv), 2)})
                        pending_evacuations.append({
                            "zone_id": z.id, "zone_name": z.name,
                            "triggered_by": trig, "people": z.population})
                        evacuations.append({"zone": z.name, "trigger": trig,
                                            "people": z.population})

            # 过程线（纯派生数据，随运行整体重建）
            series_rows = []
            for st in stations:
                series_rows.append((st.node_id, "flow", st.name, station_flow[st.id]))
                series_rows.append((st.node_id, "level", st.name, station_level[st.id]))
                series_rows.append((st.node_id, "warnlevel", st.name,
                                    [{"v": i, "l": station_warn[st.id][i]} for i in range(total_steps)]))
            for r in res_list:
                series_rows.append((r.node_id, "inflow", r.name, res_inflow[r.id]))
                series_rows.append((r.node_id, "resout", r.name, res_outflow[r.id]))
                series_rows.append((r.node_id, "reslevel", r.name, res_level[r.id]))
                series_rows.append((r.node_id, "resstorage", r.name, res_storage[r.id]))

            if persist:
                # 派生数据 + 处置记录在同一事务提交：要么全部落库，要么整体回滚，
                # 异常重试时按幂等键upsert即可自动补齐缺口，不会产生重复台账。
                _replace_series(db, run.id, series_rows)
                # 调度方案按 run_id 幂等归档，供处置单审核与回写
                save_operation_plan(db, run.id, mode, scenarios, res_list,
                                    res_inflow, res_outflow, res_level, res_storage)
                if write_ledgers:
                    for w in pending_warnings:
                        _upsert_warning(db, run.id, w)
                    for ev in pending_evacuations:
                        _upsert_evacuation(db, run.id, ev)
                run.status = "done"
                db.commit()
        except Exception:
            if persist:
                db.rollback()
                if run is not None:
                    _mark_run_failed(db, run.id)
            raise

        return {
            "run_id": run.id if run else 0,
            "run_status": run.status if run else None,
            "mode": mode,
            "total_steps": total_steps,
            "dt_h": dt,
            "event": {"id": event.id, "name": event.name, "total_mm": event.total_mm,
                      "duration_h": event.duration_h},
            "nodes": [{"id": n.id, "name": n.name, "kind": n.kind,
                       "flow": [round(v, 2) for v in node_flow.get(n.id, [])]} for n in nodes],
            "stations": [{"id": st.id, "name": st.name, "node_id": st.node_id,
                          "flow": station_flow[st.id], "level": station_level[st.id],
                          "warning": station_warn[st.id]} for st in stations],
            "reservoirs": [{"id": r.id, "name": r.name, "node_id": r.node_id,
                            "inflow": res_inflow[r.id], "outflow": res_outflow[r.id],
                            "level": res_level[r.id], "storage": res_storage[r.id],
                            "gate": chosen["gate"].get(r.id, [0.0] * total_steps),
                            "crest_level": r.crest_level, "flood_level": r.flood_level,
                            "gate_max": r.gate_max}
                           for r in res_list],
            "scenarios": {
                "natural": {"objective": scenarios["natural"]["objective"]},
                "rule": {"objective": scenarios["rule"]["objective"]},
                "optimized": {"objective": scenarios["optimized"]["objective"]},
            },
            "warnings": warnings,
            "evacuations": evacuations,
        }


def _add_series(a: List[float], b: List[float]) -> List[float]:
    n = max(len(a), len(b))
    a = a + [0.0] * (n - len(a))
    b = b + [0.0] * (n - len(b))
    return [a[i] + b[i] for i in range(n)]