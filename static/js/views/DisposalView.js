/* 视图：联合防汛处置协同 —— 调度员发起 / 预警值守审核 / 资源协同调度 / 转移负责人执行 / 完成闭环 */
window.DisposalView = {
  name: "DisposalView",
  data() {
    return {
      runs: [],
      orders: [],
      current: null,          // 当前选中处置单详情
      role: "dispatcher",     // 当前扮演角色
      operatorNames: { dispatcher: "张调度", duty: "李值守", transfer_lead: "王转移",
                       supply_manager: "陈物资", commander: "赵指挥" },
      loading: false,
      actionNote: "",
      // 资源台账（避难点/车辆/物资实时可用量）
      shelters: [],
      vehicles: [],
      supplies: [],
      // 协同分配表单
      shForm: { evacuation_id: null, shelter_id: null, people: 0 },
      vhForm: { vehicle_id: null, evacuation_id: null, shuttles: 1 },
      spForm: { supply_id: null, evacuation_id: null, quantity: 1 },
      // 接收新一轮预报
      refreshOpen: false,
      refreshRunId: null,
      refreshNote: "",
      refreshBusy: false,
    };
  },
  computed: {
    roles() {
      return [
        { id: "dispatcher", name: "调度员" },
        { id: "duty", name: "预警值守" },
        { id: "transfer_lead", name: "转移负责人" },
        { id: "supply_manager", name: "物资管理员" },
        { id: "commander", name: "指挥员" },
      ];
    },
    roleText() {
      return { dispatcher: "调度员", duty: "预警值守", transfer_lead: "转移负责人",
               supply_manager: "物资管理员", commander: "指挥员" };
    },
    steps() {
      return [
        { key: "initiated", name: "发起", role: "调度员", actor: "initiated_by", at: "initiated_at" },
        { key: "approved", name: "审核", role: "预警值守", actor: "reviewed_by", at: "reviewed_at" },
        { key: "resourced", name: "资源调度", role: "指挥员", actor: "resourced_by", at: "resourced_at" },
        { key: "executed", name: "执行", role: "转移负责人", actor: "executed_by", at: "executed_at" },
        { key: "completed", name: "完成", role: "转移负责人", actor: "completed_by", at: "completed_at" },
      ];
    },
    availableRuns() {
      const map = {};
      this.orders.forEach(o => { map[o.run_id] = o; });
      return this.runs.map(r => ({ ...r, order: map[r.id] || null }));
    },
    // 当前处置单可接收的新一轮预报：已完成推演、非当前轮次、未挂接其它处置单
    refreshableRuns() {
      if (!this.current) return [];
      return this.runs.filter(r => r.status === "done"
        && r.id !== this.current.run_id
        && (!r.disposal || r.disposal.id === this.current.id));
    },
    isExecuted() { return this.current && this.current.status === "executed"; },
    res() { return this.current && this.current.resources ? this.current.resources : null; },
    inResourcePhase() {
      // executed 执行中仅支持增量追加（新一轮预报新增风险区时补配）
      return this.current && ["approved", "resourced", "executed"].includes(this.current.status);
    },
    canEditResource() {
      // 执行中只可追加、不可撤回（撤回按钮按行隐藏）
      return this.inResourcePhase;
    },
    evacMap() {
      const m = {};
      (this.res ? this.res.evacuations : []).forEach(e => { m[e.evacuation_id] = e; });
      return m;
    },
  },
  methods: {
    modeText(m) { return { natural: "天然过流", rule: "规则调度", optimized: "联合优化" }[m] || m; },
    statusBadge(st) {
      return { initiated: "orange", approved: "blue", resourced: "cyan",
               executed: "yellow", completed: "green" }[st] || "gray";
    },
    statusText(st) {
      return { initiated: "待审核", approved: "待执行", resourced: "资源已调度",
               executed: "执行中", completed: "已完成" }[st] || st;
    },
    stepIndex(status) {
      // 资源调度为可选环节：未确认调度令直接执行时跳过第 3 步
      const idx = { initiated: 0, approved: 1, resourced: 2, executed: 3, completed: 4 };
      return idx[status] ?? -1;
    },
    stepReached(stepKey, status) {
      const order = ["initiated", "approved", "resourced", "executed", "completed"];
      // executed 且无资源调度令（resourced_by 为空）视为跳过「资源调度」步
      if (stepKey === "resourced" && ["executed", "completed"].includes(status)
          && !this.current.resourced_by) return false;
      return order.indexOf(status) >= order.indexOf(stepKey);
    },
    vehicleKind(k) { return { bus: "大巴", truck: "货车", ambulance: "救护车" }[k] || k; },
    vehicleBadge(st) {
      return { standby: "gray", dispatched: "blue", departed: "yellow", returned: "green" }[st] || "gray";
    },
    vehicleStatus(st) {
      return { standby: "待命", dispatched: "已派出", departed: "执行中", returned: "已归队" }[st] || st;
    },
    async load() {
      this.loading = true;
      try {
        const [runs, orders, shelters, vehicles, supplies] = await Promise.all([
          API.forecastRuns(), API.disposals(),
          API.shelters(), API.vehicles(), API.supplies()]);
        this.runs = runs;
        this.orders = orders;
        this.shelters = shelters;
        this.vehicles = vehicles;
        this.supplies = supplies;
        if (this.current) {
          this.current = orders.find(o => o.id === this.current.id) || null;
          this.resetForms();
        }
      } catch (e) {
        window.app.showToast("加载协同处置数据失败：" + e.message);
      } finally {
        this.loading = false;
      }
    },
    selectOrder(o) {
      this.current = o;
      this.actionNote = "";
      this.resetForms();
    },
    backToList() { this.current = null; },
    resetForms() {
      const firstEvac = this.res && this.res.evacuations.length ? this.res.evacuations[0].evacuation_id : null;
      const firstShelter = this.shelters.length ? this.shelters[0].id : null;
      const firstVehicle = this.vehicles.find(v => v.available);
      const firstSupply = this.supplies.length ? this.supplies[0].id : null;
      this.shForm = { evacuation_id: firstEvac, shelter_id: firstShelter, people: 0 };
      this.vhForm = { vehicle_id: firstVehicle ? firstVehicle.id : null, evacuation_id: firstEvac, shuttles: 6 };
      this.spForm = { supply_id: firstSupply, evacuation_id: firstEvac, quantity: 10 };
    },
    canAct(action) {
      if (!this.current) return false;
      const need = {
        review: { from: "initiated", role: "duty" },
        execute: { from: ["approved", "resourced"], role: "transfer_lead" },
        complete: { from: "executed", role: "transfer_lead" },
      }[action];
      return need.from.includes(this.current.status) && this.role === need.role;
    },
    async initiate(run) {
      try {
        const o = await API.initiateDisposal(run.id, {
          operator: this.operatorNames.dispatcher, role: "dispatcher",
        });
        window.app.showToast(`处置单 #${o.id} 已发起，待预警值守审核`);
        await this.load();
        this.selectOrder(o);
      } catch (e) { window.app.showToast("发起失败：" + e.message); }
    },
    async act(action) {
      const id = this.current.id;
      const body = { operator: this.operatorNames[this.role], role: this.role };
      const note = this.actionNote.trim();
      if (action === "review") body.opinion = note;
      if (action === "execute") body.note = note;
      if (action === "complete") body.summary = note;
      const api = { review: API.reviewDisposal, execute: API.executeDisposal,
                    complete: API.completeDisposal }[action];
      const verb = { review: "审核通过", execute: "启动执行", complete: "确认完成" }[action];
      try {
        const o = await api(id, body);
        window.app.showToast(`处置单 #${id} ${verb}：${this.statusText(o.status)}`);
        this.actionNote = "";
        await this.load();
        this.selectOrder(this.orders.find(x => x.id === id) || o);
      } catch (e) { window.app.showToast(`${verb}失败：` + e.message); }
    },
    gateSummary(plan) {
      if (!plan || !plan.peak_flow) return "—";
      return `下游峰值 ${fmt.num(plan.peak_flow, 0)} m³/s · 削峰 ${fmt.num(plan.peak_ratio, 1)}% · 拦蓄 ${fmt.num(plan.storage_gain, 0)} 万m³`;
    },
    remarks() {
      return (this.current && this.current.remark ? this.current.remark.split("\n") : []);
    },
    // ---------------- 应急资源协同 ----------------
    async submitShelter() {
      const f = this.shForm;
      if (!f.evacuation_id || !f.shelter_id || !(f.people > 0)) {
        window.app.showToast("请选择转移区、避难点并填写安置人数");
        return;
      }
      try {
        const plan = await API.assignShelter(this.current.id, {
          evacuation_id: f.evacuation_id, shelter_id: f.shelter_id, people: Number(f.people),
          operator: this.operatorNames.transfer_lead, role: "transfer_lead",
        });
        window.app.showToast("避难点容量已分配");
        await this.afterResourceChange(plan);
      } catch (e) { window.app.showToast("分配失败：" + e.message); }
    },
    async submitVehicle() {
      const f = this.vhForm;
      if (!f.vehicle_id || !(f.shuttles > 0)) {
        window.app.showToast("请选择车辆并填写计划趟次");
        return;
      }
      try {
        const body = { vehicle_id: f.vehicle_id, shuttles: Number(f.shuttles),
                       evacuation_id: f.evacuation_id || null,
                       operator: this.operatorNames.supply_manager, role: "supply_manager" };
        const plan = await API.assignVehicle(this.current.id, body);
        window.app.showToast("车辆已派配");
        await this.afterResourceChange(plan);
      } catch (e) { window.app.showToast("派车失败：" + e.message); }
    },
    async submitSupply() {
      const f = this.spForm;
      if (!f.supply_id || !(f.quantity > 0)) {
        window.app.showToast("请选择物资并填写数量");
        return;
      }
      try {
        const body = { supply_id: f.supply_id, quantity: Number(f.quantity),
                       evacuation_id: f.evacuation_id || null,
                       operator: this.operatorNames.supply_manager, role: "supply_manager" };
        const plan = await API.assignSupply(this.current.id, body);
        window.app.showToast("物资已分配（待调度令出库）");
        await this.afterResourceChange(plan);
      } catch (e) { window.app.showToast("分配失败：" + e.message); }
    },
    async afterResourceChange(plan) {
      // 用后端返回的最新资源方案更新当前单据，并刷新资源台账可用量
      const [shelters, vehicles, supplies, orders] = await Promise.all(
        [API.shelters(), API.vehicles(), API.supplies(), API.disposals()]);
      this.shelters = shelters; this.vehicles = vehicles; this.supplies = supplies;
      this.orders = orders;
      this.current = orders.find(o => o.id === this.current.id) || this.current;
    },
    async releaseRow(kind, rowId) {
      const api = { shelter: API.releaseShelter, vehicle: API.releaseVehicle,
                    supply: API.releaseSupply }[kind];
      try {
        await api(this.current.id, rowId, this.role);
        window.app.showToast("已撤销该分配");
        await this.load();
        this.current = this.orders.find(o => o.id === this.current.id) || null;
      } catch (e) { window.app.showToast("撤销失败：" + e.message); }
    },
    async confirmOrder() {
      try {
        const plan = await API.confirmResources(this.current.id, {
          operator: this.operatorNames.commander, role: "commander",
          order_text: this.actionNote.trim(),
        });
        window.app.showToast(this.isExecuted
          ? `增量调度令已确认（追加车辆即派即发，物资只出增量）`
          : `资源调度令已确认（${plan.coverage.shelter_seats} 人位 · ${plan.coverage.vehicle_seats} 座位）`);
        this.actionNote = "";
        await this.load();
        this.current = this.orders.find(o => o.id === this.current.id) || null;
      } catch (e) { window.app.showToast("调度令确认失败：" + e.message); }
    },
    covPct(got, need) { return need > 0 ? Math.min(100, Math.round(got / need * 100)) : 0; },
    // ---------------- 接收新一轮预报 ----------------
    canRefresh() {
      return this.current && this.role === "dispatcher"
        && ["approved", "resourced", "executed"].includes(this.current.status);
    },
    openRefresh() {
      if (!this.canRefresh()) return;
      this.refreshRunId = this.refreshableRuns.length ? this.refreshableRuns[0].id : null;
      this.refreshNote = "";
      this.refreshOpen = true;
    },
    closeRefresh() { this.refreshOpen = false; this.refreshBusy = false; },
    runLabel(r) {
      return `#${r.id} ${r.event_name} · ${this.modeText(r.mode)}`;
    },
    async submitRefresh() {
      if (!this.refreshRunId) { window.app.showToast("请选择新一轮预报运行"); return; }
      this.refreshBusy = true;
      try {
        const o = await API.refreshDisposalForecast(this.current.id, {
          new_run_id: Number(this.refreshRunId),
          operator: this.operatorNames.dispatcher, role: "dispatcher",
          note: this.refreshNote.trim(),
        });
        const d = o.refresh_delta || {};
        const w = d.warnings || {}, e = d.evacuations || {};
        window.app.showToast(
          `已滚动到第 ${o.round} 轮预报：预警延续${w.carried || 0}/新增${w.added || 0}/解除${w.retired || 0}`
          + `，转移延续${e.carried || 0}/新增${e.added || 0}/解除${e.retired || 0}；人工状态与已出库物资、执行中车辆保持不变`);
        this.refreshOpen = false;
        await this.load();
        this.current = this.orders.find(x => x.id === o.id) || this.current;
      } catch (err) { window.app.showToast("接收新一轮预报失败：" + err.message); }
      finally { this.refreshBusy = false; }
    },
  },
  mounted() { this.load(); },
  template: `
  <div class="page">
    <div class="page-title">联合防汛处置协同
      <span class="sub">发起 · 审核 · 避难容量/车辆/物资协同调度 · 执行 · 闭环销警</span>
      <button class="btn sm" style="margin-left:auto" @click="load">刷新</button>
    </div>

    <!-- 角色切换 -->
    <div class="panel">
      <div class="panel-body" style="display:flex;gap:14px;align-items:center;flex-wrap:wrap">
        <span style="font-size:12.5px;color:#7d95b4">当前值守角色</span>
        <button v-for="r in roles" :key="r.id" class="btn sm"
                :class="{primary: role===r.id}" @click="role=r.id">{{ r.name }}</button>
        <div class="field" style="margin-left:auto">
          <label>操作人（电子签名）</label>
          <input v-model="operatorNames[role]" class="role-input" style="min-width:160px"/>
        </div>
      </div>
    </div>

    <!-- 列表视图 -->
    <template v-if="!current">
      <div class="row">
        <div class="col col-1">
          <div class="panel">
            <div class="panel-head">可发起处置的预报运行 <span class="tag">一次运行一单 · 重复发起幂等</span></div>
            <div class="panel-body nopad">
              <table class="grid">
                <thead><tr><th>运行</th><th>降雨情景</th><th>工况</th><th>处置单</th><th></th></tr></thead>
                <tbody>
                  <tr v-for="r in availableRuns" :key="r.id">
                    <td class="mono">#{{ r.id }}</td>
                    <td>{{ r.event_name }}</td>
                    <td><span class="badge gray">{{ modeText(r.mode) }}</span></td>
                    <td>
                      <span v-if="r.order" class="badge" :class="statusBadge(r.order.status)">
                        #{{ r.order.id }} {{ statusText(r.order.status) }}
                      </span>
                      <span v-else class="badge gray">未发起</span>
                    </td>
                    <td style="text-align:right">
                      <button v-if="r.order" class="btn sm" @click="selectOrder(r.order)">查看</button>
                      <button v-else class="btn sm primary" :disabled="role!=='dispatcher'"
                              @click="initiate(r)">发起处置</button>
                    </td>
                  </tr>
                  <tr v-if="!runs.length">
                    <td colspan="5" style="text-align:center;color:#7d95b4;padding:26px">
                      暂无预报运行，请先在「洪水预报」视图执行推演
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>

      <div class="panel" v-if="orders.length">
        <div class="panel-head">处置单台账 <span class="tag">{{ orders.length }} 单</span></div>
        <div class="panel-body nopad">
          <table class="grid">
            <thead><tr><th>单号</th><th>标题</th><th>工况</th><th>状态</th><th>资源覆盖</th><th>预警/转移</th><th>发起时间</th><th></th></tr></thead>
            <tbody>
              <tr v-for="o in orders" :key="o.id" style="cursor:pointer" @click="selectOrder(o)">
                <td class="mono">#{{ o.id }}</td>
                <td>{{ o.title }}</td>
                <td>{{ o.mode_text }}</td>
                <td><span class="badge" :class="statusBadge(o.status)">{{ o.status_text }}</span></td>
                <td>
                  <span v-if="o.resource_summary && o.resource_summary.shelter_seats"
                        class="badge" :class="o.resource_summary.ready ? 'green' : 'orange'">
                    {{ o.resource_summary.shelter_seats }}人位 / {{ o.resource_summary.vehicle_seats }}座 / {{ o.resource_summary.supply_kinds }}类
                  </span>
                  <span v-else class="badge gray">未调度</span>
                </td>
                <td class="num">{{ o.linked_warnings }} / {{ o.linked_evacuations }}</td>
                <td style="font-size:11.5px;color:#7d95b4">{{ fmt.time(o.initiated_at) }}</td>
                <td style="text-align:right"><button class="btn sm" @click.stop="selectOrder(o)">详情</button></td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>

    <!-- 详情视图 -->
    <template v-else>
      <div class="panel">
        <div class="panel-body">
          <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
            <button class="btn sm" @click="backToList">← 返回</button>
            <div style="font-size:16px;font-weight:700">处置单 #{{ current.id }} · {{ current.title }}</div>
            <span class="badge" :class="statusBadge(current.status)">{{ current.status_text }}</span>
            <span class="badge cyan" v-if="current.round > 1">第 {{ current.round }} 轮预报</span>
            <button v-if="canRefresh()" class="btn sm primary" @click="openRefresh">
              ↻ 接收新一轮预报
            </button>
            <span style="margin-left:auto;font-size:12px;color:#7d95b4">
              预报运行 #{{ current.run_id }} · {{ current.event_name }} · {{ current.mode_text }}
            </span>
          </div>
        </div>
      </div>

      <!-- 五态流转 -->
      <div class="panel">
        <div class="panel-head">协同流转 <span class="tag">发起 → 审核 → 资源调度（可选）→ 执行 → 完成</span></div>
        <div class="panel-body">
          <div class="flow-steps">
            <template v-for="(s, i) in steps" :key="s.key">
              <div class="flow-step"
                   :class="{done: stepReached(s.key, current.status),
                            active: current.status===s.key,
                            skipped: s.key==='resourced' && !stepReached('resourced', current.status) && stepIndex(current.status) > 2}">
                <div class="dot">{{ i + 1 }}</div>
                <div class="meta">
                  <div class="name">{{ s.name }}</div>
                  <div class="role">{{ s.role }}</div>
                  <div class="who" v-if="current[s.actor]">{{ current[s.actor] }} · {{ fmt.time(current[s.at]) }}</div>
                </div>
              </div>
              <div class="flow-arrow" v-if="i < steps.length - 1">→</div>
            </template>
          </div>

          <!-- 当前角色待办 -->
          <div style="margin-top:16px;display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap">
            <div class="field" style="flex:1;min-width:260px" v-if="canAct('review')||canAct('execute')||canAct('complete')">
              <label>{{ canAct('review') ? '审核意见' : canAct('execute') ? '执行说明' : '完成小结' }}（可选）</label>
              <input v-model="actionNote" :placeholder="canAct('review') ? '如：同意按方案调度，密切监视白水渡水位' : '记录处置情况…'"/>
            </div>
            <div class="field" style="flex:1;min-width:260px" v-else-if="role==='commander' && inResourcePhase && res && res.evacuations.length">
              <label>资源调度令（可选）</label>
              <input v-model="actionNote" placeholder="如：18:00 前完成转移，物资随车下发"/>
            </div>
            <button v-if="canAct('review')" class="btn primary" @click="act('review')">✓ 审核通过并回写台账</button>
            <button v-if="role==='commander' && inResourcePhase && res && res.evacuations.length"
                    class="btn primary" @click="confirmOrder">
              ⌘ {{ isExecuted ? '确认增量资源调度令（只出增量）' : '确认资源调度令并回写进度' }}
            </button>
            <button v-if="canAct('execute')" class="btn primary" @click="act('execute')">▶ 启动转移执行</button>
            <button v-if="canAct('complete')" class="btn primary" @click="act('complete')">✔ 确认完成闭环</button>
            <span v-if="current.status!=='completed' && !(canAct('review')||canAct('execute')||canAct('complete'))
                         && !(role==='commander' && inResourcePhase && res && res.evacuations.length)"
                  style="font-size:12.5px;color:#7d95b4">
              当前角色（{{ roleText[role] }}）本环节无待办，可切换角色继续流转
            </span>
            <span v-if="current.status==='completed'" class="badge green">处置已闭环</span>
          </div>
        </div>
      </div>

      <!-- 应急资源与避难点协同调度（审核通过后） -->
      <template v-if="res && (stepIndex(current.status) >= 1)">
        <!-- 覆盖总览 -->
        <div class="stats">
          <div class="stat blue"><div class="k">需转移人口</div><div class="v">{{ res.coverage.people }}<small>人</small></div></div>
          <div class="stat green"><div class="k">已分配避难容量</div><div class="v">{{ res.coverage.shelter_seats }}<small>人位</small></div></div>
          <div class="stat amber"><div class="k">已组织运力</div><div class="v">{{ res.coverage.vehicle_seats }}<small>座位</small></div></div>
          <div class="stat" :class="res.coverage.ready ? 'green' : ''">
            <div class="k">物资（已分/出库）</div>
            <div class="v">{{ res.coverage.supply_kinds }}<small>类 · {{ res.coverage.supply_quantity }}</small></div>
          </div>
          <div class="stat" :style="res.coverage.ready ? '' : '--c:#ff9f43'">
            <div class="k">协同覆盖</div>
            <div class="v" :style="res.coverage.ready ? 'color:var(--ok)' : 'color:var(--orange-lv)'">
              {{ res.coverage.ready ? '就绪' : '缺口' }}
            </div>
          </div>
        </div>

        <!-- 各转移区容量/运力覆盖 -->
        <div class="panel">
          <div class="panel-head">转移区资源覆盖 <span class="tag">转移负责人分容量 · 物资管理员配运力</span></div>
          <div class="panel-body nopad">
            <table class="grid">
              <thead><tr><th>风险区</th><th>需转移</th><th>避难点容量</th><th>覆盖率</th><th>车辆运力（含机动）</th><th>指定避难点</th><th>转移状态</th></tr></thead>
              <tbody>
                <tr v-for="e in res.evacuations" :key="e.evacuation_id">
                  <td>{{ e.zone_name }}</td>
                  <td class="num">{{ e.people }} 人</td>
                  <td class="num">
                    <span :style="e.shelter_ready ? 'color:var(--ok)' : 'color:var(--orange-lv)'">{{ e.shelter_seats }}</span>
                  </td>
                  <td>
                    <div class="cov-bar"><div class="cov-fill" :class="e.shelter_ready ? 'ok' : 'gap'"
                         :style="{width: covPct(e.shelter_seats, e.people) + '%'}"></div></div>
                  </td>
                  <td class="num">
                    <span :style="e.vehicle_ready ? 'color:var(--ok)' : 'color:var(--orange-lv)'">
                      {{ e.vehicle_seats + e.pool_seats }}（机动 {{ e.pool_seats }}）
                    </span>
                  </td>
                  <td>{{ e.shelter_name || '—' }}</td>
                  <td><span class="badge" :class="{orange:e.status==='pending',blue:e.status==='moving',green:e.status==='safe'}">
                    {{ {pending:'待转移',moving:'转移中',safe:'已安全'}[e.status] || e.status }}
                  </span></td>
                </tr>
                <tr v-if="!res.evacuations.length">
                  <td colspan="7" style="text-align:center;color:#7d95b4;padding:22px">
                    本次运行无转移台账（历史运行补算不回造台账），可直接启动执行
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div class="row">
          <!-- 转移负责人：避难点容量 -->
          <div class="col col-1">
            <div class="panel">
              <div class="panel-head">避难点容量分配 <span class="tag">转移负责人</span></div>
              <div class="panel-body">
                <div v-if="role==='transfer_lead' && inResourcePhase && res.evacuations.length" class="res-form">
                  <select v-model.number="shForm.evacuation_id">
                    <option v-for="e in res.evacuations" :key="e.evacuation_id" :value="e.evacuation_id">
                      {{ e.zone_name }}（{{ e.people }}人）
                    </option>
                  </select>
                  <select v-model.number="shForm.shelter_id">
                    <option v-for="s in shelters" :key="s.id" :value="s.id" :disabled="s.available<=0">
                      {{ s.name }} · 可用 {{ s.available }}/{{ s.capacity }}
                    </option>
                  </select>
                  <input type="number" min="1" v-model.number="shForm.people" placeholder="安置人数"/>
                  <button class="btn sm primary" @click="submitShelter">分配</button>
                </div>
                <div v-if="isExecuted && res.evacuations.length" style="font-size:11.5px;color:#ffb061;margin:4px 0">
                  执行中：仅支持为新一轮预报新增/缺口风险区追加容量，已占用容量不可撤回
                </div>
                <table class="grid" style="margin-top:10px">
                  <thead><tr><th>避难点</th><th>服务风险区</th><th>人数</th><th>操作人</th><th></th></tr></thead>
                  <tbody>
                    <tr v-for="a in res.shelters" :key="a.id">
                      <td>{{ a.shelter_name }}</td>
                      <td>{{ evacMap[a.evacuation_id] ? evacMap[a.evacuation_id].zone_name : '—' }}</td>
                      <td class="num">{{ a.people }}</td>
                      <td style="font-size:11.5px;color:#7d95b4">{{ a.created_by }}</td>
                      <td style="text-align:right">
                        <button v-if="role==='transfer_lead' && inResourcePhase && !isExecuted" class="btn xs danger"
                                @click="releaseRow('shelter', a.id)">撤销</button>
                      </td>
                    </tr>
                    <tr v-if="!res.shelters.length"><td colspan="5" style="color:#7d95b4;text-align:center;padding:16px">尚未分配避难点</td></tr>
                  </tbody>
                </table>
              </div>
            </div>
          </div>

          <!-- 物资管理员：车辆 -->
          <div class="col col-1">
            <div class="panel">
              <div class="panel-head">车辆运力分配 <span class="tag">物资管理员 · 跨单互斥</span></div>
              <div class="panel-body">
                <div v-if="role==='supply_manager' && inResourcePhase && res.evacuations.length" class="res-form">
                  <select v-model.number="vhForm.vehicle_id">
                    <option v-for="v in vehicles" :key="v.id" :value="v.id" :disabled="!v.available">
                      {{ v.plate }} {{ v.kind_text }}{{ v.available ? ' · '+v.seats+'座' : ' · 占用中' }}
                    </option>
                  </select>
                  <select v-model.number="vhForm.evacuation_id">
                    <option :value="null">机动运力（全区）</option>
                    <option v-for="e in res.evacuations" :key="e.evacuation_id" :value="e.evacuation_id">
                      {{ e.zone_name }}
                    </option>
                  </select>
                  <input type="number" min="1" v-model.number="vhForm.shuttles" title="计划往返趟次"/>
                  <button class="btn sm primary" @click="submitVehicle">派车</button>
                </div>
                <div v-if="isExecuted && res.evacuations.length" style="font-size:11.5px;color:#ffb061;margin:4px 0">
                  执行中：追加车辆即派即发，执行中车辆不可撤回
                </div>
                <table class="grid" style="margin-top:10px">
                  <thead><tr><th>车辆</th><th>类型</th><th>服务</th><th>趟次</th><th>运力</th><th></th></tr></thead>
                  <tbody>
                    <tr v-for="d in res.vehicles" :key="d.id">
                      <td>{{ d.plate }}</td>
                      <td>{{ vehicleKind(d.kind) }}</td>
                      <td>{{ d.evacuation_id && evacMap[d.evacuation_id] ? evacMap[d.evacuation_id].zone_name : '机动' }}</td>
                      <td class="num">{{ d.shuttles }}</td>
                      <td class="num">{{ d.capacity }} 座</td>
                      <td style="text-align:right">
                        <button v-if="role==='supply_manager' && inResourcePhase && !isExecuted" class="btn xs danger"
                                @click="releaseRow('vehicle', d.id)">撤回</button>
                      </td>
                    </tr>
                    <tr v-if="!res.vehicles.length"><td colspan="6" style="color:#7d95b4;text-align:center;padding:16px">尚未派出车辆</td></tr>
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>

        <div class="row">
          <!-- 物资管理员：物资 -->
          <div class="col col-1">
            <div class="panel">
              <div class="panel-head">应急物资分配 <span class="tag">物资管理员 · 调度令确认后出库</span></div>
              <div class="panel-body">
                <div v-if="role==='supply_manager' && inResourcePhase && res.evacuations.length" class="res-form">
                  <select v-model.number="spForm.supply_id">
                    <option v-for="s in supplies" :key="s.id" :value="s.id">
                      {{ s.name }} · 库存{{ s.stock }}{{ s.unit }} · 可分{{ s.available }}{{ s.unit }}
                    </option>
                  </select>
                  <select v-model.number="spForm.evacuation_id">
                    <option :value="null">处置单公用</option>
                    <option v-for="e in res.evacuations" :key="e.evacuation_id" :value="e.evacuation_id">{{ e.zone_name }}</option>
                  </select>
                  <input type="number" min="1" v-model.number="spForm.quantity"/>
                  <button class="btn sm primary" @click="submitSupply">分配</button>
                </div>
                <div v-if="isExecuted && res.evacuations.length" style="font-size:11.5px;color:#ffb061;margin:4px 0">
                  执行中：仅支持增量追加，已出库物资不回滚，指挥员再确认时只出增量
                </div>
                <table class="grid" style="margin-top:10px">
                  <thead><tr><th>物资</th><th>投向</th><th>数量</th><th>出库状态</th><th></th></tr></thead>
                  <tbody>
                    <tr v-for="a in res.supplies" :key="a.id">
                      <td>{{ a.supply_name }}</td>
                      <td>{{ a.evacuation_id && evacMap[a.evacuation_id] ? evacMap[a.evacuation_id].zone_name : '公用' }}</td>
                      <td class="num">{{ a.quantity }} {{ a.unit }}</td>
                      <td><span class="badge" :class="a.issued ? 'green' : 'orange'">{{ a.issued ? '已出库' : '待出库' }}</span>
                        <span class="badge cyan" v-if="a.issued_quantity > 0 && !a.issued">已出 {{ a.issued_quantity }}</span>
                      </td>
                      <td style="text-align:right">
                        <button v-if="role==='supply_manager' && inResourcePhase && !isExecuted && !a.issued" class="btn xs danger"
                                @click="releaseRow('supply', a.id)">撤销</button>
                      </td>
                    </tr>
                    <tr v-if="!res.supplies.length"><td colspan="5" style="color:#7d95b4;text-align:center;padding:16px">尚未分配物资</td></tr>
                  </tbody>
                </table>
              </div>
            </div>
          </div>

          <!-- 资源台账实时状态 -->
          <div class="col col-1">
            <div class="panel">
              <div class="panel-head">应急资源台账 <span class="tag">跨处置单实时占用</span></div>
              <div class="panel-body" style="max-height:340px;overflow-y:auto">
                <div style="font-size:12px;font-weight:600;color:#b9cbe2;margin:4px 0 6px">避难点</div>
                <div v-for="s in shelters" :key="'s'+s.id" class="res-pool">
                  <span>{{ s.name }}</span>
                  <span class="num mono">{{ s.available }}/{{ s.capacity }}</span>
                </div>
                <div style="font-size:12px;font-weight:600;color:#b9cbe2;margin:12px 0 6px">车辆</div>
                <div v-for="v in vehicles" :key="'v'+v.id" class="res-pool">
                  <span>{{ v.plate }} · {{ v.kind_text }}（{{ v.seats }}座）</span>
                  <span class="badge" :class="vehicleBadge(v.status)">{{ vehicleStatus(v.status) }}</span>
                </div>
                <div style="font-size:12px;font-weight:600;color:#b9cbe2;margin:12px 0 6px">物资</div>
                <div v-for="s in supplies" :key="'p'+s.id" class="res-pool">
                  <span>{{ s.name }}</span>
                  <span>
                    <span class="badge" v-if="s.low" style="margin-right:6px">低于安全库存</span>
                    <span class="num mono">{{ s.available }}/{{ s.stock }} {{ s.unit }}</span>
                  </span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </template>

      <!-- 审核方案快照 + 回写情况 -->
      <div class="row" v-if="current.plan">
        <div class="col col-2">
          <div class="panel">
            <div class="panel-head">审核归档调度方案 <span class="tag">{{ current.plan.plan_name }}</span></div>
            <div class="panel-body">
              <div style="font-size:13px;color:#b9cbe2;margin-bottom:10px">{{ current.plan.objective }}</div>
              <div class="stats" style="grid-template-columns:repeat(3,1fr)">
                <div class="stat blue"><div class="k">下游峰值</div><div class="v">{{ fmt.num(current.plan.peak_flow,0) }}<small>m³/s</small></div></div>
                <div class="stat green"><div class="k">削峰率</div><div class="v">{{ fmt.num(current.plan.peak_ratio,1) }}<small>%</small></div></div>
                <div class="stat amber"><div class="k">拦蓄水量</div><div class="v">{{ fmt.num(current.plan.storage_gain,0) }}<small>万m³</small></div></div>
              </div>
              <table class="grid" style="margin-top:12px">
                <thead><tr><th>水库</th><th>洪峰水位(m)</th><th>末水位(m)</th><th>末库容(万m³)</th><th>峰值出流(m³/s)</th></tr></thead>
                <tbody>
                  <tr v-for="r in current.plan.reservoirs" :key="r.id">
                    <td>{{ r.name }}</td>
                    <td class="num mono">{{ fmt.num(r.peak_level,2) }}</td>
                    <td class="num mono">{{ fmt.num(r.final_level,2) }}</td>
                    <td class="num mono">{{ fmt.num(r.final_storage,1) }}</td>
                    <td class="num mono">{{ fmt.num(r.peak_outflow,1) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
        <div class="col col-1">
          <div class="panel">
            <div class="panel-head">台账回写情况 <span class="tag">审核/调度令/闭环联动</span></div>
            <div class="panel-body" style="display:flex;flex-direction:column;gap:12px;font-size:13px">
              <div class="writeback-item">
                <span class="badge green" v-if="stepIndex(current.status) >= 1">已回写</span>
                <span class="badge gray" v-else>待审核</span>
                <span>水库工况按方案末水位/末库容更新（{{ current.plan.reservoirs.length }} 座）</span>
              </div>
              <div class="writeback-item">
                <span class="badge green" v-if="stepIndex(current.status) >= 1">已挂接</span>
                <span class="badge gray" v-else>待审核</span>
                <span>预警台账 {{ current.linked_warnings }} 条关联处置单</span>
              </div>
              <div class="writeback-item">
                <span class="badge green" v-if="stepIndex(current.status) >= 1">已挂接</span>
                <span class="badge gray" v-else>待审核</span>
                <span>转移台账 {{ current.linked_evacuations }} 处关联处置单</span>
              </div>
              <div class="writeback-item">
                <span class="badge green" v-if="current.status==='resourced' || stepIndex(current.status) >= 3">已回写</span>
                <span class="badge gray" v-else>待调度令</span>
                <span>避难点容量回写转移进度 · 预警进入处置中 · 物资出库</span>
              </div>
              <div class="writeback-item">
                <span class="badge green" v-if="current.status==='completed'">已闭环</span>
                <span class="badge gray" v-else>待完成</span>
                <span>转移全部到位 · 关联预警统一销警 · 车辆归队</span>
              </div>
            </div>
          </div>
          <div class="panel" v-if="remarks().length">
            <div class="panel-head">处置记录</div>
            <div class="panel-body" style="font-size:12.5px;color:#b9cbe2;line-height:1.9">
              <div v-for="(line, i) in remarks()" :key="i">{{ line }}</div>
            </div>
          </div>
          <div class="panel" v-if="current.plan && current.plan.rounds && current.plan.rounds.length">
            <div class="panel-head">预报轮次演进 <span class="tag">第 {{ current.round }} 轮进行中</span></div>
            <div class="panel-body nopad">
              <table class="grid">
                <thead><tr><th>轮次</th><th>降雨情景</th><th>工况</th><th>下游峰值</th><th>削峰率</th><th>拦蓄</th></tr></thead>
                <tbody>
                  <tr v-for="r in current.plan.rounds" :key="r.round">
                    <td><span class="badge gray">第{{ r.round }}轮</span></td>
                    <td>{{ r.event_name }}</td>
                    <td>{{ modeText(r.mode) }}</td>
                    <td class="num mono">{{ fmt.num(r.peak_flow, 0) }}</td>
                    <td class="num mono">{{ fmt.num(r.peak_ratio, 1) }}%</td>
                    <td class="num mono">{{ fmt.num(r.storage_gain, 0) }}</td>
                  </tr>
                  <tr style="background:rgba(64,158,255,.08)">
                    <td><span class="badge blue">第{{ current.round }}轮</span></td>
                    <td>{{ current.event_name }}</td>
                    <td>{{ current.mode_text }}</td>
                    <td class="num mono">{{ fmt.num(current.plan.peak_flow, 0) }}</td>
                    <td class="num mono">{{ fmt.num(current.plan.peak_ratio, 1) }}%</td>
                    <td class="num mono">{{ fmt.num(current.plan.storage_gain, 0) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- 接收新一轮预报弹窗 -->
    <div v-if="refreshOpen" class="modal-mask" @click.self="closeRefresh">
      <div class="modal">
        <div class="modal-head">
          接收新一轮预报
          <button class="modal-close" @click="closeRefresh">×</button>
        </div>
        <div class="modal-body">
          <p style="font-size:12.5px;color:#b9cbe2;line-height:1.9;margin:0 0 12px">
            将处置单 #{{ current.id }} 滚动绑定到新的预报运行：调度方案滚动更新；
            预警/转移按站点与风险区<b>增量迁移</b>（等级、峰值、受威胁人数刷新，
            <b style="color:#7dd3a0">处置中/已销警/转移中/已到位等人工状态保留</b>）；
            已分配避难点容量沿用，新风险区形成缺口需补配；
            <b style="color:#7dd3a0">已出库物资不回滚、执行中车辆不动</b>，
            指挥员再确认调度令时仅出增量。
          </p>
          <div class="field">
            <label>新一轮预报运行</label>
            <select v-model.number="refreshRunId">
              <option v-for="r in refreshableRuns" :key="r.id" :value="r.id">{{ runLabel(r) }}</option>
            </select>
            <div v-if="!refreshableRuns.length" style="font-size:12px;color:#ff9f43;margin-top:6px">
              暂无可接收的运行：请先在「洪水预报」执行其它情景/工况推演（当前轮次与已挂接其它处置单的运行不可选）
            </div>
          </div>
          <div class="field" style="margin-top:10px">
            <label>调度说明（可选）</label>
            <input v-model="refreshNote" placeholder="如：上游台风路径修正，按新一轮落雨区增补转移"/>
          </div>
        </div>
        <div class="modal-foot">
          <button class="btn" @click="closeRefresh">取消</button>
          <button class="btn primary" :disabled="refreshBusy || !refreshRunId" @click="submitRefresh">
            {{ refreshBusy ? "滚动中…" : "确认接收并增量调整" }}
          </button>
        </div>
      </div>
    </div>
  </div>`,
};
