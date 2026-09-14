// 工作流图：节点位置手工排版，边全部来自构建时导出的路由表（与 text2sql.agent.graph 一致）。
// 版式：主线居中；返工回路画在主线上方（红色），提前结束的出口走下方轨道，两类路线不交叉。

import { s } from "./dom.js";

const MAIN = ["understand", "link", "plan", "guard", "execute", "profile", "visualize", "narrate", "reflect", "finalize"];
const X0 = 84;
const STEP = 104;
const Y = 176;
const W = 94;
const H = 50;
const REPAIR_Y = 66;
const TOP_RAIL = 20;
const BOTTOM_RAIL = 292;

function positions() {
  const pos = {};
  MAIN.forEach((name, index) => (pos[name] = { x: X0 + index * STEP, y: Y }));
  pos.repair = { x: (pos.guard.x + pos.execute.x) / 2, y: REPAIR_Y };
  pos.__start__ = { x: 24, y: Y };
  pos.__end__ = { x: pos.finalize.x + 76, y: Y };
  return pos;
}

function marker(id, color) {
  return s(
    "marker",
    { id, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" },
    s("path", { d: "M0,0 L10,5 L0,10 z", fill: color }),
  );
}

function orthogonal(points, radius = 7) {
  // 折线转成带圆角的路径
  let d = `M${points[0][0]},${points[0][1]}`;
  for (let i = 1; i < points.length - 1; i += 1) {
    const [px, py] = points[i - 1];
    const [cx, cy] = points[i];
    const [nx, ny] = points[i + 1];
    const inX = Math.sign(cx - px);
    const inY = Math.sign(cy - py);
    const outX = Math.sign(nx - cx);
    const outY = Math.sign(ny - cy);
    d += ` L${cx - inX * radius},${cy - inY * radius} Q${cx},${cy} ${cx + outX * radius},${cy + outY * radius}`;
  }
  const last = points.at(-1);
  return `${d} L${last[0]},${last[1]}`;
}

function edgePath(source, target, pos) {
  const a = pos[source];
  const b = pos[target];
  const top = (p) => p.y - H / 2;
  const bottom = (p) => p.y + H / 2;
  if (source === "__start__") return { d: `M${a.x + 8},${a.y} L${b.x - W / 2},${b.y}`, kind: "" };
  if (target === "__end__") return { d: `M${a.x + W / 2},${a.y} L${b.x - 10},${b.y}`, kind: "" };

  if (target === "repair") {
    const side = source === "guard" ? -1 : 1;
    const startX = a.x - side * 14;
    const endX = b.x + side * (W / 2);
    return { d: `M${startX},${top(a)} C${startX},${b.y + 8} ${endX + side * 26},${b.y} ${endX},${b.y}`, kind: "loop" };
  }
  if (source === "repair" && target === "guard") {
    const startX = a.x - 22;
    return { d: `M${startX},${bottom(a)} C${startX},${b.y - 70} ${b.x - 30},${top(b) - 26} ${b.x - 30},${top(b)}`, kind: "loop" };
  }
  if (source === "repair" && target === "finalize") {
    return { d: orthogonal([[a.x, top(a)], [a.x, TOP_RAIL], [b.x, TOP_RAIL], [b.x, top(b)]]), kind: "exit" };
  }

  const adjacent = MAIN.indexOf(target) - MAIN.indexOf(source) === 1;
  if (adjacent) return { d: `M${a.x + W / 2},${a.y} L${b.x - W / 2},${b.y}`, kind: "" };
  if (target === "finalize") {
    return { d: orthogonal([[a.x, bottom(a)], [a.x, BOTTOM_RAIL], [b.x, BOTTOM_RAIL], [b.x, bottom(b)]]), kind: "exit" };
  }
  return { d: `M${a.x},${a.y} L${b.x},${b.y}`, kind: "" };
}

export function drawWorkflow(svgEl, routes, labels) {
  const pos = positions();
  svgEl.setAttribute("viewBox", `0 0 ${pos.__end__.x + 24} ${BOTTOM_RAIL + 34}`);
  svgEl.replaceChildren(
    s("defs", {}, marker("arrow", "#626c78"), marker("arrow-loop", "#d8342a"), marker("arrow-exit", "#98a1ab")),
  );

  const edges = [["__start__", "understand", false]];
  for (const [source, target] of Object.entries(routes.fixed)) edges.push([source, target, false]);
  for (const [source, targets] of Object.entries(routes.conditional)) {
    for (const target of targets) edges.push([source, target, true]);
  }

  const edgeLayer = s("g");
  for (const [source, target, conditional] of edges) {
    const { d, kind } = edgePath(source, target, pos);
    const classes = ["wf-edge", conditional ? "conditional" : "", kind].filter(Boolean).join(" ");
    const markerId = kind === "loop" ? "arrow-loop" : kind === "exit" ? "arrow-exit" : "arrow";
    edgeLayer.append(s("path", { d, class: classes, "marker-end": `url(#${markerId})`, dataset: { edge: `${source}>${target}` } }));
  }
  svgEl.append(edgeLayer);

  svgEl.append(
    s("circle", { cx: pos.__start__.x, cy: pos.__start__.y, r: 7, fill: "none", stroke: "#0f1318", "stroke-width": 1.2 }),
    s("circle", { cx: pos.__end__.x, cy: pos.__end__.y, r: 9, fill: "none", stroke: "#0f1318", "stroke-width": 1.2 }),
    s("circle", { cx: pos.__end__.x, cy: pos.__end__.y, r: 4.5, fill: "#0f1318" }),
  );

  const nodeLayer = s("g");
  for (const name of [...MAIN, "repair"]) {
    const { x, y } = pos[name];
    nodeLayer.append(
      s(
        "g",
        { class: `wf-node${name === "repair" ? " is-repair" : ""}`, dataset: { node: name } },
        s("rect", { x: x - W / 2, y: y - H / 2, width: W, height: H }),
        s("rect", { class: "bar", x: x - W / 2, y: y - H / 2, width: W, height: 5 }),
        s("text", { x, y: y - 2 }, labels[name] || name),
        s("text", { x, y: y + 15, class: "code" }, name),
      ),
    );
  }
  svgEl.append(nodeLayer);
  svgEl.append(
    s("text", { x: pos.link.x, y: BOTTOM_RAIL + 22, class: "wf-note", "text-anchor": "middle" }, "提前结束：拒答 · 无法编译 · 不可修复"),
    s("text", { x: pos.repair.x + W / 2 + 12, y: REPAIR_Y + 4, class: "wf-note wf-note-accent" }, "返工：报错 / 代价超限 / 可疑空结果，最多 2 次"),
  );
}

export function highlightPath(svgEl, trace) {
  const visited = new Set(trace.map((entry) => entry.node));
  svgEl.querySelectorAll(".wf-node").forEach((node) => node.classList.toggle("is-visited", visited.has(node.dataset.node)));
  const pairs = new Set();
  for (let i = 1; i < trace.length; i += 1) pairs.add(`${trace[i - 1].node}>${trace[i].node}`);
  if (trace.length) pairs.add(`__start__>${trace[0].node}`);
  if (trace.at(-1)?.node === "finalize") pairs.add("finalize>__end__");
  svgEl.querySelectorAll(".wf-edge").forEach((edge) => edge.classList.toggle("is-visited", pairs.has(edge.dataset.edge)));
}
