/* 视图：流域总览图 —— SVG 河网 + 水库 + 站点 + 风险区 */
window.BasinMap = {
  name: "BasinMap",
  data() {
    return { data: null, tip: null, tipX: 0, tipY: 0, loading: false, selRes: null };
  },
  computed: {
    posMap() {
      if (!this.data) return {};
      const m = {};
      this.data.nodes.forEach(n => { m[n.id] = { x: n.x, y: n.y }; });
      return m;
    },
  },
  methods: {
    async load() {
      this.loading = true;
      try {
        this.data = await API.map();
        store.mapData = this.data;
      } finally { this.loading = false; }
    },
    nodePos(id) { return this.posMap[id] || { x: 0, y: 0 }; },
    riverPath(r) {
      const a = this.nodePos(r.from), b = this.nodePos(r.to);
      const d = Math.hypot(b.x - a.x, b.y - a.y);
      const ctrl = d * 0.18;
      return `M${a.x},${a.y} C${a.x + ctrl},${a.y} ${b.x - ctrl},${b.y} ${b.x},${b.y}`;
    },
    onMove(e, text) {
      const rect = this.$refs.mapWrap.getBoundingClientRect();
      this.tip = text; this.tipX = e.clientX - rect.left; this.tipY = e.clientY - rect.top;
    },
    clearTip() { this.tip = null; },
    resText(r) {
      const d = (r.current_level == null ? 0 : r.current_level) / r.crest_level;
      const st = d > 0.9 ? "red" : d > 0.82 ? "orange" : d > 0.7 ? "yellow" : "green";
      return { r, d, st };
    },
    nodeKindName(k) {
      return { headwater: "源头", reservoir: "水库节点", junction: "汇合点", control: "控制断面", outlet: "河口" }[k] || k;
    },
    resShape(r) {
      const rx = 26, ry = 20;
      return `M${r.x},${r.y - ry} a${rx},${ry} 0 1,0 0.01,0 Z`;
    },
    resWater(r) {
      const rx = 20, ry = 15;
      return `M${r.x},${r.y - ry + 3} a${rx},${ry} 0 1,0 0.01,0 Z`;
    },
    zonePoly(z) {
      const w = 64, h = 38;
      return `${z.x - w},${z.y + h} ${z.x},${z.y - h} ${z.x + w},${z.y + h}`;
    },
    pctOf(v, max) { return Math.max(0, Math.min(100, (v / max) * 100)); },
  },
  mounted() { this.load(); },
  template: `
  <div class="page">
    <div class="page-title">流域总览
      <span class="sub">青岚江流域河网 · 水库 · 监测站点 · 淹没风险区</span>
      <button class="btn sm" style="margin-left:auto" @click="load">刷新</button>
    </div>
    <div class="row">
      <div class="col col-3">
        <div class="panel">
          <div class="panel-head">流域河网示意图 <span class="tag">点击水库查看详情 · 悬停查看要素信息</span></div>
          <div class="panel-body nopad" ref="mapWrap" style="position:relative">
            <svg v-if="data" viewBox="0 0 800 560" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;display:block">
              <!-- 支流子流域范围 -->
              <g v-for="s in data.sub_basins" :key="'sub'+s.id">
                <circle :cx="s.x" :cy="s.y" :r="Math.sqrt(s.area_km2)*2.6" class="sub-area"/>
                <text :x="s.x" :y="s.y + 3" text-anchor="middle" style="font-size:9px;fill:#6f93b8">{{ s.name }}</text>
              </g>

              <!-- 河网 -->
              <g v-for="r in data.reaches" :key="'rch'+r.id">
                <path :d="riverPath(r)" class="river-path glow"/>
                <path :d="riverPath(r)" class="river-path anim"/>
                <path :d="riverPath(r)" class="river-path anim2"/>
              </g>

              <!-- 风险区 -->
              <g v-for="z in data.flood_zones" :key="'zone'+z.id">
                <polygon :points="zonePoly(z)" :class="['zone-area', {high: z.risk_level==='high'}]"/>
                <text :x="z.x" :y="z.y + 4" text-anchor="middle" class="zone-label">{{ z.name }} · {{ z.population }}人</text>
              </g>

              <!-- 水库 -->
              <g v-for="r in data.reservoirs" :key="'res'+r.id"
                 @mousemove="onMove($event, resText(r).st === 'red' ? r.name + '：<b style=color:#ff5c6c>超防洪高水位</b>' : r.name)"
                 @mouseleave="clearTip" @click="selRes = r" style="cursor:pointer">
                <path :d="resShape(r)" class="res-body"/>
                <path :d="resWater(r)" class="res-water"/>
                <text :x="r.x" :y="r.y - 16" text-anchor="middle" class="res-name">{{ r.name }}</text>
                <text :x="r.x" :y="r.y - 4" text-anchor="middle" class="res-level">水位 {{ fmt.num(r.current_level, 1) }}m</text>
                <circle :cx="r.x" :cy="r.y" r="4" :fill="resText(r).st==='red' ? '#ff4757' : resText(r).st==='orange' ? '#ff9f43' : resText(r).st==='yellow' ? '#f7d54a' : '#2fd07f'" stroke="#fff" stroke-width="1.2"/>
              </g>

              <!-- 雨量站 -->
              <g v-for="s in data.rain_stations" :key="'rn'+s.id" @mousemove="onMove($event, s.name + '：雨量站')" @mouseleave="clearTip">
                <circle :cx="s.x" :cy="s.y" r="5.5" fill="#14532d" stroke="#4ade80" class="station-dot"/>
                <text :x="s.x" :y="s.y - 9" text-anchor="middle" class="station-name">☔ {{ s.name }}</text>
              </g>

              <!-- 水位站 -->
              <g v-for="s in data.stations" :key="'ws'+s.id" @mousemove="onMove($event, s.name + '：水位站')" @mouseleave="clearTip">
                <circle :cx="s.x" :cy="s.y" r="5.5" fill="#1e3a8a" stroke="#38b6ff" class="station-dot"/>
                <text :x="s.x" :y="s.y - 9" text-anchor="middle" class="station-name">◉ {{ s.name }}</text>
              </g>

              <!-- 普通节点 -->
              <g v-for="n in data.nodes" :key="'nd'+n.id" @mousemove="onMove($event, n.name + '（' + nodeKindName(n.kind) + '）')" @mouseleave="clearTip">
                <circle :cx="n.x" :cy="n.y" r="3.4" class="node-dot"/>
                <text :x="n.x" :y="n.y - 8" text-anchor="middle" class="node-name">{{ n.name }}</text>
              </g>
            </svg>
            <div class="map-legend" v-if="data">
              <span class="it"><span class="dot" style="background:#2fd07f"></span>水库·安全</span>
              <span class="it"><span class="dot" style="background:#f7d54a"></span>水库·注意</span>
              <span class="it"><span class="dot" style="background:#ff9f43"></span>水库·警戒</span>
              <span class="it"><span class="dot" style="background:#ff4757"></span>水库·超限</span>
              <span class="it"><span class="dot" style="background:#38b6ff;border-radius:50%"></span>水位站</span>
              <span class="it"><span class="dot" style="background:#4ade80;border-radius:50%"></span>雨量站</span>
            </div>
            <div class="map-tooltip" v-if="tip" :style="{left: tipX + 14 + 'px', top: tipY + 12 + 'px'}" v-html="tip"></div>
          </div>
        </div>
      </div>

      <div class="col col-1">
        <div class="panel">
          <div class="panel-head">水库实时状态 <span class="tag">{{ data ? data.reservoirs.length : 0 }} 座</span></div>
          <div class="panel-body nopad">
            <div class="flow-list">
              <div class="flow-item" v-for="r in (data ? data.reservoirs : [])" :key="r.id">
                <span class="badge" :class="resText(r).st">{{ resText(r).st === 'red' ? '超限' : resText(r).st === 'orange' ? '警戒' : resText(r).st === 'yellow' ? '注意' : '正常' }}</span>
                <span class="name">{{ r.name }}</span>
                <span class="val" :style="{color: resText(r).st === 'red' ? '#ff5c6c' : '#dce9f7'}">{{ fmt.num(r.current_level, 1) }}<small style="font-size:11px;color:#7d95b4">m</small></span>
              </div>
            </div>
          </div>
        </div>

        <div class="panel" v-if="selRes">
          <div class="panel-head">水库详情 — {{ selRes.name }}</div>
          <div class="panel-body" style="display:flex;gap:18px;align-items:flex-end">
            <div class="level-bar">
              <div class="fill" :style="{height: pctOf(selRes.current_level, selRes.crest_level) + '%'}"></div>
              <div class="mark" :style="{bottom: pctOf(selRes.flood_level, selRes.crest_level) + '%', color:'#f5b83d'}">汛限</div>
              <div class="mark" :style="{bottom: pctOf(selRes.crest_level, selRes.crest_level) + '%', color:'#ff5c6c'}">防洪高</div>
            </div>
            <table style="font-size:12.5px;flex:1">
              <tr><td style="color:#7d95b4;padding:3px 0">正常蓄水位</td><td class="mono">{{ fmt.num(selRes.normal_level, 1) }} m</td></tr>
              <tr><td style="color:#7d95b4;padding:3px 0">汛限水位</td><td class="mono">{{ fmt.num(selRes.flood_level, 1) }} m</td></tr>
              <tr><td style="color:#7d95b4;padding:3px 0">防洪高水位</td><td class="mono">{{ fmt.num(selRes.crest_level, 1) }} m</td></tr>
              <tr><td style="color:#7d95b4;padding:3px 0">闸门最大泄流</td><td class="mono">{{ fmt.num(selRes.gate_max, 0) }} m³/s</td></tr>
              <tr><td style="color:#7d95b4;padding:3px 0">当前水位</td><td class="mono" :style="{color: resText(selRes).st==='red' ? '#ff5c6c' : '#dce9f7'}">{{ fmt.num(selRes.current_level, 1) }} m</td></tr>
            </table>
          </div>
        </div>
      </div>
    </div>
  </div>`,
};
