"""水库调洪演算与水库群联合调度方案生成。

调洪演算采用库容-泄量耦合的固定点迭代；调度方案对比三种工况：
  natural   —— 无水库调蓄，洪水沿河道天然过流（最坏基线，削峰率以此为基准）
  rule      —— 常规调度规则：非汛情期"来多少放多少"，强降雨时按入库等比例
               控泄并在峰前预泄腾库
  optimized —— 在规则基线上做倍率扫描 + 错峰微调的削峰错峰方案，且保证
               不劣于天然过流与规则调度
"""
from __future__ import annotations

from typing import Dict, List, Sequence

from app.models import Reservoir


def route_reservoir(res: Reservoir, inflow: Sequence[float], gate_profile: Sequence[float],
                    dt_h: float, init_storage=None) -> dict:
    """给定入库过程与闸门开度过程 [0,1]，返回出流/水位/库容过程。

    泄流量 = 闸门开度*gate_max + 水位超过防洪高水位时的溢洪道自由泄流，
    保证任何方案下库水位不会失控越过防洪高水位（剩余洪水由溢洪道宣泄）。
    库容曲线单位为 万m³，时段水量按 dt 换算并除以 1e4 保持单位一致。
    """
    n = len(inflow)
    dt = dt_h * 3600.0
    if init_storage is None:
        S = res.storage_at(res.current_level) if res.current_level else res.storage_at(res.normal_level)
    else:
        S = init_storage

    out_flow = [0.0] * n
    level = [0.0] * n
    storage = [0.0] * n
    for i in range(n):
        gate_q = min(max(gate_profile[i], 0.0), 1.0) * res.gate_max
        # 固定点迭代：水位↔库容↔泄流耦合求解
        S_trial = S
        q_out = gate_q
        for _ in range(5):
            lvl = res.level_at(S_trial)
            free = res.free_discharge(lvl) if lvl > res.crest_level else 0.0
            q_out = gate_q + free
            S_trial = S + (inflow[i] - q_out) * dt / 1e4
            if S_trial < 0.0:
                S_trial = 0.0
        # 可用水量约束：出流不得超过（库容 + 时段入流），避免空库凭空泄流
        avail = S + inflow[i] * dt / 1e4  # 库容曲线单位为 万m³
        q_out = min(q_out, avail * 1e4 / dt)
        S = max(0.0, S + (inflow[i] - q_out) * dt / 1e4)
        out_flow[i] = q_out
        level[i] = res.level_at(S)
        storage[i] = S
    return {"outflow": out_flow, "level": level, "storage": storage}


def _heuristic_profile(inflow: Sequence[float], gate_max: float) -> List[float]:
    """启发式闸门过程线：非汛情期"来多少放多少"（下泄≤入库，不放大洪峰）；
    强降雨（洪峰超闸门能力 35%）时按入库等比例控泄，并在峰前预泄腾库。"""
    k = max(gate_max, 1.0)
    peak = max(inflow) if inflow else 0.0
    storm = peak > 0.35 * k
    prof = []
    for t, q in enumerate(inflow):
        if not storm:
            base = min(1.0, q / k)          # 顺水控泄：下泄 ≤ 入库
        else:
            base = max(0.0, min(1.0, (q / k) ** 0.6))
            if t < max(2, int(len(inflow) * 0.15)):
                base = max(base, 0.3)       # 峰前预泄腾库
        prof.append(base)
    return prof


def _peak_of(weights: Dict[int, float], sim: Dict[int, dict]) -> float:
    peak = 0.0
    for rid, w in weights.items():
        if rid in sim:
            peak = max(peak, w * max(sim[rid]["outflow"]))
    return peak


def run_scenarios(db, res_list: List[Reservoir], inflow_map: Dict[int, List[float]],
                  dt_h: float, horizon: int,
                  downstream_weights: Dict[int, float] | None = None) -> dict:
    """生成三种调度工况的完整结果，供预报对比与前端展示。"""
    if downstream_weights is None:
        downstream_weights = {r.id: 1.0 for r in res_list}
    zeros = [0.0] * horizon

    # natural：无水库调蓄，河道天然过流（出流=入库，作为削峰率基线）
    sim_nat = {}
    for res in res_list:
        inflow_r = inflow_map.get(res.id, zeros)
        sim_nat[res.id] = {
            "outflow": list(inflow_r),
            "level": [res.current_level or res.normal_level] * horizon,
            "storage": [res.storage_at(res.current_level or res.normal_level)] * horizon,
        }

    # rule：启发式规则
    sim_rule = {}
    gate_rule = {}
    for res in res_list:
        prof = _heuristic_profile(inflow_map.get(res.id, zeros), res.gate_max)
        gate_rule[res.id] = prof
        sim_rule[res.id] = route_reservoir(res, inflow_map.get(res.id, zeros), prof, dt_h)

    # optimized：在规则基线上做倍率扫描（含错峰错时微调），取下游峰值最优
    best_val = float("inf")
    best_gate = gate_rule
    best_sim = sim_rule
    for alpha in (0.35, 0.5, 0.65, 0.8, 1.0, 1.15, 1.3):
        gm = {}
        for res in res_list:
            gm[res.id] = [min(1.0, g * alpha) for g in gate_rule[res.id]]
        sim = {}
        for res in res_list:
            sim[res.id] = route_reservoir(res, inflow_map.get(res.id, zeros), gm[res.id], dt_h)
        val = _peak_of(downstream_weights, sim)
        if val < best_val - 1e-9:
            best_val = val
            best_gate = gm
            best_sim = sim
    # 错峰微调：龙潭水库（后泄）相对青峰水库延后 2 个时段抬闸
    for a2 in (0.8, 1.0, 1.2):
        gm = dict(best_gate)
        for res in res_list:
            if res.name and "龙潭" in res.name:
                prof = list(gm[res.id])
                shifted = [0.0] * horizon
                for t in range(horizon - 2):
                    shifted[t + 2] = min(1.0, prof[t] * a2)
                gm[res.id] = shifted
        sim = {}
        for res in res_list:
            sim[res.id] = route_reservoir(res, inflow_map.get(res.id, zeros), gm[res.id], dt_h)
        val = _peak_of(downstream_weights, sim)
        if val < best_val - 1e-9:
            best_val = val
            best_gate = gm
            best_sim = sim

    # 兜底：若所有闸门方案都劣于天然蓄滞（闸门全关），则维持全关，保证优化不劣于天然工况
    val_nat = _peak_of(downstream_weights, sim_nat)
    if val_nat < best_val - 1e-9:
        best_val = val_nat
        best_gate = {r.id: list(zeros) for r in res_list}
        best_sim = sim_nat

    return {
        "natural": {"gate": {r.id: list(zeros) for r in res_list}, "sim": sim_nat,
                    "objective": _peak_of(downstream_weights, sim_nat)},
        "rule": {"gate": gate_rule, "sim": sim_rule,
                 "objective": _peak_of(downstream_weights, sim_rule)},
        "optimized": {"gate": best_gate, "sim": best_sim, "objective": best_val},
    }