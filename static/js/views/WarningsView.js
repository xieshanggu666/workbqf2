/* 视图：预警与转移 —— 预警记录、风险区转移、转移路线 */
window.WarningsView = {
  name: "WarningsView",
  data() {
    return { warnings: [], evacuations: [], zones: [], map: null, loading: false };
  },
  computed: {
    activeWarns() { return this.warnings.filter(w => w.status !== "cleared"); },
    totals() {
      const t = { red: 0, orange: 0, yellow: 0, blue: 0 };
      this.warnings.forEach(w => { if (t[w.level] !== undefined) t[w.level]++; });
      return t;
    },
  },
  methods: {
    async load() {
      this.loading = true;
      try {
        const [w, e, mp] = await Promise.all([API.warnings(), API.evacuations(), API.map()]);
        this.warnings = w; this.evacuations = e; this.map = mp;
        this.zones = mp.flood_zones;
      } finally { this.loading = false; }
    },
    evacColor(st) {
      return { pending: "orange", moving: "blue", safe: "green" }[st] || "gray";
    },
    evacName(st) {
      return { pending: "待转移", moving: "转移中", safe: "已安全" }[st] || st;
    },
    zoneRoute(z) {
      if (!z || !z.route) return "";
      return `<svg viewBox="0 0 800 560" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto">
        <rect width="800" height="560" fill="#0b1a2e"/>
        <g>${this.buildRouteSvg(z)}</g>
      </svg>`;
    },
    buildRouteSvg(z) {
      let s = "";
      const pts = z.route || [];
      if (pts.length >= 2) {
        const d = "M" + pts.map(p => `${p[0]},${p[1]}`).join(" L");
        s += `<path d="${d}" stroke="#2fd07f" stroke-width="2.2" stroke-dasharray="6 5" fill="none"/>`;
        // 箭头
        for (let i = 1; i < pts.length; i++) {
          const a = pts[i - 1], b = pts[i];
          const ang = Math.atan2(b[1] - a[1], b[0] - a[0]);
          const lx = b[0], ly = b[1];
          s += `<path d="M${lx},${ly} L${(lx - 8 * Math.cos(ang - 0.5)).toFixed(1)},${(ly - 8 * Math.sin(ang - 0.5)).toFixed(1)} M${lx},${ly} L${(lx - 8 * Math.cos(ang + 0.5)).toFixed(1)},${(ly - 8 * Math.sin(ang + 0.5)).toFixed(1)}" stroke="#2fd07f" stroke-width="2"/>`;
        }
        // 起点风险区
        s += `<circle cx="${pts[0][0]}" cy="${pts[0][1]}" r="9" fill="#ff5c6c" opacity=".25"/><circle cx="${pts[0][0]}" cy="${pts[0][1]}" r="4.5" fill="#ff5c6c"/>`;
        s += `<text x="${pts[0][0]}" y="${pts[0][1] - 12}" text-anchor="middle" fill="#ff8d97" font-size="11">${z.name}</text>`;
        // 终点安全区
        const end = pts[pts.length - 1];
        s += `<circle cx="${end[0]}" cy="${end[1]}" r="11" fill="#2fd07f" opacity=".25"/><circle cx="${end[0]}" cy="${end[1]}" r="5" fill="#2fd07f"/>`;
        s += `<text x="${end[0]}" y="${end[1] - 14}" text-anchor="middle" fill="#7bf0b3" font-size="11">安全安置点</text>`;
      }
      // 人口
      s += `<text x="${z.x}" y="${z.y + 30}" text-anchor="middle" fill="#7d95b4" font-size="10">受威胁人口 ${z.population} 人</text>`;
      return s;
    },
    routeLen(z) { return z.route && z.route.length >= 2 ? Math.hypot(z.route[z.route.length-1][0]-z.route[0][0], z.route[z.route.length-1][1]-z.route[0][1]) : 0; },
  },
  mounted() { this.load(); },
  template: `
  <div class="page">
    <div class="page-title">预警与转移
      <span class="sub">流域预警台账 · 风险区受威胁评估 · 群众转移方案</span>
      <button class="btn sm" style="margin-left:auto" @click="load">刷新</button>
    </div>

    <!-- 预警统计 -->
    <div class="stats">
      <div class="stat red"><div class="k">红色预警</div><div class="v">{{ totals.red }}</div></div>
      <div class="stat" style="--c:var(--orange-lv)"><div class="k">橙色预警</div><div class="v" style="color:var(--orange-lv)">{{ totals.orange }}</div></div>
      <div class="stat"><div class="k">黄色预警</div><div class="v" style="color:var(--yellow-lv)">{{ totals.yellow }}</div></div>
      <div class="stat"><div class="k">蓝色预警</div><div class="v" style="color:var(--blue-lv)">{{ totals.blue }}</div></div>
      <div class="stat red"><div class="k">风险区待转移</div><div class="v">{{ evacuations.filter(e => e.status !== 'safe').length }}<small>处</small></div></div>
      <div class="stat amber"><div class="k">受威胁总人口</div><div class="v">{{ evacuations.reduce((a, e) => a + (e.status !== 'safe' ? e.people : 0), 0) }}<small>人</small></div></div>
    </div>

    <div class="row">
      <div class="col col-1">
        <div class="panel">
          <div class="panel-head">预警记录 <span class="tag">{{ warnings.length }} 条</span></div>
          <div class="panel-body nopad" style="max-height:480px;overflow-y:auto">
            <table class="grid">
              <thead><tr><th>等级</th><th>目标</th><th>触发值</th><th>处置单</th><th>时间</th></tr></thead>
              <tbody>
                <tr v-for="w in warnings" :key="w.id">
                  <td><span class="badge" :class="fmt.lvBadge(w.level)">{{ fmt.lvName(w.level) }}</span></td>
                  <td>{{ w.target_name }}</td>
                  <td class="num mono">{{ fmt.num(w.value,1) }} / {{ fmt.num(w.threshold,1) }}</td>
                  <td>
                    <span v-if="w.disposal_id" class="badge blue" style="cursor:pointer"
                          @click="$root.view='disposal'">#{{ w.disposal_id }}</span>
                    <span v-else class="badge gray">历史</span>
                  </td>
                  <td style="font-size:11.5px;color:#7d95b4">{{ fmt.time(w.created_at) }}</td>
                </tr>
                <tr v-if="!warnings.length"><td colspan="5" style="text-align:center;color:#7d95b4;padding:26px">暂无预警记录，执行洪水预报后自动生成</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <div class="col col-1">
        <div class="panel">
          <div class="panel-head">风险区转移台账 <span class="tag">{{ evacuations.length }} 次</span></div>
          <div class="panel-body nopad">
            <table class="grid">
              <thead><tr><th>风险区</th><th>触发方式</th><th>人数</th><th>状态</th><th>指定避难点</th><th>处置单</th><th>时间</th></tr></thead>
              <tbody>
                <tr v-for="e in evacuations" :key="e.id">
                  <td>{{ e.zone_name }}</td>
                  <td style="font-size:12px">{{ e.triggered_by }}</td>
                  <td class="num">{{ e.people }} 人</td>
                  <td><span class="badge" :class="evacColor(e.status)">{{ evacName(e.status) }}</span></td>
                  <td style="font-size:12px">{{ e.shelter_name || '—' }}</td>
                  <td>
                    <span v-if="e.disposal_id" class="badge blue" style="cursor:pointer"
                          @click="$root.view='disposal'">#{{ e.disposal_id }}</span>
                    <span v-else class="badge gray">历史</span>
                  </td>
                  <td style="font-size:11.5px;color:#7d95b4">{{ fmt.time(e.created_at) }}</td>
                </tr>
                <tr v-if="!evacuations.length"><td colspan="6" style="text-align:center;color:#7d95b4;padding:26px">无转移记录</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <!-- 转移路线 -->
    <div class="row">
      <div class="col col-1" v-for="z in zones" :key="z.id">
        <div class="panel">
          <div class="panel-head">{{ z.name }} · 转移路线 <span class="tag" v-if="z.route && z.route.length">距安全点 {{ fmt.num(routeLen(z),0) }} 单位 · 转移 {{ z.population }} 人</span></div>
          <div class="panel-body" v-html="zoneRoute(z)"></div>
        </div>
      </div>
    </div>
  </div>`,
};