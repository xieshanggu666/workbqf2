/* 视图：调度方案 —— 水库群联合调度、闸门过程线、削峰对比 */
window.DispatchView = {
  name: "DispatchView",
  data() {
    return { result: null };
  },
  computed: {
    hasData() { return !!this.result; },
    scenarioObj() { return this.result ? this.result.scenarios : {}; },
    reduction() {
      if (!this.scenarioObj) return null;
      const nat = this.scenarioObj.natural ? this.scenarioObj.natural.objective : 0;
      const opt = this.scenarioObj.optimized ? this.scenarioObj.optimized.objective : 0;
      return nat > 0 ? ((nat - opt) / nat) * 100 : 0;
    },
    labels() { return this.result ? this.result.total_steps : 0; },
    bestVal() {
      const s = this.scenarioObj;
      if (!s || !s.natural) return 0;
      return Math.min(s.natural.objective, s.rule ? s.rule.objective : 0, s.optimized ? s.optimized.objective : 0);
    },
  },
  methods: {
    gateChart(res) {
      // 闸门开度：出流/泄流能力近似表示
      const gate = res.outflow.map((q) => Math.min(1, (q / (res.gate_max || 1))));
      return Charts.lineChart({
        series: [{ name: "闸门相对开度", color: "#f5b83d", values: gate }],
        labels: this.hourLabels(), unit: "", height: 170,
      });
    },
    hourLabels() {
      if (!this.result) return [];
      const a = [];
      for (let i = 0; i < this.result.total_steps; i++) a.push(`${String(i).padStart(2, "0")}h`);
      return a;
    },
    inflowChart(res) {
      return Charts.lineChart({
        series: [
          { name: "入库", color: "#38b6ff", values: res.inflow },
          { name: "出流(调度)", color: "#f5b83d", values: res.outflow },
        ],
        labels: this.hourLabels(), unit: "m³/s", height: 190,
      });
    },
    openDispatch(res) {
      this.$emit("open-dispatch");
    },
    scenarioCards() {
      const s = this.scenarioObj;
      return [
        { id: "natural", t: "天然工况", obj: s.natural ? s.natural.objective : 0 },
        { id: "rule", t: "规则调度", obj: s.rule ? s.rule.objective : 0 },
        { id: "optimized", t: "联合优化", obj: s.optimized ? s.optimized.objective : 0 },
      ];
    },
  },
  mounted() { this.result = store.lastForecast; },
  template: `
  <div class="page">
    <div class="page-title">联合调度方案
      <span class="sub">水库群削峰错峰调度 · 闸门运行过程 · 方案效果评估</span>
    </div>

    <div v-if="!hasData" class="panel" style="padding:60px;text-align:center;color:#7d95b4">
      <div style="font-size:38px;margin-bottom:12px">⚙</div>
      <div style="font-size:15px">尚未生成预报结果</div>
      <div style="font-size:12px;margin-top:6px">请先在「洪水预报」视图执行一次推演，调度方案将基于预报入库过程自动生成</div>
      <button class="btn primary sm" style="margin-top:16px" @click="$emit('open-dispatch')">去预报</button>
    </div>

    <template v-else>
      <!-- 方案总览 -->
      <div class="row">
        <div class="col col-3">
          <div class="panel">
            <div class="panel-head">调度方案对比评估</div>
            <div class="panel-body">
              <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:14px">
                <div class="scene-card" v-for="c in scenarioCards()" :key="c.id">
                  <div class="t">{{ c.t }}</div>
                  <div class="o">
                    下游峰值 <span class="mono" :style="{color: c.obj===bestVal ? '#2fd07f' : '#7d95b4'}">{{ fmt.num(c.obj, 0) }}</span> m³/s
                    <span class="badge green" v-if="c.obj===bestVal">最优</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
        <div class="col col-1">
          <div class="panel">
            <div class="panel-head">优化削峰效果</div>
            <div class="panel-body" style="text-align:center">
              <div style="font-size:40px;font-weight:800;color:#2fd07f">{{ fmt.num(reduction, 1) }}<small style="font-size:16px">%</small></div>
              <div style="font-size:12px;color:#7d95b4;margin-top:4px">联合优化相对天然工况的削峰率</div>
              <div style="font-size:11.5px;color:#7d95b4;margin-top:12px;text-align:left;line-height:1.8">
                ① 基于入库过程生成启发式闸门基线<br>
                ② 倍率扫描寻优削峰系数<br>
                ③ 下游水库错时抬闸实现错峰
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 各水库调度 -->
      <div class="page-title" style="margin-top:4px">水库调度运行方案</div>
      <div class="row">
        <div class="col col-2">
          <div class="panel" v-for="r in result.reservoirs" :key="r.id">
            <div class="panel-head">{{ r.name }} · 调度方案 <span class="tag">闸门相对开度过程</span></div>
            <div class="panel-body" v-html="gateChart(r)"></div>
          </div>
        </div>
        <div class="col col-2">
          <div class="panel" v-for="r in result.reservoirs" :key="r.id">
            <div class="panel-head">{{ r.name }} · 入库与调度出流</div>
            <div class="panel-body" v-html="inflowChart(r)"></div>
          </div>
        </div>
      </div>
    </template>
  </div>`,
};