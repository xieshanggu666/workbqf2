"""水文引擎单元测试：SCS-CN 产流 / 三角单位线 / 卷积汇流 / 马斯京根演算 / 河网拓扑。"""
import math

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models import RiverNode, RiverReach, SubBasin
from app.services.hydrology import (
    BasinTopology,
    compute_sub_basin,
    convolution,
    muskingum,
    scs_net_rain,
    triangular_uh,
)


def test_scs_net_rain_zero_below_initial_abstraction():
    # CN=100 => S=0, Ia=0，任何降雨全部产流
    assert scs_net_rain(10.0, 100.0) == pytest.approx(10.0)
    # CN=50 => S=254, Ia=50.8，小雨不产流
    assert scs_net_rain(20.0, 50.0) == 0.0


def test_scs_net_rain_known_value():
    # CN=75 => S=84.667, Ia=16.933；P=50 => Q=(50-16.933)^2/(50-16.933+84.667)
    s = 25400.0 / 75.0 - 254.0
    expected = (50 - 0.2 * s) ** 2 / (50 - 0.2 * s + s)
    assert scs_net_rain(50.0, 75.0) == pytest.approx(expected, rel=1e-6)


def test_scs_net_rain_monotonic():
    vals = [scs_net_rain(p, 70.0) for p in range(0, 201, 20)]
    assert vals == sorted(vals)


def test_triangular_uh_normalization():
    # 归一化后纵坐标之和 ≈ dt/3.6（1mm 净雨铺开后单位时段出流体积守恒）
    for lag in (0.5, 1.0, 2.0):
        uh = triangular_uh(dt_h=1.0, lag_hr=lag, total_steps=48)
        assert sum(uh) == pytest.approx(1.0 / 3.6, rel=1e-6)
        assert all(v >= 0 for v in uh)


def test_triangular_uh_single_peak():
    uh = triangular_uh(dt_h=1.0, lag_hr=1.0, total_steps=48)
    peak_idx = max(range(len(uh)), key=lambda i: uh[i])
    # 峰前单调上升、峰后单调下降
    assert all(uh[i] <= uh[i + 1] for i in range(peak_idx))
    assert all(uh[i] >= uh[i + 1] for i in range(peak_idx, len(uh) - 1))


def test_convolution_length_and_nonneg():
    rain = [5, 8, 3, 0, 0]
    uh = [0.1, 0.3, 0.2]
    out = convolution(rain, uh, total_steps=8)
    assert len(out) == 8
    assert all(v >= 0 for v in out)
    # 单时段净雨 + 单位线 => 单位线平移
    single = convolution([1.0, 0, 0, 0], uh, total_steps=8)
    assert single[:3] == pytest.approx([0.1, 0.3, 0.2])


def test_convolution_linearity():
    a = convolution([2.0, 3.0, 0], [0.2, 0.4, 0.1], total_steps=6)
    b = convolution([4.0, 6.0, 0], [0.2, 0.4, 0.1], total_steps=6)
    assert b == pytest.approx([2 * x for x in a])


def test_muskingum_coefficients_sum_to_one():
    dt = 1.0
    k, x = 2.0, 0.2
    denom = k * (1 - x) + 0.5 * dt
    c0 = (0.5 * dt - k * x) / denom
    c1 = (0.5 * dt + k * x) / denom
    c2 = (k * (1 - x) - 0.5 * dt) / denom
    assert c0 + c1 + c2 == pytest.approx(1.0)


def test_muskingum_steady_state():
    inflow = [100.0] * 24
    out = muskingum(inflow, k_hr=2.0, x_coef=0.2, dt_h=1.0)
    # 恒定入流下演算应稳定到同一出流（首时段允许过渡）
    assert out[-1] == pytest.approx(100.0, abs=1e-3)
    assert all(v >= 0 for v in out)


def test_muskingum_lag_shape():
    inflow = [0] * 5 + [200] * 5 + [0] * 10
    out = muskingum(inflow, k_hr=3.0, x_coef=0.1, dt_h=1.0)
    # 传播耗散：出流峰值应低于入流峰值且存在时间滞后
    assert max(out) < 200.0
    assert out.index(max(out)) > inflow.index(200)


def test_compute_sub_basin_volume_conservation():
    sub = SubBasin(name="t", area_km2=100.0, cn=85.0, lag_hr=1.0, outlet_node_id=1)
    hydro = [10.0] * 12
    dt = 1.0
    q = compute_sub_basin(hydro, sub, dt_h=dt, total_steps=48)
    assert len(q) == 48
    assert all(v >= 0 for v in q)
    # 出流总体积 ≈ 净雨总量 × 面积（mm·km² → m³），时段数足够时可近似守恒
    net_vol = sum(scs_net_rain(p, sub.cn) for p in hydro) * sub.area_km2 * 1000.0
    out_vol = sum(q) * dt * 3600.0
    assert out_vol == pytest.approx(net_vol, rel=0.02)


@pytest.fixture()
def topo_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add_all([
        RiverNode(id=1, name="源头A", kind="headwater"),
        RiverNode(id=2, name="汇合B", kind="junction"),
        RiverNode(id=3, name="控制断面C", kind="control"),
        RiverReach(id=1, name="r1", from_node_id=1, to_node_id=2),
        RiverReach(id=2, name="r2", from_node_id=2, to_node_id=3),
    ])
    db.commit()
    yield db
    db.close()


def test_basin_topology_order(topo_db):
    topo = BasinTopology(topo_db)
    # 上游节点必须先于下游节点
    assert topo.upstream_order == [1, 2, 3]
    assert topo.in_edges[3][0].from_node_id == 2
    assert topo.out_edges[1][0].to_node_id == 2


def test_basin_topology_fork(topo_db):
    db = topo_db
    db.add(RiverReach(id=3, name="r3", from_node_id=1, to_node_id=3))
    db.commit()
    topo = BasinTopology(db)
    order = topo.upstream_order
    assert order.index(1) < order.index(3)
    assert order.index(2) < order.index(3)
