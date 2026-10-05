const { createApp } = Vue;

const I = {
  map: '<path d="M9 20 3 21V5l6-1 6-1 6 1v16l-6 1z"/><path d="M9 19V4M15 18V3"/>',
  monitor: '<path d="M4 19V5h12l4 4v10z"/><path d="M16 5v4h4"/><path d="M8 11h6M8 15h4"/>',
  forecast: '<path d="M12 3v3M5.6 5.6l2 2M3 12h3m12 0h3M16.4 16.4l2 2M8 8h6a3 3 0 1 1 0 6H6"/>',
  strat: '<circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 0 0-.14-1.4l2-1.5-2-3.4-2.3 1a7 7 0 0 0-2.4-1.4L13.8 3h-4l-.36 2.3a7 7 0 0 0-2.4 1.4l-2.3-1-2 3.4 2 1.5A7 7 0 0 0 5 12c0 .48.05.94.14 1.4l-2 1.5 2 3.4 2.3-1a7 7 0 0 0 2.4 1.4l.36 2.3h4l.36-2.3a7 7 0 0 0 2.4-1.4l2.3 1 2-3.4-2-1.5c.09-.46.14-.92.14-1.4z"/>',
  alarm: '<path d="M12 4 12 5.5"/><path d="M4 19h16"/><path d="M6 19 6.5 13M9 19v-8M12 19v-6M15 19v-8M18 19l.5-6"/>',
  joint: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
};

const App = {
  data() {
    return {
      view: "basinmap",
      tabs: [
        { id: "basinmap", name: "流域总览", icon: I.map },
        { id: "monitor", name: "实时监测", icon: I.monitor },
        { id: "forecast", name: "洪水预报", icon: I.forecast },
        { id: "dispatch", name: "联合调度", icon: I.strat },
        { id: "disposal", name: "处置协同", icon: I.joint },
        { id: "warnings", name: "预警转移", icon: I.alarm },
      ],
      now: "",
      toast: "",
      loading: false,
      _toastTimer: null,
    };
  },
  computed: {
    loadingText() { return this.loading ? "数据处理中…" : ""; },
  },
  methods: {
    tick() {
      const d = new Date();
      const s = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")} ${String(d.getHours()).padStart(2,"0")}:${String(d.getMinutes()).padStart(2,"0")}:${String(d.getSeconds()).padStart(2,"0")}`;
      this.now = s;
    },
    showToast(msg) {
      this.toast = msg;
      clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(() => { this.toast = ""; }, 3000);
    },
    showLoading(_t) { this.loading = true; },
    hideLoading() { this.loading = false; },
    onForecastRun(result) {
      if (result && result.warnings && result.warnings.length) {
        this.showToast(`推演完成：触发 ${result.warnings.length} 项预警，联动 ${result.evacuations.length} 处转移`);
      } else {
        this.showToast("推演完成，流域安全");
      }
    },
  },
  mounted() {
    this.tick();
    setInterval(() => this.tick(), 1000);
    window.app = this;
  },
  template: `
  <div style="height:100vh;display:flex;flex-direction:column">
    <header class="topbar">
      <div class="brand">
        <svg viewBox="0 0 24 24" class="logo"><path d="M2 6c1.8 2.4 3.4 2.4 5.2 0s3.4-2.4 5.2 0 3.4 2.4 5.2 0 3.4-2.4 5.2 0" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M2 12c1.8 2.4 3.4 2.4 5.2 0s3.4-2.4 5.2 0 3.4 2.4 5.2 0 3.4-2.4 5.2 0" fill="none" stroke="currentColor" stroke-width="1.6" opacity=".7"/><path d="M2 18c1.8 2.4 3.4 2.4 5.2 0s3.4-2.4 5.2 0 3.4 2.4 5.2 0 3.4-2.4 5.2 0" fill="none" stroke="currentColor" stroke-width="1.6" opacity=".45"/></svg>
        <div class="brand-text">
          <h1>青岚江流域 · 洪水预报与水库群联合调度</h1>
          <span>Flood Forecasting &amp; Joint Reservoir Dispatch System</span>
        </div>
      </div>
      <nav class="nav">
        <a v-for="t in tabs" :key="t.id" :class="['nav-item', {active: view === t.id}]" @click="view = t.id">
          <svg viewBox="0 0 24 24" v-html="t.icon"></svg>{{ t.name }}
        </a>
      </nav>
      <div class="topbar-time">{{ now }}</div>
    </header>

    <main class="main">
      <transition name="page" mode="out-in">
        <keep-alive>
          <component :is="view" @forecast-run="onForecastRun" @open-warnings="view='warnings'" @open-dispatch="view='forecast'"></component>
        </keep-alive>
      </transition>
    </main>

    <transition name="fade">
      <div class="global-loading" v-if="loadingText">
        <div class="spinner"></div><span>{{ loadingText }}</span>
      </div>
    </transition>
    <transition name="slide-up">
      <div class="toast" v-if="toast">{{ toast }}</div>
    </transition>
  </div>`,
};

const app = createApp(App);
// 全局工具（fmt/store/API/Charts）注入，供各视图模板直接使用
app.config.globalProperties.fmt = window.fmt;
app.config.globalProperties.store = window.store;
app.config.globalProperties.API = window.API;
app.config.globalProperties.Charts = window.Charts;
app.component("basinmap", window.BasinMap);
app.component("monitor", window.MonitorView);
app.component("forecast", window.ForecastView);
app.component("dispatch", window.DispatchView);
app.component("disposal", window.DisposalView);
app.component("warnings", window.WarningsView);
const vmRoot = app.mount("#app");
window.app = vmRoot;  // 供视图访问 showLoading / showToast / hideLoading