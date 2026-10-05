"""水库调洪演算与联合调度方案单元测试。"""
import math

import pytest

from app.models import Reservoir
from app.services.reservoir import route_reservoir, run_scenarios


def make_reservoir(res_id, name="测试水库", gate_max=1200.0, current_level=180.0,
                   normal_level=180.0, flood_level=182.0, crest_level=184.0):
    """小型水库：库容曲线 175→186m 约 300→1500 万m³，溢洪道 184m 以上最大 800m³/s。"""
    return Reservoir(
        id=res_id, name=name, node_id=res_id,
        normal_level=normal_level, flood_level=flood_level, crest_level=crest_level,
        storage_curve=[[175, 300], [180, 600], [182, 800], [184, 1000], [186, 1500]],
        discharge_curve=[[184, 0], [186, 800]],
        gate_max=gate_max, current_level=current_level,
        current_storage=600.0,
    )


def test_route_reservoir_no_inflow_no_gate():
    res = make_reservoir(1)
    r = route_reservoir(res, [0.0] * 10, [0.0] * 10, dt_h=1.0)
    assert all(q == 0 for q in r["outflow"])
    assert all(abs(l - res.current_level) < 1e-6 for l in r["level"])


def test_route_reservoir_equilibrium():
    # 恒定入流 + 闸门恒定开度（闸门泄流=入流）：出流最终应逼近入流（蓄泄平衡）
    res = make_reservoir(1, gate_max=2000.0)
    inflow = [500.0] * 24
    gate = [0.25] * 24  # 0.25 * 2000 = 500 m³/s
    r = route_reservoir(res, inflow, gate, dt_h=1.0)
    assert r["outflow"][-1] == pytest.approx(500.0, abs=2.0)
    assert all(v >= 0 for v in r["outflow"])


def test_route_reservoir_no_phantom_outflow_from_dry():
    # 空库 + 无上游补水时，即便闸门大开也不得凭空泄流（库容非负一致性）
    res = make_reservoir(1, gate_max=5000.0)
    inflow = [0.0, 0.0, 50.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    gate = [1.0] * 10
    r = route_reservoir(res, inflow, gate, dt_h=1.0)
    # 任一时刻库容非负
    assert all(r["storage"][i] >= -1e-6 for i in range(10))
    # 闸门泄流不得超过当前可用水量（库容+本时段入流），单位统一为 万m³
    for i in range(10):
        avail = (res.storage_at(res.current_level) if i == 0 else r["storage"][i - 1])
        avail += inflow[i] * 3600.0 / 1e4
        assert r["outflow"][i] * 3600.0 / 1e4 <= avail + 1e-6


def test_route_reservoir_level_never_negative():
    res = make_reservoir(1, gate_max=5000.0)
    r = route_reservoir(res, [2000.0] * 12, [1.0] * 12, dt_h=1.0)
    assert all(l >= 0 for l in r["level"])
    assert len(r["level"]) == len(r["outflow"]) == len(r["storage"]) == 12


def test_optimized_scenario_reduces_peak():
    res1 = make_reservoir(1, "青峰水库", gate_max=1200.0)
    res2 = make_reservoir(2, "龙潭水库", gate_max=900.0)
    # 典型双峰入库过程
    inflow1 = [200 + 900 * (math.sin(i / 2.0) ** 2) for i in range(24)]
    inflow2 = [150 + 700 * (math.sin(i / 2.0 + 0.5) ** 2) for i in range(24)]
    inflow1[12] = 2200.0  # 主峰
    inflow2[13] = 1800.0
    scenarios = run_scenarios(
        db=None,
        res_list=[res1, res2],
        inflow_map={1: inflow1, 2: inflow2},
        dt_h=1.0,
        horizon=24,
        downstream_weights={1: 1.0, 2: 1.0},
    )
    # 优化方案必须不劣于规则调度与天然蓄滞（削峰效果非负）
    assert scenarios["optimized"]["objective"] <= scenarios["rule"]["objective"] + 1e-9
    assert scenarios["optimized"]["objective"] <= scenarios["natural"]["objective"] + 1e-9
    assert scenarios["natural"]["objective"] > 0
    # 三种工况闸门开度均在 [0,1]
    for scenario in scenarios.values():
        for gates in scenario["gate"].values():
            assert all(0.0 <= g <= 1.0 + 1e-9 for g in gates)


def test_optimized_never_worse_than_natural():
    res = make_reservoir(1, gate_max=1000.0)
    inflow = [150.0] * 6 + [400.0] * 4 + [250.0] * 6 + [100.0] * 8
    scenarios = run_scenarios(None, [res], {1: inflow}, dt_h=1.0, horizon=24)
    nat = scenarios["natural"]["objective"]
    opt = scenarios["optimized"]["objective"]
    # 削峰效果至少不劣于天然工况，且闸门开启后峰值应更低
    assert opt <= nat * (1.0 + 1e-9)
