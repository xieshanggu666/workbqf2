/* 视图：实时监测 —— 概览指标 + 水库/雨量站状态 + 预警看板 */
window.MonitorView = {
  name: "MonitorView",
  data() {
    return { ov: null, map: null, loading: false, tick: 0 };
  },
  computed: {
    resList() { return this.map ? this.map.reservoirs : []; },
    stations() { return this.map ? this.map.stations : []; },
    rainStations() { return this.map ? this.map.rain_stations : []; },
    zones() { return this.map ? this.map.flood_zones : []; },
    maxLevel() {
      if (!this.map) return { avg: 0, count: 0 };
      const xs = this.map.stations.map(s => { const t = s.thresholds || {}; return t.red || t.orange || t.yellow || t.blue || 0; });
      return { avg: xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0, count: xs.length };
    },
  },
  methods: {
    async load() {
      this.loading = true;
      try {
        const [ov, mp] = await Promise.all([API.overview(), API.map()]);
        this.ov = ov; this.map = mp; store.overview = ov;
      } finally { this.loading = false; }
    },
    resSt(r) {
      const d = (r.current_level || 0) / r.crest_level;
      return d > 0.9 ? "超限" : d > 0.82 ? "警戒" : d > 0.7 ? "注意" : "正常";
    },
    resColor(r) {
      const d = (r.current_level || 0) / r.crest_level;
      return d > 0.9 ? "#ff4757" : d > 0.82 ? "#ff9f43" : d > 0.7 ? "#f7d54a" : "#2fd07f";
    },
    warnLevel(s) {
      const th = s.thresholds || {};
      const sl = s.simLevel || 0;
      if (sl >= th.red) return "red";
      if (sl >= th.orange) return "orange";
      if (sl >= th.yellow) return "yellow";
      if (sl >= th.blue) return "blue";
      return "";
    },
    pct(v, m) { return Math.max(0, Math.min(100, (v / m) * 100)); },
  },
  mounted() { this.load(); this._t = setInterval(() => { this.load(); }, 60000); },
  beforeUnmount() { clearInterval(this._t); },
  template: `
  <div class="page">
    <div class="page-title">实时监测
      <span class="sub">流域水雨情 · 水库蓄水 · 监测站点</span>
      <span class="badge green" style="margin-left:auto">数据自动刷新</span>
    </div>

    <div class="stats" v-if="ov">
      <div class="stat blue"><div class="k">子流域</div><div class="v">{{ ov.sub_basins }}<small>个</small></div></div>
      <div class="stat blue"><div class="k">河段</div><div class="v">{{ ov.reaches }}<small>条</small></div></div>
      <div class="stat"><div class="k">雨量站</div><div class="v">{{ ov.rain_stations }}<small>个</small></div></div>
      <div class="stat"><div class="k">水位站</div><div class="v">{{ ov.water_stations }}<small>个</small></div></div>
      <div class="stat green"><div class="k">水库</div><div class="v">{{ ov.reservoirs }}<small>座</small></div></div>
      <div class="stat amber"><div class="k">总防洪库容</div><div class="v">{{ fmt.num(ov.total_capacity, 0) }}<small>万m³</small></div></div>
      <div class="stat red"><div class="k">风险区人口</div><div class="v">{{ fmt.num(ov.population_at_risk, 0) }}<small>人</small></div></div>
      <div class="stat"><div class="k">可用降雨情景</div><div class="v">{{ ov.events }}<small>场</small></div></div>
    </div>

    <div class="row">
      <div class="col col-2">
        <div class="panel">
          <div class="panel-head">水库蓄水态势 <span class="tag">水位 / 汛限 / 防洪高</span></div>
          <div class="panel-body">
            <div style="display:flex;gap:26px;flex-wrap:wrap">
              <div v-for="r in resList" :key="r.id" style="display:flex;gap:10px;align-items:flex-end;flex-direction:column">
                <div style="font-size:12px">{{ r.name }}</div>
                <div class="level-bar" style="height:160px">
                  <div class="fill" :style="{height: pct(r.current_level, r.crest_level) + '%', background: resColor(r)}"></div>
                  <div class="mark" :style="{bottom: pct(r.flood_level, r.crest_level) + '%', color:'#f5b83d'}">汛限{{ fmt.num(r.flood_level,0) }}</div>
                  <div class="mark" :style="{bottom: pct(r.crest_level, r.crest_level) + '%', color:'#ff5c6c'}">防洪{{ fmt.num(r.crest_level,0) }}</div>
                </div>
                <div style="font-size:13px;font-weight:700;color:#dce9f7">{{ fmt.num(r.current_level, 1) }} m</div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="col col-1">
        <div class="panel">
          <div class="panel-head">监测站点一览</div>
          <div class="panel-body nopad">
            <table class="grid">
              <thead><tr><th>站点</th><th>类型</th><th>预警阈值(m)</th></tr></thead>
              <tbody>
                <tr v-for="s in stations" :key="s.id">
                  <td>{{ s.name }}</td>
                  <td>水位</td>
                  <td class="mono" style="font-size:11.5px">
                    <span class="badge blue">蓝{{ fmt.num(s.thresholds.blue,1) }}</span>
                    <span class="badge yellow">黄{{ fmt.num(s.thresholds.yellow,1) }}</span>
                    <span class="badge orange">橙{{ fmt.num(s.thresholds.orange,1) }}</span>
                    <span class="badge red">红{{ fmt.num(s.thresholds.red,1) }}</span>
                  </td>
                </tr>
                <tr v-for="s in rainStations" :key="'rn'+s.id">
                  <td>{{ s.name }}</td><td>雨量</td>
                  <td style="color:#7d95b4">面雨量自动采集</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <div class="row">
      <div class="col col-1">
        <div class="panel">
          <div class="panel-head">风险区分布
            <button class="btn sm" style="margin-left:auto" @click="$emit('open-warnings')">查看预警 →</button>
          </div>
          <div class="panel-body nopad">
            <table class="grid">
              <thead><tr><th>风险区</th><th>风险等级</th><th>人口</th><th>转移阈值</th></tr></thead>
              <tbody>
                <tr v-for="z in zones" :key="z.id">
                  <td>{{ z.name }}</td>
                  <td><span class="badge" :class="z.risk_level==='high' ? 'red' : z.risk_level==='medium' ? 'orange' : 'yellow'">{{ z.risk_level === 'high' ? '高' : z.risk_level === 'medium' ? '中' : '低' }}</span></td>
                  <td class="num">{{ z.population }} 人</td>
                  <td class="num mono">{{ fmt.num(z.low_level, 0) }} / {{ fmt.num(z.high_level, 0) }} m</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  </div>`,
};