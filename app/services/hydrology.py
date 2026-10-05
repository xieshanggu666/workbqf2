"""流域水文模拟引擎：SCS-CN 产流 + 三角单位线汇流 + 马斯京根河道演算。

模拟对象为相对独立的子流域（面积小、汇流时间为小时级），因此对每个子流域
用"降雨 → SCS 净雨 → 单位线汇流"得到出流过程，再沿河网做马斯京根演算与节点叠加。
"""
from __future__ import annotations

from typing import List, Sequence

from sqlalchemy.orm import Session

from app.models import RainfallEvent, RiverNode, RiverReach, SubBasin


def scs_net_rain(P: float, CN: float) -> float:
    """SCS 曲线数法计算单时段净雨深 (mm)。S = (25400/CN) - 254。"""
    S = (25400.0 / max(CN, 1.0)) - 254.0
    Ia = 0.2 * S
    if P <= Ia:
        return 0.0
    return (P - Ia) ** 2 / (P - Ia + S)


def triangular_uh(dt_h: float, lag_hr: float, total_steps: int) -> List[float]:
    """生成三角单位线（单位净雨 1mm，dt 时段单宽出流），保证纵坐标之和=1。"""
    tp = 0.5 + 0.6 * lag_hr            # 峰现时间 h
    tfall = 0.6 * tp                    # 退水段时长 h
    Tb = tp + tfall                     # 总历时 h
    n_tp = max(1, round(tp / dt_h))
    n_Tb = max(n_tp + 1, round(Tb / dt_h))
    n_Tb = min(n_Tb, total_steps)
    qp = 2.0 / Tb                       # 无量纲峰值（三角线面积=1）
    uh = []
    for i in range(n_Tb):
        t = dt_h * (i + 0.5)
        if t <= tp:
            uh.append(qp * t / tp)
        else:
            uh.append(max(0.0, qp * (Tb - t) / (Tb - tp)))
    s = sum(uh) or 1.0
    # 单位线折算为：净雨 1mm 铺在流域上 → 出流 (m³/s per mm)
    return [(v / s) * (dt_h / 3.6) for v in uh]


def convolution(rain_mm: Sequence[float], uh: Sequence[float], total_steps: int) -> List[float]:
    """单位线卷积：逐时段净雨与单位线线性叠加，得到出流过程 m³/s。"""
    out = [0.0] * total_steps
    for k, P in enumerate(rain_mm):
        if P <= 0:
            continue
        for j, u in enumerate(uh):
            t = k + j
            if t < total_steps:
                out[t] += P * u
    return out


def compute_sub_basin(hydrograph: Sequence[float], sub: SubBasin, dt_h: float, total_steps: int) -> List[float]:
    """子流域净雨 → 出流过程（m³/s）。area_km2 将 mm→m³/s 缩放。"""
    net = [scs_net_rain(p, sub.cn) for p in hydrograph]
    uh = triangular_uh(dt_h, sub.lag_hr, total_steps)
    q = convolution(net, uh, total_steps)
    area_scale = sub.area_km2 * 1000.0 / (dt_h * 3600.0)  # mm·km² 上净雨换算
    # 卷积输出基于 "1mm 净雨/单位线"，上一步已按三角形面积=1 归一，
    # 但宽幅折算是 mm*km² → 流量，需再乘面积/时段换算：Q[m³/s]=net[mm]*A[km²]*1000/3600/dt
    total_net_mm = sum(net)
    total_q = sum(q)
    scale = (total_net_mm * sub.area_km2 * 1000.0 / (dt_h * 3600.0)) / total_q if total_q > 0 else 0.0
    return [v * scale for v in q]


def muskingum(inflow: Sequence[float], k_hr: float, x_coef: float, dt_h: float, initial=0.0) -> List[float]:
    """马斯京根掩藏算法演算河段出流。必须满足 C0+C1+C2=1 且 k>dt/2 保证稳定。"""
    k = max(k_hr, dt_h * 0.51)
    x = min(max(x_coef, 0.0), 0.5)
    denom = k * (1 - x) + 0.5 * dt_h
    c0 = (0.5 * dt_h - k * x) / denom
    c1 = (0.5 * dt_h + k * x) / denom
    c2 = (k * (1 - x) - 0.5 * dt_h) / denom
    out = [0.0] * len(inflow)
    prev_out = initial
    for i, q_in in enumerate(inflow):
        out_i = c0 * q_in + c1 * (inflow[i - 1] if i > 0 else q_in) + c2 * prev_out
        out[i] = max(out_i, 0.0)
        prev_out = out_i
    return out


class BasinTopology:
    """河网拓扑预计算：节点出边、入边、上游子流域与节点排序。"""

    def __init__(self, db: Session):
        self.nodes: dict[int, RiverNode] = {n.id: n for n in db.query(RiverNode).all()}
        self.reaches = db.query(RiverReach).all()
        self.out_edges: dict[int, list] = {}
        self.in_edges: dict[int, list] = {}
        for r in self.reaches:
            self.out_edges.setdefault(r.from_node_id, []).append(r)
            self.in_edges.setdefault(r.to_node_id, []).append(r)
        # 上游节点拓扑排序（供正向演算）
        self.upstream_order = self._topo_order()

    def _topo_order(self) -> list:
        # DFS 后序遍历，其逆序即为 DAG 的正拓扑序（每条边 from 在 to 之前）
        order: list = []
        temp: set = set()
        perm: set = set()

        def visit(nid):
            if nid in perm:
                return
            if nid in temp:
                return  # 环保护
            temp.add(nid)
            for e in self.out_edges.get(nid, []):
                visit(e.to_node_id)
            temp.discard(nid)
            perm.add(nid)
            order.append(nid)

        for nid in self.nodes:
            visit(nid)
        return list(reversed(order))