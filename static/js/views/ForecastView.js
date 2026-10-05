/* 视图：洪水预报 —— 情景选择 + 站点过程线 + 水库调蓄 + 预警 */
window.ForecastView = {
  name: "ForecastView",
  data() {
    return {
      events: [], selected: null, eventDetail: null,
      mode: "natural",
      result: null, running: false,
    };
  },
  computed: {
    labels() {
      if (!this.result) return [];
      const L = this.result.total_steps;
      const arr = [];
      for (let i = 0; i < L; i++) arr.push(`${String(i).padStart(2, "0")}h`);
      return arr;
    },
    peakSt() {
      if (!this.result) return null;
      let max = 0, name = "", nodeId = 0;
      this.result.stations.forEach(s => {
        const p = Math.max(...s.flow);
        if (p > max) { max = p; name = s.name; nodeId = s.node_id; }
      });
      return { max, name, nodeId };
    },
    maxWarnCount() {
      if (!this.result) return 0;
      let c = 0;
      this.result.stations.forEach(s => {
        s.warning.forEach(w => { if (w.l && w.l !== "") c++; });
      });
      return c;
    },
    scenarioObj() {
      if (!this.result || !this.result.scenarios) return {};
      return this.result.scenarios;
    },
  },
  methods: {
    async loadEvents() {
      this.events = await API.rainEvents();
      if (this.events.length && !this.selected) this.selected = this.events[0].id;
      this.loadDetail();
    },
    async loadDetail() {
      if (!this.selected) return;
      this.eventDetail = await API.rainEvent(this.selected);
    },
    async run() {
      if (!this.selected) return;
      this.running = true;
      window.app.showLoading("正在推演洪水预报过程…");
      try {
        this.result = await API.forecast(this.selected, this.mode);
        store.lastForecast = this.result;
        if (this.result.run_id) {
          const runs = await API.forecastRuns();
          store.runs = runs;
        }
        this.$emit("forecast-run", this.result);
      } catch (e) {
        window.app.toast("预报推演失败：" + e.message);
      } finally {
        window.app.hideLoading();
        this.running = false;
      }
    },
    resChart(res) {
      return Charts.lineChart({
        series: [
          { name: "入库", color: "#38b6ff", values: res.inflow },
          { name: "出流", color: "#f5b83d", values: res.outflow },
        ],
        labels: this.labels, unit: "m³/s", height: 210,
        hlines: [{ v: res.gate_max || 0, label: "闸门最大泄流", color: "#ff5c6c" }],
      });
    },
    levelChart(res) {
      return Charts.lineChart({
        series: [{ name: "库水位", color: "#2fd07f", values: res.level }],
        labels: this.labels, unit: "m", height: 200,
        hlines: [
          { v: res.flood_level, label: "汛限", color: "#f5b83d" },
          { v: res.crest_level, label: "防洪高", color: "#ff5c6c" },
        ],
      });
    },
    stFlowChart(s) {
      return Charts.lineChart({
        series: [{ name: "流量", color: "#38b6ff", values: s.flow }],
        labels: this.labels, unit: "m³/s", height: 200,
      });
    },
    stLevelChart(s) {
      const th = this.stThreshold(s);
      return Charts.lineChart({
        series: [{ name: "水位", color: "#2fd07f", values: s.level }],
        labels: this.labels, unit: "m", height: 200,
        hlines: [
          { v: th.blue, label: "蓝", color: "#38b6ff" },
          { v: th.yellow, label: "黄", color: "#f5b83d" },
          { v: th.orange, label: "橙", color: "#ff9f43" },
          { v: th.red, label: "红", color: "#ff4757" },
        ],
      });
    },
    stThreshold(s) {
      const map = store.mapData || {};
      const st = (map.stations || []).find(x => x.id === s.id);
      return (st && st.thresholds) || { blue: 0, yellow: 0, orange: 0, red: 0 };
    },
    rainChart() {
      if (!this.eventDetail) return "";
      return Charts.barChart(this.eventDetail.hyetograph || [], this.labels.slice(0, (this.eventDetail.hyetograph || []).length), { unit: "mm/h", color: "#37b6ff" });
    },
    warnSeries(s) {
      return s.warning;
    },
    scenarioCards() {
      const s = this.scenarioObj;
      return [
        { id: "natural", t: "天然工况", d: "无水库调蓄，洪水沿河道天然过流，反映最不利基线", obj: s.natural ? s.natural.objective : 0 },
        { id: "rule", t: "规则调度", d: "按入库量成比例控泄常规调度规则，人工操作人员依规程执行", obj: s.rule ? s.rule.objective : 0 },
        { id: "optimized", t: "联合优化", d: "削峰错峰寻优：倍率扫描+错时抬闸，最大化下游削峰能力", obj: s.optimized ? s.optimized.objective : 0 },
      ];
    },
    bestOf(id) {
      const s = this.scenarioObj;
      if (!s || !s.natural) return false;
      const vals = [s.natural, s.rule, s.optimized].map(x => x.objective);
      return s[id] && s[id].objective === Math.min(...vals);
    },
    warnBadge(s) {
      const th = this.stThreshold(s);
      const peak = Math.max(...s.level);
      let lv = "";
      if (peak >= th.red) lv = "red"; else if (peak >= th.orange) lv = "orange";
      else if (peak >= th.yellow) lv = "yellow"; else if (peak >= th.blue) lv = "blue";
      return lv ? `<span class="badge ${fmt.lvBadge(lv)}" style="margin-left:auto">峰值水位达${fmt.lvName(lv)}</span>` : `<span class="badge green" style="margin-left:auto">水位安全</span>`;
    },
  },
  mounted() { this.loadEvents(); },
  template: `
  <div class="page">
    <div class="page-title">洪水预报推演
      <span class="sub">降雨情景 → 产汇流 → 水库调度 → 断面过程 → 预警判定</span>
    </div>

    <!-- 参数区 -->
    <div class="panel">
      <div class="panel-head">预报情景与调度工况</div>
      <div class="panel-body" style="display:flex;gap:20px;flex-wrap:wrap;align-items:flex-end">
        <div class="field">
          <label>降雨情景</label>
          <select v-model="selected" @change="loadDetail" style="min-width:220px">
            <option v-for="e in events" :key="e.id" :value="e.id">{{ e.name }}（{{ fmt.num(e.total_mm,0) }}mm / {{ e.duration_h }}h）</option>
          </select>
        </div>
        <div class="field">
          <label>调度工况</label>
          <div style="display:flex;gap:8px">
            <button v-for="c in scenarioCards()" :key="c.id" class="btn sm" :class="{primary: mode===c.id}" @click="mode = c.id" :disabled="running">{{ c.t }}</button>
          </div>
        </div>
        <button class="btn primary" @click="run" :disabled="running || !selected">
          {{ running ? '推演中…' : '▶ 开始洪水预报' }}
        </button>
        <span v-if="eventDetail" style="font-size:12px;color:#7d95b4">重现期 {{ eventDetail.return_period }} · {{ eventDetail.note }}</span>
      </div>
    </div>

    <!-- 降雨 + 概览 -->
    <div v-if="result" class="row">
      <div class="col col-1">
        <div class="panel">
          <div class="panel-head">情景面雨量过程线</div>
          <div class="panel-body" v-html="rainChart()"></div>
        </div>
      </div>
      <div class="col col-1">
        <div class="panel">
          <div class="panel-head">调度工况对比·下游峰值 <span class="tag">目标：峰值最小</span></div>
          <div class="panel-body">
            <div v-for="c in scenarioCards()" :key="c.id" class="scene-card" :class="{active: mode===c.id}" @click="mode=c.id">
              <div class="t">{{ c.t }} <span class="badge" :class="bestOf(c.id) ? 'green' : 'gray'" v-if="c.obj">{{ bestOf(c.id) ? '最优' : '' }}</span></div>
              <div class="d">{{ c.d }}</div>
              <div class="o">下游峰值 <span class="mono">{{ fmt.num(c.obj,0) }}</span> m³/s</div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 站点过程 -->
    <div v-if="result">
      <div class="page-title" style="margin-top:6px">控制断面过程线 <span class="sub">峰值 {{ fmt.num(peakSt.max,0) }} m³/s · {{ peakSt.name }}</span></div>
      <div class="row">
        <div class="col col-2">
          <div class="panel" v-for="s in result.stations" :key="s.id">
            <div class="panel-head">{{ s.name }} · 流量 <span class="tag" v-if="peakSt.nodeId === s.node_id">峰值断面</span></div>
            <div class="panel-body" v-html="stFlowChart(s)"></div>
          </div>
        </div>
        <div class="col col-2">
          <div class="panel" v-for="s in result.stations" :key="s.id">
            <div class="panel-head">{{ s.name }} · 水位 {{ warnBadge(s) }}</div>
            <div class="panel-body" v-html="stLevelChart(s)"></div>
          </div>
        </div>
      </div>
    </div>

    <!-- 水库调蓄 -->
    <div v-if="result">
      <div class="page-title" style="margin-top:6px">水库调蓄演算 <span class="sub">入库 / 出流 / 库水位</span></div>
      <div class="row">
        <div class="col col-2">
          <div class="panel" v-for="r in result.reservoirs" :key="r.id">
            <div class="panel-head">{{ r.name }} · 调度出流过程</div>
            <div class="panel-body" v-html="resChart(r)"></div>
          </div>
        </div>
        <div class="col col-2">
          <div class="panel" v-for="r in result.reservoirs" :key="r.id">
            <div class="panel-head">{{ r.name }} · 库水位过程</div>
            <div class="panel-body" v-html="levelChart(r)"></div>
          </div>
        </div>
      </div>
    </div>

    <!-- 无结果提示 -->
    <div v-if="!result" class="panel" style="padding:60px;text-align:center;color:#7d95b4">
      <div style="font-size:38px;margin-bottom:12px">🌧</div>
      <div style="font-size:15px">选择上方降雨情景与调度工况，点击「开始洪水预报」进行推演</div>
      <div style="font-size:12px;margin-top:6px">系统将模拟产汇流、河网演算、水库调洪与预警判定全过程</div>
    </div>
  </div>`,
};