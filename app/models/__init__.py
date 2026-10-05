from datetime import datetime

from sqlalchemy import (Column, DateTime, Float, ForeignKey, Integer, JSON,
                        String, Text, UniqueConstraint)

from app.core.database import Base


class SubBasin(Base):
    """子流域（降雨产流单元）"""
    __tablename__ = "sub_basins"

    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)
    area_km2 = Column(Float, nullable=False)      # 面积 km²
    cn = Column(Float, nullable=False)            # SCS 曲线数（土壤-植被综合）
    lag_hr = Column(Float, nullable=False, default=1.0)  # 汇流滞后时间 h
    outlet_node_id = Column(Integer, nullable=False)      # 汇入河网节点
    x = Column(Float, default=0)
    y = Column(Float, default=0)


class RiverNode(Base):
    """河网节点：源头 / 水库 / 汇合点 / 控制断面 / 河口"""
    __tablename__ = "river_nodes"

    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)
    kind = Column(String(24), nullable=False, default="junction")  # headwater/reservoir/junction/control/outlet
    desc = Column(String(200), default="")
    x = Column(Float, default=0)
    y = Column(Float, default=0)


class RiverReach(Base):
    """河段（马斯京根演算单元）"""
    __tablename__ = "river_reaches"

    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)
    from_node_id = Column(Integer, nullable=False)
    to_node_id = Column(Integer, nullable=False)
    length_km = Column(Float, default=1.0)
    slope = Column(Float, default=0.001)
    k_hr = Column(Float, default=1.0)     # 马斯京根 K（传播时间 h）
    x_coef = Column(Float, default=0.25)  # 马斯京根 X


class RainStation(Base):
    """雨量站"""
    __tablename__ = "rain_stations"

    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)
    node_id = Column(Integer, nullable=False)
    x = Column(Float, default=0)
    y = Column(Float, default=0)


class WaterStation(Base):
    """水位站（含分级预警阈值）"""
    __tablename__ = "water_stations"

    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)
    node_id = Column(Integer, nullable=False)
    x = Column(Float, default=0)
    y = Column(Float, default=0)
    thresholds = Column(JSON, default=dict)  # {"blue":m,"yellow":m,"orange":m,"red":m}


class Reservoir(Base):
    """水库（库容曲线 + 泄流能力 + 调度规则）"""
    __tablename__ = "reservoirs"

    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)
    node_id = Column(Integer, nullable=False)
    normal_level = Column(Float, nullable=False)     # 正常蓄水位 m
    flood_level = Column(Float, nullable=False)      # 汛限水位 m
    crest_level = Column(Float, nullable=False)      # 防洪高水位 m
    storage_curve = Column(JSON, nullable=False)     # [[level,storage万m³],...]
    discharge_curve = Column(JSON, nullable=False)   # [[level,泄流m³/s],...]
    gate_max = Column(Float, default=0.0)            # 闸门最大泄流 m³/s
    current_level = Column(Float, default=0.0)
    current_storage = Column(Float, default=0.0)
    active = Column(Integer, default=1)
    x = Column(Float, default=0)
    y = Column(Float, default=0)

    def storage_at(self, level: float) -> float:
        curve = sorted(self.storage_curve or [], key=lambda p: p[0])
        if level <= curve[0][0]:
            return curve[0][1]
        if level >= curve[-1][0]:
            return curve[-1][1]
        for i in range(len(curve) - 1):
            l0, s0 = curve[i]
            l1, s1 = curve[i + 1]
            if l0 <= level <= l1:
                return s0 + (s1 - s0) * (level - l0) / (l1 - l0)
        return curve[-1][1]

    def level_at(self, storage: float) -> float:
        curve = sorted(self.storage_curve or [], key=lambda p: p[1])
        if storage <= curve[0][1]:
            return curve[0][0]
        if storage >= curve[-1][1]:
            return curve[-1][0]
        for i in range(len(curve) - 1):
            st0, lv0 = curve[i][1], curve[i][0]
            st1, lv1 = curve[i + 1][1], curve[i + 1][0]
            if st0 <= storage <= st1:
                return lv0 + (lv1 - lv0) * (storage - st0) / (st1 - st0)
        return curve[-1][0]

    def free_discharge(self, level: float) -> float:
        """溢洪道自由泄流（无闸门控制时）"""
        curve = sorted(self.discharge_curve or [], key=lambda p: p[0])
        if level <= curve[0][0]:
            return 0.0
        if level >= curve[-1][0]:
            return curve[-1][1]
        for i in range(len(curve) - 1):
            l0, q0 = curve[i]
            l1, q1 = curve[i + 1]
            if l0 <= level <= l1:
                return q0 + (q1 - q0) * (level - l0) / (l1 - l0)
        return curve[-1][1]


class RainfallEvent(Base):
    """降雨情景（设计暴雨过程线）"""
    __tablename__ = "rainfall_events"

    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)
    return_period = Column(String(32), default="")   # 重现期描述
    duration_h = Column(Integer, default=24)
    total_mm = Column(Float, nullable=False)
    hyetograph = Column(JSON, nullable=False)        # 逐小时面雨量 mm/h
    note = Column(String(200), default="")


class ForecastRun(Base):
    """洪水预报运行（同一情景×工况归并为同一次运行，重复触发幂等复用）"""
    __tablename__ = "forecast_runs"
    __table_args__ = (
        UniqueConstraint("event_id", "mode", name="uq_forecast_run_event_mode"),
    )

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, nullable=False)
    mode = Column(String(24), default="natural")     # natural / rule / optimized
    created_at = Column(DateTime, default=datetime.now)
    status = Column(String(24), default="running")   # running / done / failed


class ForecastSeries(Base):
    """预报过程线（节点×变量×时段）"""
    __tablename__ = "forecast_series"
    __table_args__ = (
        UniqueConstraint("run_id", "node_id", "kind", "name",
                         name="uq_series_run_node_kind"),
    )

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, nullable=False)
    node_id = Column(Integer, default=0)
    kind = Column(String(24), nullable=False)        # rain/netrain/inflow/outflow/flow/level
    name = Column(String(64), default="")
    values = Column(JSON, nullable=False)            # [v0,v1,...] 逐小时

    @classmethod
    def save(cls, db, run_id, node_id, kind, name, values):
        rec = cls(run_id=run_id, node_id=node_id, kind=kind, name=name, values=list(values))
        db.add(rec)
        return rec


class OperationPlan(Base):
    """水库群联合调度方案（按 run_id 幂等：一次预报运行至多一份方案）"""
    __tablename__ = "operation_plans"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_operation_plan_run"),
    )

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, nullable=False)
    name = Column(String(64), nullable=False)
    objective = Column(String(200), default="")
    peak_flow = Column(Float, default=0.0)           # 下游断面峰值 m³/s
    peak_ratio = Column(Float, default=0.0)          # 削峰率 %
    storage_gain = Column(Float, default=0.0)        # 蓄水增量 万m³
    gate_schedule = Column(JSON, default=dict)       # {reservoir_id: {hour: 泄流系数}}
    reservoir_outcome = Column(JSON, default=dict)   # {reservoir_id: {name,peak_level,final_level,final_storage,peak_outflow}}


class DisposalOrder(Base):
    """联合防汛处置协同单：调度员发起 → 预警值守审核 → 应急资源调度 → 转移负责人执行 → 完成闭环。

    一次预报运行至多发起一单（run_id 唯一）。审核通过后，方案快照
    (plan_snapshot) 回写水库工况、预警与转移台账；审核通过后转移负责人分配
    避难点容量、物资管理员分配车辆与物资、指挥员确认资源调度令（状态
    approved → resourced，可直接跳过资源调度环节进入执行，兼容历史流转）；
    历史预报运行缺少方案/过程线时，发起环节自动补算补齐，兼容历史运行记录。
    状态机：initiated（待审核）→ reviewing 通过 → approved（待执行）
    → 资源协同（resourced 资源已调度，可选）→ executing → executed（执行中）
    → completing → completed（已闭环）。
    """
    __tablename__ = "disposal_orders"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_disposal_order_run"),
    )

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, nullable=False)
    title = Column(String(128), nullable=False)
    status = Column(String(16), default="initiated")  # initiated/approved/resourced/executed/completed
    plan_snapshot = Column(JSON, default=dict)        # 审核归档的调度方案快照
    remark = Column(String(200), default="")

    initiated_by = Column(String(64), default="")     # 发起：调度员
    reviewed_by = Column(String(64), default="")      # 审核：预警值守
    resourced_by = Column(String(64), default="")     # 资源调度令：指挥员
    executed_by = Column(String(64), default="")      # 执行：转移负责人
    completed_by = Column(String(64), default="")     # 完成：转移负责人

    initiated_at = Column(DateTime, default=datetime.now)
    reviewed_at = Column(DateTime, nullable=True)
    resourced_at = Column(DateTime, nullable=True)
    executed_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class WarningRecord(Base):
    """预警记录（按 run_id+目标 幂等关联预报运行；run_id 为 NULL 的是历史遗留记录）"""
    __tablename__ = "warning_records"
    __table_args__ = (
        UniqueConstraint("run_id", "target_type", "target_id", "kind",
                         name="uq_warning_run_target"),
    )

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, nullable=True)               # 关联预报运行；历史记录为 NULL
    disposal_id = Column(Integer, nullable=True)          # 处置单审核回写关联；历史记录为 NULL
    target_type = Column(String(24), default="station")   # station/reservoir/zone
    target_id = Column(Integer, default=0)
    target_name = Column(String(64), default="")
    kind = Column(String(24), default="water_level")      # water_level/flow/rain
    level = Column(String(16), default="blue")            # blue/yellow/orange/red
    value = Column(Float, default=0.0)
    threshold = Column(Float, default=0.0)
    message = Column(String(200), default="")
    created_at = Column(DateTime, default=datetime.now)
    status = Column(String(16), default="active")         # active/handling/cleared


class FloodZone(Base):
    """淹没风险区（含转移路线）"""
    __tablename__ = "flood_zones"

    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)
    node_id = Column(Integer, nullable=False)
    population = Column(Integer, default=0)
    area_desc = Column(String(200), default="")
    risk_level = Column(String(16), default="medium")     # high/medium/low
    low_level = Column(Float, default=0.0)                # 预警启动水位
    high_level = Column(Float, default=0.0)               # 强制转移水位
    route = Column(JSON, default=list)                    # [[x,y,label],...] 转移路线
    x = Column(Float, default=0)
    y = Column(Float, default=0)


class EvacuationRecord(Base):
    """转移行动记录（按 run_id+风险区 幂等关联预报运行；run_id 为 NULL 的是历史遗留记录）"""
    __tablename__ = "evacuation_records"
    __table_args__ = (
        UniqueConstraint("run_id", "zone_id", name="uq_evacuation_run_zone"),
    )

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, nullable=True)           # 关联预报运行；历史记录为 NULL
    disposal_id = Column(Integer, nullable=True)      # 处置单审核回写关联；历史记录为 NULL
    zone_id = Column(Integer, nullable=False)
    zone_name = Column(String(64), default="")
    triggered_by = Column(String(64), default="")
    people = Column(Integer, default=0)
    status = Column(String(24), default="pending")        # pending/moving/safe
    shelter_id = Column(Integer, nullable=True)           # 避难容量分配回写；历史记录为 NULL
    shelter_name = Column(String(64), default="")         # 分配避难点名称冗余
    created_at = Column(DateTime, default=datetime.now)


class Shelter(Base):
    """应急避难点（容量由协同调度按处置单分配，跨处置单统一占用校验）"""
    __tablename__ = "shelters"

    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)
    address = Column(String(200), default="")
    capacity = Column(Integer, nullable=False, default=0)   # 可安置容量（人）
    contact = Column(String(64), default="")
    active = Column(Integer, default=1)
    x = Column(Float, default=0)
    y = Column(Float, default=0)


class Vehicle(Base):
    """应急车辆（运力池，处置单分配后跨单互斥，执行时发车、完成后归队）"""
    __tablename__ = "vehicles"

    id = Column(Integer, primary_key=True)
    plate = Column(String(32), nullable=False)
    kind = Column(String(24), default="bus")          # bus 大巴 / truck 货车 / ambulance 救护
    seats = Column(Integer, default=0)                # 核载（人/车）
    team = Column(String(64), default="")             # 所属车队/单位
    status = Column(String(16), default="standby")    # standby/dispatched/departed/returned
    active = Column(Integer, default=1)


class Supply(Base):
    """应急物资（库存按处置单分配预占，指挥员确认调度令时实际出库）"""
    __tablename__ = "supplies"

    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)
    unit = Column(String(16), default="份")
    stock = Column(Integer, default=0)                # 当前库存
    safety_stock = Column(Integer, default=0)         # 安全库存（仅预警提示，不阻断）
    active = Column(Integer, default=1)


class ShelterAssignment(Base):
    """避难点容量分配：处置单 × 避难点 × 转移台账（同一单同一区同一点至多一条）"""
    __tablename__ = "shelter_assignments"
    __table_args__ = (
        UniqueConstraint("disposal_id", "evacuation_id", "shelter_id",
                         name="uq_shelter_assign"),
    )

    id = Column(Integer, primary_key=True)
    disposal_id = Column(Integer, nullable=False)
    evacuation_id = Column(Integer, nullable=False)
    shelter_id = Column(Integer, nullable=False)
    people = Column(Integer, default=0)
    note = Column(String(200), default="")
    created_by = Column(String(64), default="")       # 分配：转移负责人
    created_at = Column(DateTime, default=datetime.now)


class VehicleDispatch(Base):
    """车辆分配：处置单 × 车辆（一辆车同一处置单至多一条，跨处置单互斥）"""
    __tablename__ = "vehicle_dispatches"
    __table_args__ = (
        UniqueConstraint("disposal_id", "vehicle_id", name="uq_vehicle_dispatch"),
    )

    id = Column(Integer, primary_key=True)
    disposal_id = Column(Integer, nullable=False)
    vehicle_id = Column(Integer, nullable=False)
    evacuation_id = Column(Integer, nullable=True)    # 指定服务的转移台账；空=机动运力
    shuttles = Column(Integer, default=1)             # 计划往返趟次（运力=核载×趟次）
    note = Column(String(200), default="")
    created_by = Column(String(64), default="")       # 分配：物资管理员
    created_at = Column(DateTime, default=datetime.now)


class SupplyAllocation(Base):
    """物资分配：处置单 × 物资 × 转移台账（同一单同一区同一物资至多一条）"""
    __tablename__ = "supply_allocations"
    __table_args__ = (
        UniqueConstraint("disposal_id", "evacuation_id", "supply_id",
                         name="uq_supply_alloc"),
    )

    id = Column(Integer, primary_key=True)
    disposal_id = Column(Integer, nullable=False)
    evacuation_id = Column(Integer, nullable=True)    # 指定转移台账；空=单级公用物资
    supply_id = Column(Integer, nullable=False)
    quantity = Column(Integer, default=0)
    issued_quantity = Column(Integer, default=0)      # 调度令确认时已出库数量（重复确认只出增量）
    note = Column(String(200), default="")
    created_by = Column(String(64), default="")       # 分配：物资管理员
    created_at = Column(DateTime, default=datetime.now)
