/* 轻量 API 封装 + 全局加载态 */
const API = {
  async get(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  },
  async post(path, body) {
    const r = await fetch(path, {
      method: "POST",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    return data;
  },
  async del(path, body) {
    const r = await fetch(path, {
      method: "DELETE",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    return data;
  },
  overview: () => API.get("/api/overview"),
  map: () => API.get("/api/map"),
  rainEvents: () => API.get("/api/rain-events"),
  rainEvent: (id) => API.get(`/api/rain-events/${id}`),
  reservoirs: () => API.get("/api/reservoirs"),
  warnings: () => API.get("/api/warnings"),
  evacuations: () => API.get("/api/evacuations"),
  forecast: (eid, mode) => API.post(`/api/forecast/${eid}/${mode}`),
  forecastRuns: () => API.get("/api/forecast/runs"),
  forecastSeries: (runId) => API.get(`/api/forecast/series/${runId}`),
  // 联合防汛处置协同
  disposals: () => API.get("/api/disposals"),
  disposal: (id) => API.get(`/api/disposals/${id}`),
  initiateDisposal: (runId, body) => API.post(`/api/disposals/from-run/${runId}`, body),
  reviewDisposal: (id, body) => API.post(`/api/disposals/${id}/review`, body),
  executeDisposal: (id, body) => API.post(`/api/disposals/${id}/execute`, body),
  completeDisposal: (id, body) => API.post(`/api/disposals/${id}/complete`, body),
  refreshDisposalForecast: (id, body) => API.post(`/api/disposals/${id}/refresh-forecast`, body),
  // 应急资源与避难点协同调度
  shelters: () => API.get("/api/resources/shelters"),
  vehicles: () => API.get("/api/resources/vehicles"),
  supplies: () => API.get("/api/resources/supplies"),
  assignShelter: (id, body) => API.post(`/api/disposals/${id}/shelter-assignments`, body),
  releaseShelter: (id, aid, role) => API.del(`/api/disposals/${id}/shelter-assignments/${aid}`, { role }),
  assignVehicle: (id, body) => API.post(`/api/disposals/${id}/vehicle-dispatches`, body),
  releaseVehicle: (id, did, role) => API.del(`/api/disposals/${id}/vehicle-dispatches/${did}`, { role }),
  assignSupply: (id, body) => API.post(`/api/disposals/${id}/supply-allocations`, body),
  releaseSupply: (id, aid, role) => API.del(`/api/disposals/${id}/supply-allocations/${aid}`, { role }),
  confirmResources: (id, body) => API.post(`/api/disposals/${id}/confirm-resources`, body),
};

/* 全局运行状态：跨视图共享最近一次预报结果 / 运行记录 */
const store = {
  lastForecast: null,        // {run_id, ...完整预报结果}
  runs: [],                  // [{id, event_id, mode, created_at}]
  mapData: null,
  overview: null,
};

const fmt = {
  num(v, d = 1) { return (v == null ? "—" : Number(v).toFixed(d)); },
  lvName(lv) { return { red: "红色预警", orange: "橙色预警", yellow: "黄色预警", blue: "蓝色预警", "": "" }[lv] || lv; },
  lvClass(lv) { return { red: "lv-4", orange: "lv-3", yellow: "lv-2", blue: "lv-1" }[lv] || ""; },
  lvBadge(lv) { return { red: "red", orange: "orange", yellow: "yellow", blue: "blue" }[lv] || "gray"; },
  hour(h) { return `${String(h).padStart(2, "0")}:00`; },
  time(t) { if (!t) return "—"; const d = new Date(t); return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; },
};

// 普通 script 的顶层 const 不会挂到 window，显式导出供视图/全局配置使用
window.store = store;
window.fmt = fmt;
window.API = API;
