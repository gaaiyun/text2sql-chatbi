// 按智能体推荐的图表规格绘制 SVG。保留 SQL 的行顺序，最大值用安全黄标出（带墨色描边保证对比度）。

import { formatNumber, h, isNumber, s, truncate } from "./dom.js";

const INK = "#2b4a64";
const ACCENT = "#ffcc00";
const OUTLINE = "#0f1318";
const SECOND = "#7d97ad";
const RULE = "#dcdfe3";
const TEXT = "#343b44";
const MUTED = "#626c78";

function niceStep(range, count) {
  const rough = range / Math.max(count, 1);
  const power = 10 ** Math.floor(Math.log10(rough || 1));
  const fraction = rough / power;
  const nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 2.5 ? 2.5 : fraction <= 5 ? 5 : 10;
  return nice * power;
}

function scaleFor(values, count = 4) {
  const max = Math.max(0, ...values);
  const min = Math.min(0, ...values);
  const step = niceStep(max - min || 1, count);
  const top = Math.ceil(max / step) * step || step;
  const bottom = Math.floor(min / step) * step;
  const ticks = [];
  for (let v = bottom; v <= top + step / 2; v += step) ticks.push(Number(v.toFixed(10)));
  return { top, bottom, ticks };
}

function shortNumber(value) {
  const abs = Math.abs(value);
  if (abs >= 1e8) return `${formatNumber(value / 1e8)}亿`;
  if (abs >= 1e4) return `${formatNumber(value / 1e4)}万`;
  return formatNumber(value);
}

// SVG 表现属性不解析 CSS 变量，字体通过 style 设置
function text(x, y, content, props = {}) {
  const { "font-family": family = "var(--mono)", ...rest } = props;
  return s("text", { x, y, fill: TEXT, "font-size": 11, style: `font-family:${family}`, ...rest }, content);
}

function numeric(rows, column) {
  return rows.map((row) => (isNumber(row[column]) ? row[column] : 0));
}

function wrapFigure(svgEl, spec) {
  const figure = h("figure", { class: "chart" });
  if (spec.title) figure.append(h("p", { class: "chart-title" }, spec.title));
  figure.append(svgEl);
  if (spec.reason) figure.append(h("figcaption", {}, `图表：${spec.reason}`));
  return figure;
}

function yAxis(svgEl, scale, { left, right, top, height }) {
  const y = (v) => top + height - ((v - scale.bottom) / (scale.top - scale.bottom)) * height;
  for (const tick of scale.ticks) {
    const py = y(tick);
    svgEl.append(
      s("line", {
        x1: left,
        x2: right,
        y1: py,
        y2: py,
        stroke: RULE,
        "stroke-width": tick === 0 ? 1.2 : 0.8,
      }),
      text(left - 8, py + 4, shortNumber(tick), { "text-anchor": "end", fill: MUTED }),
    );
  }
  return y;
}

function barChart(spec, x, measure, rows) {
  const width = 760;
  const n = rows.length;
  const labels = rows.map((row) => String(row[x] ?? "—"));
  const rotate = n > 8 || labels.some((l) => l.length > 6);
  const margin = { top: 18, right: 8, bottom: rotate ? 86 : 40, left: 58 };
  const height = 300;
  const innerH = height - margin.top - margin.bottom;
  const values = numeric(rows, measure);
  const scale = scaleFor(values);
  const svgEl = s("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": spec.title || measure });
  const y = yAxis(svgEl, scale, { left: margin.left, right: width - margin.right, top: margin.top, height: innerH });
  const band = (width - margin.left - margin.right) / n;
  const barW = Math.min(46, band * 0.62);
  const maxValue = Math.max(...values);
  rows.forEach((row, i) => {
    const value = values[i];
    const cx = margin.left + band * i + band / 2;
    const y0 = y(Math.max(0, value));
    const barH = Math.abs(y(value) - y(0));
    const bar = s("rect", {
      x: cx - barW / 2,
      y: y0,
      width: barW,
      height: Math.max(barH, 0.5),
      fill: value === maxValue ? ACCENT : INK,
      stroke: value === maxValue ? OUTLINE : "none",
      "stroke-width": 1.2,
    });
    bar.append(s("title", {}, `${labels[i]}：${formatNumber(row[measure])}`));
    svgEl.append(bar);
    if (n <= 16) {
      svgEl.append(text(cx, y0 - 6, shortNumber(value), { "text-anchor": "middle", "font-size": 10.5 }));
    }
    const label = truncate(labels[i], rotate ? 10 : 8);
    const ly = height - margin.bottom + 16;
    svgEl.append(
      rotate
        ? text(cx, ly, label, {
            "text-anchor": "end",
            transform: `rotate(-38 ${cx} ${ly})`,
            "font-family": "var(--sans)",
            "font-size": 11.5,
          })
        : text(cx, ly, label, { "text-anchor": "middle", "font-family": "var(--sans)", "font-size": 12 }),
    );
  });
  svgEl.append(text(margin.left - 8, 10, measure, { "text-anchor": "end", fill: MUTED, "font-family": "var(--sans)" }));
  return wrapFigure(svgEl, spec);
}

function barhChart(spec, x, measure, rows) {
  const width = 760;
  const rowH = 26;
  const labels = rows.map((row) => String(row[x] ?? "—"));
  const labelW = Math.min(230, Math.max(70, ...labels.map((l) => Math.min(l.length, 16) * 12.5 + 12)));
  const valueW = 72;
  const height = rows.length * rowH + 12;
  const values = numeric(rows, measure);
  const maxValue = Math.max(...values, 0) || 1;
  const svgEl = s("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": spec.title || measure });
  const span = width - labelW - valueW;
  rows.forEach((row, i) => {
    const value = values[i];
    const top = i * rowH + 4;
    const barW = Math.max((Math.max(value, 0) / maxValue) * span, 0.5);
    svgEl.append(
      text(labelW - 10, top + 15, truncate(labels[i], 16), {
        "text-anchor": "end",
        "font-family": "var(--sans)",
        "font-size": 12.5,
      }),
      s("rect", { x: labelW, y: top + 4, width: barW, height: rowH - 10, fill: value === maxValue ? ACCENT : INK, stroke: value === maxValue ? OUTLINE : "none", "stroke-width": 1.2 }),
      text(labelW + barW + 8, top + 15, formatNumber(row[measure]), { "font-size": 11.5 }),
    );
  });
  svgEl.append(s("line", { x1: labelW, x2: labelW, y1: 0, y2: height, stroke: TEXT, "stroke-width": 1 }));
  return wrapFigure(svgEl, spec);
}

const SERIES_COLORS = [INK, "#c98a00", SECOND, "#8a4f7d", "#3f7f6a", "#9a5b3c"];

function lineChart(spec, x, measures, rows) {
  const width = 760;
  const height = 290;
  // 同一根纵轴上画几条线由后端决定（量级相近的指标才放在一起），这里只设颜色上限
  const limit = SERIES_COLORS.length;
  const margin = { top: 28, right: 64, bottom: 40, left: 58 };
  const innerW = width - margin.left - margin.right;
  const innerH = height - margin.top - margin.bottom;
  // 缺失的点不画（不当成 0），折线在缺口处断开
  const series = measures.slice(0, limit).map((name, index) => ({
    name,
    color: SERIES_COLORS[index],
    values: rows.map((row) => (isNumber(row[name]) ? row[name] : null)),
  }));
  const scale = scaleFor(series.flatMap((serie) => serie.values.filter((v) => v !== null)));
  const svgEl = s("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": spec.title || measures.join("、") });
  const y = yAxis(svgEl, scale, { left: margin.left, right: width - margin.right, top: margin.top, height: innerH });
  const n = rows.length;
  const px = (i) => margin.left + (n === 1 ? innerW / 2 : (innerW * i) / (n - 1));
  const every = Math.ceil(n / 12);
  rows.forEach((row, i) => {
    if (i % every === 0 || i === n - 1) {
      svgEl.append(text(px(i), height - margin.bottom + 18, String(row[x] ?? ""), { "text-anchor": "middle", fill: MUTED }));
    }
  });
  for (const serie of series) {
    const present = serie.values.map((v, i) => [v, i]).filter(([v]) => v !== null);
    if (!present.length) continue;
    let run = [];
    const flush = () => {
      if (run.length > 1) svgEl.append(s("polyline", { points: run.join(" "), fill: "none", stroke: serie.color, "stroke-width": 2, "stroke-linejoin": "round" }));
      run = [];
    };
    serie.values.forEach((v, i) => (v === null ? flush() : run.push(`${px(i).toFixed(1)},${y(v).toFixed(1)}`)));
    flush();
    const [lastValue, lastIndex] = present[present.length - 1];
    for (const [v, i] of present) {
      const last = i === lastIndex;
      const dot = s("circle", { cx: px(i), cy: y(v), r: last ? 5 : n > 30 ? 1.6 : 3, fill: last ? ACCENT : serie.color, stroke: last ? OUTLINE : "none", "stroke-width": 1.2 });
      dot.append(s("title", {}, `${series.length > 1 ? `${serie.name} · ` : ""}${rows[i][x]}：${formatNumber(v)}`));
      svgEl.append(dot);
    }
    svgEl.append(text(px(lastIndex) + 8, y(lastValue) + 4, shortNumber(lastValue), { fill: serie.color, "font-weight": 500 }));
  }
  let lx = margin.left;
  for (const serie of series) {
    const label = truncate(serie.name, 14);
    svgEl.append(
      s("line", { x1: lx, x2: lx + 18, y1: 10, y2: 10, stroke: serie.color, "stroke-width": 2 }),
      text(lx + 24, 14, label, { "font-family": "var(--sans)", "font-size": 12 }),
    );
    lx += 24 + label.length * 12.5 + 26;
  }
  return wrapFigure(svgEl, spec);
}

// 长表（对象 × 时间 × 指标）转成宽表：每个对象一列，时间按先后排序
function pivotSeries(spec, rows) {
  const measure = spec.y[0];
  const names = [...new Set(rows.map((row) => String(row[spec.series] ?? "—")))].slice(0, SERIES_COLORS.length);
  const byX = new Map();
  for (const row of rows) {
    const key = row[spec.x];
    if (key === null || key === undefined) continue;
    if (!byX.has(key)) byX.set(key, { [spec.x]: key });
    byX.get(key)[String(row[spec.series] ?? "—")] = row[measure];
  }
  const wide = [...byX.values()].sort((a, b) => (a[spec.x] > b[spec.x] ? 1 : a[spec.x] < b[spec.x] ? -1 : 0));
  return { names, wide };
}

// 饼图只画前 6 块，其余合并成“其他”；块从 12 点钟方向顺时针排，最大块用安全黄
const PIE_COLORS = [ACCENT, INK, SECOND, "#4f6f8a", "#b9c7d3", "#98a1ab", "#dcdfe3"];

function pieChart(spec, x, measure, rows) {
  const entries = rows
    .map((row) => ({ label: String(row[x] ?? "—"), value: isNumber(row[measure]) ? row[measure] : 0 }))
    .filter((entry) => entry.value > 0)
    .sort((a, b) => b.value - a.value);
  const head = entries.slice(0, 6);
  const rest = entries.slice(6).reduce((sum, entry) => sum + entry.value, 0);
  if (rest > 0) head.push({ label: "其他", value: rest });
  const total = head.reduce((sum, entry) => sum + entry.value, 0) || 1;
  const width = 760;
  const height = 280;
  const cx = 150;
  const cy = height / 2;
  const radius = 112;
  const inner = 58;
  const svgEl = s("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": spec.title || measure });
  let angle = -Math.PI / 2;
  const point = (r, a) => `${(cx + r * Math.cos(a)).toFixed(2)} ${(cy + r * Math.sin(a)).toFixed(2)}`;
  head.forEach((entry, index) => {
    const sweep = (entry.value / total) * Math.PI * 2;
    const end = angle + Math.min(sweep, Math.PI * 2 - 1e-4);
    const large = sweep > Math.PI ? 1 : 0;
    const path = [
      `M ${point(radius, angle)}`,
      `A ${radius} ${radius} 0 ${large} 1 ${point(radius, end)}`,
      `L ${point(inner, end)}`,
      `A ${inner} ${inner} 0 ${large} 0 ${point(inner, angle)}`,
      "Z",
    ].join(" ");
    const slice = s("path", { d: path, fill: PIE_COLORS[index % PIE_COLORS.length], stroke: "#ffffff", "stroke-width": 1.5 });
    slice.append(s("title", {}, `${entry.label}：${formatNumber(entry.value)}（${((entry.value / total) * 100).toFixed(1)}%）`));
    svgEl.append(slice);
    angle = end;
  });
  svgEl.append(
    text(cx, cy - 2, shortNumber(total), { "text-anchor": "middle", "font-size": 15, "font-weight": 700 }),
    text(cx, cy + 16, "合计", { "text-anchor": "middle", fill: MUTED, "font-family": "var(--sans)" }),
  );
  head.forEach((entry, index) => {
    const top = 34 + index * 30;
    svgEl.append(
      s("rect", { x: 320, y: top - 11, width: 14, height: 14, fill: PIE_COLORS[index % PIE_COLORS.length], stroke: index === 0 ? OUTLINE : "none" }),
      text(344, top, truncate(entry.label, 18), { "font-family": "var(--sans)", "font-size": 13 }),
      text(width - 12, top, `${formatNumber(entry.value)}  ${((entry.value / total) * 100).toFixed(1)}%`, { "text-anchor": "end" }),
    );
  });
  return wrapFigure(svgEl, spec);
}

function kpis(spec, rows) {
  const row = rows[0] || {};
  const wrap = h("div", { class: "kpis" });
  for (const name of spec.y || []) {
    wrap.append(h("div", { class: "kpi" }, h("span", { class: "value" }, formatNumber(row[name])), h("span", { class: "name" }, name)));
  }
  return wrap;
}

export const CHART_TYPES = [
  ["auto", "推荐"],
  ["bar", "柱状"],
  ["barh", "条形"],
  ["line", "折线"],
  ["pie", "饼图"],
  ["table", "仅表格"],
];

// 手动切换图表时推断坐标：维度取第一个非数值列（年份这类原样显示的数字列也算维度），指标取其余数值列
export function chartAxes(columns, rows, spec, rawColumn) {
  const numeric = (column) => rows.some((row) => isNumber(row[column])) && !rawColumn.test(column);
  const x = spec?.x && columns.includes(spec.x) ? spec.x : columns.find((column) => !numeric(column));
  const preferred = (spec?.y || []).filter((column) => columns.includes(column) && column !== x);
  const y = preferred.length ? preferred : columns.filter((column) => column !== x && numeric(column));
  return x && y.length && rows.length > 1 ? { x, y } : null;
}

export function renderChart(spec, columns, rows) {
  if (!spec || !rows || rows.length === 0) return null;
  if (spec.type === "kpi") return kpis(spec, rows);
  const measures = (spec.y || []).filter((m) => columns.includes(m));
  if (!columns.includes(spec.x) || measures.length === 0) return null;
  if (spec.series && columns.includes(spec.series)) {
    const { names, wide } = pivotSeries({ ...spec, y: measures }, rows);
    return lineChart({ ...spec, title: spec.title || `${measures[0]}（按${spec.series}）` }, spec.x, names, wide);
  }
  if (spec.type === "bar") return barChart(spec, spec.x, measures[0], rows);
  if (spec.type === "barh") return barhChart(spec, spec.x, measures[0], rows);
  if (spec.type === "line") return lineChart(spec, spec.x, measures, rows);
  if (spec.type === "pie") return pieChart(spec, spec.x, measures[0], rows);
  return null;
}
