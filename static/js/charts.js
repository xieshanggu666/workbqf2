/* 纯 SVG 折线/面积图工具：无需第三方库 */
window.Charts = (() => {
  const W = 640, H = 250, P = { l: 44, r: 14, t: 16, b: 30 };

  /* 生成平滑路径（Catmull-Rom → Bezier），避免折线毛刺 */
  function smoothPath(pts) {
    if (pts.length < 2) return "";
    let d = `M${pts[0][0]},${pts[0][1]}`;
    for (let i = 0; i < pts.length - 1; i++) {
      const p0 = pts[i - 1] || pts[i];
      const p1 = pts[i];
      const p2 = pts[i + 1];
      const p3 = pts[i + 2] || p2;
      const c1x = p1[0] + (p2[0] - p0[0]) / 6;
      const c1y = p1[1] + (p2[1] - p0[1]) / 6;
      const c2x = p2[0] - (p3[0] - p1[0]) / 6;
      const c2y = p2[1] - (p3[1] - p1[1]) / 6;
      d += ` C${c1x.toFixed(1)},${c1y.toFixed(1)} ${c2x.toFixed(1)},${c2y.toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)}`;
    }
    return d;
  }

  /* 多序列折线图
     opts: {series:[{name,color,values}], labels:[x轴], unit, hlines:[{v,label,color,dash}], legend, height, xStep} */
  function lineChart(opts) {
    const series = opts.series || [];
    const labels = opts.labels || [];
    const unit = opts.unit || "";
    const height = opts.height || H;
    const w = W, h = height, pl = P.l, pr = P.r, pt = P.t, pb = P.b;
    const iw = w - pl - pr, ih = h - pt - pb;

    let vmax = 0;
    series.forEach(s => { s.values.forEach(v => { if (v > vmax) vmax = v; }); });
    (opts.hlines || []).forEach(hl => { if (hl.v > vmax) vmax = hl.v; });
    if (vmax <= 0) vmax = 1;
    const vmaxTicks = niceCeil(vmax);
    const xN = labels.length || series[0].values.length;

    // 网格 + 纵轴刻度（5 段）
    let grid = "";
    const nTicks = 5;
    for (let i = 0; i <= nTicks; i++) {
      const v = vmaxTicks * i / nTicks;
      const y = pt + ih - (ih * (v / vmaxTicks));
      grid += `<line class="grid-line" x1="${pl}" y1="${y}" x2="${w - pr}" y2="${y}"/>`;
      grid += `<text class="axis-text" x="${pl - 7}" y="${y + 3.5}" text-anchor="end">${fmtTick(v)}</text>`;
    }
    // 横轴时刻
    let xt = "";
    const step = Math.max(1, Math.floor(xN / 10));
    for (let i = 0; i < xN; i += step) {
      const x = pl + iw * (i / Math.max(xN - 1, 1));
      const lb = labels[i] != null ? labels[i] : i;
      xt += `<text class="axis-text" x="${x}" y="${h - 8}" text-anchor="middle">${lb}</text>`;
    }

    // 水平参考线（阈值）
    let hl = "";
    (opts.hlines || []).forEach((line) => {
      if (line.v > vmaxTicks * 1.25) return;
      const y = pt + ih - ih * (line.v / vmaxTicks);
      hl += `<line x1="${pl}" y1="${y}" x2="${w - pr}" y2="${y}" stroke="${line.color || "#f5b83d"}" stroke-dasharray="5 4" stroke-width="1.3" opacity=".85"/>`;
      hl += `<text class="hline-text" x="${pl + 4}" y="${y - 4}" fill="${line.color || "#f5b83d"}">${line.label || ""}</text>`;
    });

    // 序列
    let paths = "";
    series.forEach((s, si) => {
      const pts = s.values.map((v, i) => [
        pl + iw * (i / Math.max(xN - 1, 1)),
        pt + ih - ih * (Math.min(v, vmaxTicks) / vmaxTicks),
      ]);
      const line = smoothPath(pts);
      let fill = "";
      if (opts.filled && si === 0) {
        const base = pt + ih;
        fill = `<path d="${line} L${pts[pts.length - 1][0]},${base} L${pts[0][0]},${base} Z" class="series-fill" fill="${s.color}" stroke="none"/>`;
      }
      paths += fill;
      paths += `<path class="series-line" d="${line}" stroke="${s.color}"/>`;
    });

    // 图例
    let lg = "";
    if (opts.legend !== false) {
      lg = `<div class="legend" style="padding:0 14px 10px">` +
        series.map(s => `<span class="lg"><span class="sw" style="background:${s.color}"></span>${s.name}${unit ? ` (${unit})` : ""}</span>`).join("") +
        (opts.hlines || []).map(hl => `<span class="lg"><span class="sw" style="background:${hl.color || "#f5b83d"};height:1px;border-top:1px dashed"></span>${hl.label}</span>`).join("") +
        `</div>`;
    }

    return `<div class="chart"><svg viewBox="0 0 ${w} ${h}" xmlns="http://www.w3.org/2000/svg">
      ${grid}${hl}${paths}${xt}
    </svg>${lg}</div>`;
  }

  /* 柱状图（雨量） */
  function barChart(values, labels, opts = {}) {
    const unit = opts.unit || "";
    const color = opts.color || "#37b6ff";
    let vmax = Math.max(...values, 1);
    const w = W, h = 210, pl = P.l, pr = P.r, pt = P.t, pb = P.b;
    const iw = w - pl - pr, ih = h - pt - pb;
    const n = values.length;
    const bw = Math.min(9, iw / n * 0.72);

    let bars = "", axis = "";
    values.forEach((v, i) => {
      const bh = Math.max(2, ih * (v / vmax));
      const x = pl + iw * (i / Math.max(n - 1, 1)) - bw / 2;
      bars += `<rect x="${x.toFixed(1)}" y="${(pt + ih - bh).toFixed(1)}" width="${bw}" height="${bh.toFixed(1)}" fill="${color}" opacity="${0.35 + 0.65 * (v / vmax)}" rx="1.5"/>`;
    });
    const step = Math.max(1, Math.floor(n / 12));
    for (let i = 0; i < n; i += step) {
      const x = pl + iw * (i / Math.max(n - 1, 1));
      const lb = labels[i] != null ? labels[i] : i;
      axis += `<text class="axis-text" x="${x}" y="${h - 8}" text-anchor="middle">${lb}</text>`;
    }
    for (let i = 0; i <= 4; i++) {
      const v = vmax * i / 4;
      const y = pt + ih - ih * (i / 4);
      axis += `<line class="grid-line" x1="${pl}" y1="${y}" x2="${w - pr}" y2="${y}"/>`;
      axis += `<text class="axis-text" x="${pl - 7}" y="${y + 3.5}" text-anchor="end">${fmtTick(v)}</text>`;
    }
    return `<div class="chart"><svg viewBox="0 0 ${w} ${h}" xmlns="http://www.w3.org/2000/svg">${axis}${bars}</svg>
      <div class="legend" style="padding:0 14px 10px"><span class="lg"><span class="sw" style="background:${color}"></span>逐小时面雨量${unit ? ` (${unit})` : ""}</span></div></div>`;
  }

  function niceCeil(v) {
    if (v <= 0) return 1;
    const mag = Math.pow(10, Math.floor(Math.log10(v)));
    const norm = v / mag;
    let n;
    if (norm <= 1) n = 1; else if (norm <= 2) n = 2;
    else if (norm <= 2.5) n = 2.5; else if (norm <= 5) n = 5;
    else n = 10;
    return n * mag;
  }
  function fmtTick(v) {
    if (v >= 10000) return (v / 10000).toFixed(1) + "w";
    if (v >= 1000) return (v / 1000).toFixed(1) + "k";
    return Number.isInteger(v) ? String(v) : v.toFixed(1);
  }

  return { lineChart, barChart };
})();
