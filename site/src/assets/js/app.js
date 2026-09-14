import { CHART_TYPES, chartAxes, renderChart } from "./charts.js";
import { drawWorkflow, highlightPath } from "./diagram.js";
import { $, clear, csvFor, download, formatMs, formatNumber, formatPercent, h, isNumber } from "./dom.js";
import { EngineClient } from "./engine-client.js";
import { sqlBlock } from "./sql.js";

const BUILD = new URL(import.meta.url).searchParams.get("v") || "dev";

const NODE_LABELS = {
  understand: "理解意图",
  link: "Schema 链接",
  plan: "规划 SQL",
  guard: "安全门",
  execute: "执行",
  repair: "诊断修复",
  profile: "结果画像",
  visualize: "图表推荐",
  narrate: "生成解读",
  reflect: "质量检查",
  finalize: "收尾",
};
const STATUS = {
  answered: ["已回答", "tag-answered"],
  declined: ["暂不支持", "tag-declined"],
  rejected: ["已拒绝", "tag-rejected"],
  failed: ["执行失败", "tag-failed"],
};
const DETAIL_KEYS = {
  intent: "意图",
  task: "任务",
  tables: "表",
  planner: "规划",
  semantic_declined: "放弃原因",
  rows: "行",
  shape: "形态",
  facts: "事实",
  chart: "图表",
  source: "解读",
  score: "得分",
  status: "状态",
  follow_up: "改写",
  error: "错误",
  code: "诊断",
  attempt: "第几次",
};
const DETAIL_VALUES = {
  query: "查询",
  write_request: "写操作",
  injection: "注入",
  out_of_domain: "领域外",
  distribution: "分布",
  ranking: "排名",
  trend: "趋势",
  share: "占比",
  list: "名单",
  detail: "详情",
  aggregate: "汇总",
  semantic: "语义层",
  llm: "SQL 智能体",
  deterministic: "由结果生成",
  category_metric: "类别对比",
  time_series: "时间序列",
  scalar: "单值",
  single_row: "单行",
  table: "明细",
  empty: "空",
  bar: "柱状",
  barh: "条形",
  line: "折线",
  kpi: "指标卡",
  ok: "通过",
  answered: "已回答",
  declined: "放弃",
  rejected: "拒绝",
  failed: "失败",
};
// 年份、月份、代码这类维度列按原样显示，不加千分位
const RAW_COLUMN = /(年份|年度|年|月份|月|代码|编号|ID|Id|id|year|Year|YEAR|month|Month)$/;

const DEMO = "demo";
const UPLOAD = "upload";
const BOARD_KEY = "t2s.board.v1";
const BOARD_LIMIT = 24;
const BOARD_ROWS = 200;

const client = new EngineClient(`/assets/js/engine.worker.js?v=${BUILD}`);
const state = {
  site: null,
  threadId: newThreadId(),
  turns: 0,
  busy: false,
  mode: "semantic",
  llm: false,
  dataset: DEMO,
  workspace: null, // 引擎工作区里当前载入的数据集：{ id, summary }
  loading: null, // 正在载入的数据集：{ id, promise }
  board: loadBoard(),
};
const ROLE_LABELS = { time: "时间", measure: "数值", dimension: "类别", text: "文本" };
const PLANNER_LABELS = { semantic: "语义层编译", llm: "SQL 智能体", manual: "手工 SQL" };
const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;
const TOOL_LABELS = {
  search_schema: "检索相关表",
  describe_table: "查看表结构",
  get_column_values: "查看字段取值",
  validate_sql: "校验 SQL",
  preview_sql: "试运行 SQL",
  submit_sql: "提交 SQL",
};

function newThreadId() {
  const bytes = crypto.getRandomValues(new Uint8Array(4));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function humanDetail(detail) {
  const parts = [];
  for (const [key, raw] of Object.entries(detail || {})) {
    if (key === "ms" || key === "description") continue;
    if (key === "semantic_declined") {
      parts.push("语义层放弃");
      continue;
    }
    if (key === "mode") {
      parts.push(raw === "tool_calling" ? "函数调用" : "单轮提示");
      continue;
    }
    if (key === "steps") {
      parts.push(`${raw} 步`);
      continue;
    }
    if (key === "tokens") {
      parts.push(`${formatNumber(raw)} tokens`);
      continue;
    }
    if (key === "exemplars") {
      parts.push(`参考示例 ${raw.join("、")}`);
      continue;
    }
    let value = raw;
    if (Array.isArray(value)) value = value.map((v) => DETAIL_VALUES[v] ?? v).join("、");
    else if (typeof value === "object" && value !== null) continue;
    else value = DETAIL_VALUES[value] ?? value;
    if (value === "" || value === null || value === undefined) continue;
    parts.push(`${DETAIL_KEYS[key] ?? key} ${value}`);
  }
  return parts.join(" · ");
}

// ------------------------------------------------------------------ 引擎状态

function setEnginePill(stateName, label) {
  const pill = $("#engine-pill");
  pill.dataset.state = stateName;
  $("#engine-label").textContent = label;
}

client.addEventListener("state", ({ detail }) => {
  if (detail.state === "loading") setEnginePill("loading", "引擎加载中");
  if (detail.state === "error") {
    setEnginePill("error", "引擎加载失败");
    $("#boot-stage").textContent = `引擎加载失败：${detail.message}`;
  }
});

client.addEventListener("progress", ({ detail }) => {
  const boot = $("#boot");
  boot.hidden = detail.stage === "ready";
  if (state.llm && client.state !== "ready" && detail.stage !== "ready") {
    $("#mode-auto-note").textContent = `Workers AI 已连接 · 引擎加载 ${Math.round(detail.ratio * 100)}%`;
  }
  $("#boot-stage").textContent = detail.label;
  $("#boot-bar").style.width = `${Math.round(detail.ratio * 100)}%`;
  $("#boot-size").textContent = detail.total
    ? `${(detail.received / 1048576).toFixed(1)} / ${(detail.total / 1048576).toFixed(1)} MB`
    : "";
});

function applyModelState(available, note) {
  state.llm = available;
  $('.mode[data-mode="auto"]').disabled = !available;
  $("#mode-auto-note").textContent = note;
  if (available && state.turns <= 1) selectMode("auto", { quiet: true });
}

// 模型是否可用只取决于 Worker 有没有绑定 Workers AI，页面打开就检测，不必等二十多 MB 的引擎下载完
async function detectModel() {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  try {
    const health = await (await fetch("/api/health", { cache: "no-store", signal: controller.signal })).json();
    if (client.state !== "ready") {
      applyModelState(Boolean(health.ai), health.ai ? "Workers AI 已连接" : "本环境未接模型");
    }
  } catch {
    if (client.state !== "ready") applyModelState(false, "模型接口不可达");
  } finally {
    clearTimeout(timer);
  }
}

function bootEngine() {
  if (client.state !== "idle") return client.bootPromise;
  $("#boot").hidden = false;
  const promise = client.boot();
  promise
    .then((info) => {
      const seconds = (info.boot_total_ms / 1000).toFixed(1);
      setEnginePill("ready", `就绪 · Python ${info.python} · DuckDB ${info.duckdb}`);
      $("#spec-engine").textContent =
        `Python ${info.python} · DuckDB ${info.duckdb} · sqlglot ${info.sqlglot} · 启动 ${seconds} s`;
      // 以引擎实际创建的智能体为准（引擎启动时自己也检测了一次模型接口）
      const available = (info.modes || []).includes("auto");
      applyModelState(available, available ? "Workers AI 已连接" : "本环境未接模型");
      if (state.site && state.dataset === DEMO) renderExamples(state.site.examples);
    })
    .catch(() => {});
  return promise;
}

function selectMode(mode, { quiet = false } = {}) {
  if (mode === state.mode) return;
  state.mode = mode;
  document.querySelectorAll(".mode").forEach((button) => {
    button.setAttribute("aria-checked", button.dataset.mode === mode ? "true" : "false");
  });
  if (!quiet) {
    client.reset(state.threadId);
    state.threadId = newThreadId();
    updateThreadLabel();
  }
}

// ------------------------------------------------------------------ 一轮问答

function createTurn(question, meta) {
  state.turns += 1;
  const ledger = h("ol");
  const total = h("div", { class: "ledger-total" });
  const main = h("div", { class: "turn-main" }, h("p", { class: "muted small" }, "等待引擎……"));
  const metaEl = h("span", { class: "turn-meta" }, meta);
  const turn = h(
    "article",
    { class: "turn" },
    h(
      "header",
      { class: "turn-head" },
      h("span", { class: "turn-index" }, h("b", {}, `Q${state.turns}`)),
      h("h3", { class: "turn-question" }, question),
      metaEl,
    ),
    h(
      "div",
      { class: "turn-body" },
      h(
        "aside",
        { class: "ledger", "aria-label": "执行账本" },
        h("div", { class: "ledger-title" }, h("span", { class: "label" }, "执行账本"), h("span", { class: "label" }, "ms")),
        ledger,
        total,
      ),
      main,
    ),
  );
  return { turn, ledger, total, main, metaEl, count: 0 };
}

function addLedgerRow(view, entry) {
  view.count += 1;
  const ok = entry.status === "ok" || entry.status === "answered";
  const detail = humanDetail(entry.detail);
  const reason = entry.detail?.semantic_declined;
  const row = h(
    "li",
    { class: ok ? "done" : "done warn", title: reason ? `语义层放弃：${reason}` : detail },
    h("span", { class: "n" }, String(view.count).padStart(2, "0")),
    h("span", { class: "label-text" }, NODE_LABELS[entry.node] || entry.node),
    h("span", { class: "ms" }, formatMs(entry.ms)),
    detail ? h("span", { class: "detail" }, detail) : null,
  );
  // SQL 智能体的工具调用发生在规划 / 修复节点内部，节点完成时替换掉占位行，让步骤排在节点下面
  if (view.planning && (entry.node === "plan" || entry.node === "repair")) {
    view.ledger.replaceChild(row, view.planning);
    view.planning = null;
  } else {
    view.ledger.append(row);
  }
}

function addStepRow(view, step) {
  if (!view.planning) {
    view.planning = h(
      "li",
      { class: "live" },
      h("span", { class: "n" }, "··"),
      h("span", { class: "label-text" }, "SQL 智能体进行中"),
      h("span", { class: "ms" }, "…"),
    );
    view.ledger.append(view.planning);
  }
  view.ledger.append(
    h(
      "li",
      { class: step.ok ? "step" : "step fail", title: step.summary || "" },
      h("span", { class: "n" }, `↳${step.index}`),
      h("span", { class: "label-text" }, step.tool),
      h("span", { class: "ms" }, formatMs(step.latency_ms)),
      h("span", { class: "detail" }, step.summary || ""),
    ),
  );
  view.ledger.lastElementChild.classList.add("done");
}

function finishLedger(view, result) {
  const visited = new Set(result.trace.map((entry) => entry.node));
  const order = state.site?.routes?.nodes || Object.keys(NODE_LABELS);
  for (const node of order) {
    if (visited.has(node)) continue;
    view.ledger.append(
      h(
        "li",
        { class: "skipped" },
        h("span", { class: "n" }, "··"),
        h("span", { class: "label-text" }, NODE_LABELS[node]),
        h("span", { class: "ms" }, "—"),
      ),
    );
  }
  const nodeMs = result.trace.reduce((sum, entry) => sum + (entry.ms || 0), 0);
  clear(view.total).append(h("span", {}, "节点合计"), h("span", {}, `${formatMs(nodeMs)} ms`));
}

function renderTable(columns, rows, { limit = 200, dimension = null } = {}) {
  const numeric = columns.map(
    (column) => column !== dimension && !RAW_COLUMN.test(column) && rows.some((row) => isNumber(row[column])),
  );
  const head = h("tr", {}, columns.map((column, i) => h("th", { class: numeric[i] ? "num" : "" }, column)));
  const body = rows.slice(0, limit).map((row) =>
    h(
      "tr",
      {},
      columns.map((column, i) => {
        const value = row[column];
        if (value === null || value === undefined) return h("td", { class: `null ${numeric[i] ? "num" : ""}` }, "—");
        return h("td", { class: numeric[i] ? "num" : "" }, numeric[i] ? formatNumber(value) : String(value));
      }),
    ),
  );
  return h("div", { class: "table-wrap" }, h("table", { class: "data" }, h("thead", {}, head), h("tbody", {}, body)));
}

function tabs(definitions) {
  const bar = h("div", { class: "tabs", role: "tablist" });
  const panels = [];
  definitions.forEach(([label, render], index) => {
    const panel = h("div", { class: "tab-panel", role: "tabpanel", hidden: index !== 0 });
    let rendered = false;
    const button = h("button", { class: "tab", type: "button", role: "tab", "aria-selected": index === 0 ? "true" : "false" }, label);
    const show = () => {
      bar.querySelectorAll(".tab").forEach((tab) => tab.setAttribute("aria-selected", "false"));
      panels.forEach((p) => (p.hidden = true));
      button.setAttribute("aria-selected", "true");
      panel.hidden = false;
      if (!rendered) {
        panel.append(...[render()].flat().filter(Boolean));
        rendered = true;
      }
    };
    button.addEventListener("click", show);
    bar.append(button);
    panels.push(panel);
    if (index === 0) {
      panel.append(...[render()].flat().filter(Boolean));
      rendered = true;
    }
  });
  return [bar, ...panels];
}

function answerList(text) {
  const lines = String(text || "")
    .split("\n")
    .map((line) => line.replace(/^\s*[-*]\s*/, "").trim())
    .filter(Boolean);
  return h("div", { class: "answer" }, h("ul", {}, lines.map((line) => h("li", {}, line))));
}

// 当前选中的图表类型对应的规格：推荐 = 智能体给的规格；手动类型沿用它的坐标，没有时按列类型推断
function chartSpec(result, type) {
  if (type === "table") return null;
  const axes = result.chart?.series ? null : chartAxes(result.columns, result.rows, result.chart, RAW_COLUMN);
  if (type === "auto" || !axes) return result.chart;
  return { type, x: axes.x, y: type === "line" ? axes.y.slice(0, 2) : axes.y.slice(0, 1), title: result.chart?.title || "", reason: "" };
}

function resultPanel(result, view) {
  const holder = h("div", { class: "chart-holder" });
  // 多序列折线（长表）只提供推荐图与表格：换成柱状或饼图会把不同对象的数值混在一起
  const axes = result.chart?.series ? null : chartAxes(result.columns, result.rows, result.chart, RAW_COLUMN);
  const options = CHART_TYPES.filter(([type]) => type === "table" || (type === "auto" ? result.chart : axes));
  const switcher = h("div", { class: "chart-switch", role: "group", "aria-label": "图表类型" }, h("span", { class: "label" }, "图表"));
  const draw = (type) => {
    view.chartType = type;
    const chart = renderChart(chartSpec(result, type), result.columns, result.rows);
    clear(holder);
    if (chart) holder.append(chart);
    switcher.querySelectorAll("button").forEach((button) => button.setAttribute("aria-pressed", button.dataset.type === type ? "true" : "false"));
  };
  for (const [type, label] of options) {
    switcher.append(h("button", { type: "button", dataset: { type }, "aria-pressed": "false", onClick: () => draw(type) }, label));
  }
  draw(view.chartType || (result.chart ? "auto" : "table"));
  const foot = h(
    "div",
    { class: "panel-foot" },
    h("span", {}, `共 ${formatNumber(result.row_count)} 行${result.truncated ? "，已达到行数上限" : ""}`),
    h("button", { class: "link-btn", type: "button", onClick: () => download("result.csv", csvFor(result.columns, result.rows), "text/csv;charset=utf-8") }, "下载 CSV"),
    result.report
      ? h("button", { class: "link-btn", type: "button", onClick: () => download("report.md", result.report, "text/markdown;charset=utf-8") }, "下载分析报告")
      : null,
  );
  return [options.length > 1 ? switcher : null, holder, renderTable(result.columns, result.rows, { dimension: result.chart?.x }), foot];
}

// 质检编号：SQL 文本的短哈希，同一条 SQL 编号不变
function inspectionNo(text) {
  let hash = 2166136261;
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(16).toUpperCase().padStart(8, "0").slice(0, 6);
}

function sqlEditor(view, result, panel) {
  const original = result.safe_sql || result.sql || "";
  const area = h("textarea", { class: "sql-editor", spellcheck: "false", rows: String(Math.min(16, original.split("\n").length + 3)), "aria-label": "SQL" });
  area.value = original;
  const status = h("span", { class: "small" });
  const run = async (button) => {
    button.disabled = true;
    status.className = "small muted";
    status.textContent = "安全检查并执行……";
    try {
      const rerun = await client.runSql(area.value, view.context.mode);
      if (rerun.status !== "answered") {
        status.className = "small check-fail";
        status.textContent = `${STATUS[rerun.status]?.[0] || rerun.status}：${rerun.message}`;
        return;
      }
      renderResult(view, {
        ...result,
        ...rerun,
        question: result.question,
        effective_question: result.effective_question,
        planner: "manual",
        description: "手工修改的 SQL，直接执行，没有经过语义层或 SQL 智能体",
        assumptions: [],
        quality: null,
        agent_steps: [],
        suggestions: [],
        report: "",
      });
    } catch (error) {
      status.className = "small check-fail";
      status.textContent = `执行出错：${error.message}`;
    } finally {
      button.disabled = false;
    }
  };
  clear(panel).append(
    h("div", { class: "sql-edit" }, area),
    h(
      "div",
      { class: "sql-edit-bar" },
      h("button", { class: "btn", type: "button", onClick: (event) => run(event.currentTarget) }, "运行修改后的 SQL"),
      h("button", { class: "link-btn", type: "button", onClick: () => clear(panel).append(...sqlPanel(result, view, panel)) }, "取消"),
      status,
    ),
    h("p", { class: "muted small" }, "修改后的 SQL 与智能体生成的 SQL 走同一道安全门：只读、表白名单、字段校验、个人信息拦截、行数上限。"),
  );
  area.focus();
}

function sqlPanel(result, view, panel) {
  const nodes = [sqlBlock(result.safe_sql || result.sql || "")];
  if (view && (result.safe_sql || result.sql)) {
    nodes.push(
      h(
        "div",
        { class: "sql-edit-bar" },
        h(
          "button",
          {
            class: "btn btn-ghost",
            type: "button",
            onClick: (event) => sqlEditor(view, result, panel || event.currentTarget.closest(".tab-panel")),
          },
          "修改 SQL 并重新运行",
        ),
      ),
    );
  }
  const guard = result.guard || {};
  const notes = h("ul");
  for (const change of guard.modifications || []) notes.append(h("li", {}, `安全门改写：${change}`));
  for (const warning of guard.warnings || []) notes.append(h("li", {}, `提示：${warning}`));
  if (!notes.childElementCount) notes.append(h("li", {}, "安全门未做改写。"));
  const tables = (guard.referenced_tables || []).join("、");
  if (tables) notes.append(h("li", { class: "muted" }, `访问的表：${tables}`));
  nodes.push(
    h(
      "div",
      { class: "guard-line" },
      guard.is_safe
        ? h(
            "span",
            { class: "qc-tag", "aria-label": "质检通过：只读，已校验" },
            h("span", {}, h("b", {}, "质检通过"), h("small", {}, `只读 · QC-${inspectionNo(result.safe_sql || result.sql || "")}`)),
          )
        : null,
      notes,
    ),
  );
  if (result.executed_sql && result.executed_sql !== result.safe_sql) {
    nodes.push(h("details", { class: "dialect" }, h("summary", {}, "演示库实际执行的 DuckDB 方言"), sqlBlock(result.executed_sql, { copy: false })));
  }
  return nodes;
}

function scopePanel(result) {
  return h(
    "div",
    { class: "scope" },
    result.description ? h("p", {}, h("b", {}, "统计口径　"), result.description) : null,
    (result.assumptions || []).length ? h("ul", {}, result.assumptions.map((note) => h("li", {}, note))) : null,
    h(
      "p",
      { class: "muted small", style: "margin-top:14px" },
      result.narrative_source === "llm" ? "解读由模型生成，其中每个数字都已与查询结果核对。" : "解读由查询结果直接生成，没有经过模型。",
    ),
  );
}

function qualityPanel(result) {
  const quality = result.quality || { checks: [] };
  const marks = { pass: ["通过", "check-pass"], warn: ["注意", "check-warn"], fail: ["未通过", "check-fail"] };
  return [
    h("div", { class: "score" }, h("b", {}, (quality.score ?? 0).toFixed(2)), h("span", { class: "muted small" }, "质量得分")),
    h(
      "ul",
      { class: "checks" },
      quality.checks.map((check) => {
        const [label, cls] = marks[check.status] || [check.status, ""];
        return h("li", {}, h("span", { class: `check-mark ${cls}` }, label), h("span", {}, check.detail));
      }),
    ),
  ];
}

function stepsPanel(result) {
  return h(
    "ol",
    { class: "steps" },
    result.agent_steps.map((step) => {
      const args = step.arguments || {};
      const body = [];
      if (args.sql) body.push(sqlBlock(args.sql, { copy: false }));
      const rest = Object.fromEntries(Object.entries(args).filter(([key]) => key !== "sql"));
      if (Object.keys(rest).length) body.push(h("pre", {}, JSON.stringify(rest, null, 2)));
      return h(
        "li",
        {},
        h(
          "div",
          { class: "step-head" },
          h("span", { class: "mono muted" }, `#${step.index}`),
          h("b", {}, step.tool),
          h("span", { class: "muted" }, TOOL_LABELS[step.tool] || ""),
          h("span", { class: step.ok ? "check-pass mono small" : "check-fail mono small" }, step.ok ? "成功" : "失败"),
          h("span", { class: "mono muted small" }, `${formatMs(step.latency_ms)} ms`),
        ),
        h("div", { class: "small" }, step.summary || ""),
        body.length ? h("div", { class: "step-args" }, body) : null,
      );
    }),
  );
}

function resultActions(view, result) {
  const bar = h("div", { class: "result-actions" });
  bar.append(h("button", { class: "act", type: "button", onClick: (event) => pinResult(view, result, event.currentTarget) }, "钉到看板"));
  // 模型写的 SQL 经人确认后存进示例库；语义层编译的 SQL 本来就是确定的，不需要
  if (result.planner !== "semantic" && view.context.mode !== "semantic" && state.llm) {
    bar.append(
      h(
        "button",
        {
          class: "act",
          type: "button",
          title: "加入当前数据集的示例库，之后相似的问题会在提示词里参考这条 SQL（只在本页有效）",
          onClick: (event) => markCorrect(event.currentTarget, view, result),
        },
        "结果正确，存为示例",
      ),
    );
  }
  return bar;
}

async function markCorrect(button, view, result) {
  button.disabled = true;
  try {
    const saved = await client.remember(result.effective_question || result.question, result.safe_sql || result.sql, view.context.mode);
    button.textContent = saved.error ? `未保存：${saved.error}` : `已存为示例 · 本页共 ${saved.exemplars} 条`;
  } catch (error) {
    button.textContent = `未保存：${error.message}`;
    button.disabled = false;
  }
}

function followUps(questions) {
  return h(
    "div",
    { class: "follow-ups" },
    h("span", { class: "label" }, "接着问"),
    questions.map((question) =>
      h(
        "button",
        {
          class: "chip",
          type: "button",
          onClick: () => {
            $("#ask-input").value = question;
            ask(question);
          },
        },
        question,
      ),
    ),
  );
}

function renderResult(view, result) {
  const main = clear(view.main);
  view.result = result;
  view.chartType = null;
  const [statusLabel, statusClass] = STATUS[result.status] || [result.status, "tag-failed"];
  const verdict = h("div", { class: "verdict" }, h("span", { class: `tag ${statusClass}` }, statusLabel));
  if (result.planner) verdict.append(h("span", { class: "tag tag-planner" }, PLANNER_LABELS[result.planner] || result.planner));
  if (result.effective_question && result.effective_question !== result.question) {
    verdict.append(h("span", { class: "rewrite" }, "已理解为 ", h("b", {}, result.effective_question)));
  }
  main.append(verdict);

  if (result.status !== "answered") {
    const notice = h("div", { class: `notice ${result.status === "declined" ? "" : "is-danger"}` }, h("p", {}, result.message || result.error || "没有得到结果"));
    if (result.status === "declined" && state.llm && view.context?.mode === "semantic") {
      notice.append(
        h(
          "p",
          { class: "small", style: "margin-top:8px" },
          "切换到“语义层优先，长尾交给 SQL 智能体”后，这类问题会由 Workers AI 驱动的 SQL 智能体尝试回答。",
        ),
      );
    }
    if ((result.agent_steps || []).length) {
      notice.append(h("details", { class: "dialect" }, h("summary", {}, "SQL 智能体的尝试过程"), stepsPanel(result)));
    }
    if ((result.suggestions || []).length) {
      notice.append(
        h(
          "div",
          { class: "suggest" },
          h("span", { class: "muted" }, "可以试试："),
          result.suggestions.map((q) => h("button", { class: "link-btn", type: "button", onClick: () => ask(q) }, q)),
        ),
      );
    }
    main.append(notice);
    return;
  }

  main.append(answerList(result.answer));
  if (view.context) main.append(resultActions(view, result));
  const definitions = [
    ["结果", () => resultPanel(result, view)],
    ["SQL", () => sqlPanel(result, view)],
    ["口径", () => scopePanel(result)],
  ];
  if (result.quality) definitions.push(["质量检查", () => qualityPanel(result)]);
  if ((result.agent_steps || []).length) definitions.push(["智能体步骤", () => stepsPanel(result)]);
  main.append(...tabs(definitions));
  if ((result.suggestions || []).length && view.context) main.append(followUps(result.suggestions));
}

function consoleNotice(title, message) {
  const view = createTurn(title, datasetMeta(state.dataset).title);
  clear(view.main).append(h("div", { class: "notice" }, h("p", {}, message)));
  $("#thread").prepend(view.turn);
}

async function ask(question) {
  const text = String(question || "").trim();
  if (!text || state.busy) return;
  const dataset = state.dataset;
  if (dataset === UPLOAD && state.workspace?.id !== UPLOAD) {
    return consoleNotice(text, "先把文件拖进上方的区域，导入之后再提问。");
  }
  if (dataset !== DEMO && client.state === "ready" && !state.llm) {
    return consoleNotice(text, "这个数据集没有手写的语义层，需要 SQL 智能体回答，而本环境没有接入模型。");
  }
  state.busy = true;
  $("#ask-button").disabled = true;
  const title = datasetMeta(dataset).title;
  const view = createTurn(text, `会话 ${state.threadId} · ${title}`);
  view.context = { dataset, mode: dataset === DEMO ? state.mode : "workspace" };
  $("#thread").prepend(view.turn);
  view.turn.scrollIntoView({ behavior: "smooth", block: "start" });
  bootEngine();
  try {
    await client.bootPromise;
    if (dataset !== DEMO) await ensureDataset(dataset);
    clear(view.main).append(h("p", { class: "muted small" }, "正在执行……"));
    const started = performance.now();
    const result = await client.ask(
      text,
      state.threadId,
      (event) => {
        if (event.type === "node") addLedgerRow(view, event);
        else if (event.type === "agent_step") addStepRow(view, event);
      },
      view.context.mode,
    );
    if (view.count === 0) result.trace.forEach((entry) => addLedgerRow(view, entry));
    finishLedger(view, result);
    const usage = result.usage || {};
    const modelNote = usage.calls ? ` · 模型 ${usage.calls} 次 · ${formatNumber(usage.total_tokens || 0)} tokens` : "";
    view.metaEl.textContent = `会话 ${state.threadId} · ${title} · 往返 ${Math.round(performance.now() - started)} ms${modelNote}`;
    renderResult(view, result);
    highlightPath($("#workflow"), result.trace);
    updateThreadLabel();
  } catch (error) {
    clear(view.main).append(h("div", { class: "notice is-danger" }, h("p", {}, `执行出错：${error.message}`)));
  } finally {
    state.busy = false;
    $("#ask-button").disabled = false;
  }
}

function updateThreadLabel() {
  $("#thread-label").textContent = `会话 ${state.threadId}`;
}

// ------------------------------------------------------------------ 数据集与上传

const DATASET_HINTS = {
  [DEMO]: ["例如：近三年每年的融资事件数", "同一会话内可以追问：先问“广州市存续企业有多少家”，再问“那深圳呢”。"],
  [UPLOAD]: ["例如：按城市统计销售额的合计", "上传的数据由 SQL 智能体回答：先检索表、确认取值、试运行，再提交 SQL。"],
};

function datasetMeta(id) {
  if (id === DEMO) return { id, title: "智能制造企业", kind: "demo" };
  if (id === UPLOAD) return { id, title: "你的文件", kind: "upload" };
  return (state.site?.datasets || []).find((dataset) => dataset.id === id) || { id, title: id };
}

function formatBytes(bytes) {
  return bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function renderDatasetStrip(datasets) {
  const tab = (id, title, note) =>
    h(
      "button",
      {
        class: "dataset",
        type: "button",
        role: "tab",
        dataset: { dataset: id },
        "aria-selected": state.dataset === id ? "true" : "false",
        onClick: () => selectDataset(id),
      },
      h("b", {}, title),
      h("small", {}, note),
    );
  clear($("#datasets")).append(
    tab(DEMO, "智能制造企业", "合成 · 手写语义层"),
    ...datasets.map((d) => tab(d.id, d.title, `${d.kind === "open" ? `开源 · ${d.license.name}` : "示例 · 合成"} · ${d.tables.length} 张表`)),
    tab(UPLOAD, "你的文件", "CSV · Excel · Parquet"),
  );
}

// 载入内置数据集到引擎工作区；同一个数据集重复调用时复用
function ensureDataset(id) {
  if (state.workspace?.id === id) return Promise.resolve(state.workspace.summary);
  if (state.loading?.id === id) return state.loading.promise;
  bootEngine();
  const promise = client
    .loadDataset(id)
    .then((summary) => {
      if (summary.error) throw new Error(summary.error);
      state.workspace = { id, summary };
      if (state.dataset === id) renderSheet(id);
      return summary;
    })
    .finally(() => {
      if (state.loading?.id === id) state.loading = null;
    });
  state.loading = { id, promise };
  return promise;
}

function selectDataset(id) {
  if (id === state.dataset || state.busy) return;
  state.dataset = id;
  document.querySelectorAll(".dataset").forEach((tab) => {
    tab.setAttribute("aria-selected", tab.dataset.dataset === id ? "true" : "false");
  });
  const meta = datasetMeta(id);
  const [placeholder, hint] = DATASET_HINTS[id] || [
    `例如：${meta.suggestions?.[0] || ""}`,
    "内置数据集由 SQL 智能体回答：表的中文名、表间关联和业务口径来自数据卡片，字段取值来自导入时的画像。",
  ];
  $("#upload").hidden = id !== UPLOAD;
  $("#modes").hidden = id !== DEMO;
  $("#ask-input").value = "";
  $("#ask-input").placeholder = placeholder;
  $("#follow-hint").textContent = hint;
  client.reset(state.threadId);
  state.threadId = newThreadId();
  updateThreadLabel();
  renderSheet(id);
  if (id === DEMO) {
    renderExamples(state.site.examples);
  } else if (id === UPLOAD) {
    renderSuggestions(state.workspace?.id === UPLOAD ? state.workspace.summary.suggestions : []);
    bootEngine();
  } else {
    renderSuggestions(meta.suggestions);
    ensureDataset(id).catch((error) => {
      if (state.dataset === id) clear($("#sheet-status")).append(h("span", { class: "check-fail" }, `载入失败：${error.message}`));
    });
  }
}

function columnChip(column) {
  const label = column.label && column.label !== column.name ? column.label : "";
  return h(
    "span",
    {
      class: `column-chip${column.sensitive ? " is-sensitive" : ""}`,
      title: column.sensitive ? "疑似个人信息，SQL 不能读取" : `${column.type}，${formatNumber(column.distinct)} 个不同取值${column.note ? `；${column.note}` : ""}`,
    },
    label ? [h("b", {}, label), h("code", {}, column.name)] : column.name,
    h("i", {}, column.sensitive ? "个人信息" : ROLE_LABELS[column.role] || column.role),
  );
}

// 表结构浏览：载入前只有表名和行数，载入后展开能看到每个字段的中文名、类型角色和个人信息标记
function schemaTables(tables, { open = false } = {}) {
  return h(
    "div",
    { class: "schema" },
    tables.map((table, index) =>
      h(
        "details",
        { class: "schema-table", open: open && index === 0 },
        h(
          "summary",
          {},
          h("b", {}, table.label || table.name),
          table.label && table.label !== table.name ? h("code", {}, table.name) : null,
          h("span", { class: "mono" }, `${formatNumber(table.rows)} 行${table.columns ? ` · ${table.columns.length} 列` : ""}`),
        ),
        table.description ? h("p", { class: "muted small" }, table.description) : null,
        table.columns
          ? h("div", { class: "columns-list" }, table.columns.map(columnChip))
          : h("p", { class: "muted small" }, "数据集载入后显示字段。"),
      ),
    ),
  );
}

function renderSheet(id) {
  const sheet = clear($("#sheet"));
  sheet.hidden = id === DEMO || id === UPLOAD;
  if (sheet.hidden) return;
  const meta = datasetMeta(id);
  const loaded = state.workspace?.id === id ? state.workspace.summary : null;
  const labels = Object.fromEntries((meta.tables || []).map((table) => [table.name, table.label]));
  const side = (ref) => {
    const [table, column] = String(ref).split(".");
    return `${labels[table] || table}.${column}`;
  };
  sheet.append(
    h(
      "div",
      { class: "sheet-head" },
      h("h3", {}, meta.title),
      h("span", { class: "sheet-tag is-license" }, meta.license?.name || ""),
      h("span", { class: "sheet-tag" }, meta.kind === "open" ? "开源数据" : "示例数据"),
      h("span", { class: "mono small muted" }, `${formatNumber(meta.rows)} 行 · ${formatBytes(meta.bytes)}`),
      h("span", { class: "spacer" }),
      h("a", { class: "sheet-source", href: meta.source?.url, target: "_blank", rel: "noopener" }, `来源 ${meta.source?.name} ↗`),
    ),
    h("p", { class: "sheet-summary" }, meta.summary),
    schemaTables(loaded ? loaded.tables : meta.tables),
    h(
      "details",
      { class: "sheet-rules" },
      h("summary", {}, `业务口径 ${meta.rules.length} 条 · 表间关联 ${meta.relationships.length} 条`),
      meta.rules.length ? h("ol", {}, meta.rules.map((rule) => h("li", {}, rule))) : null,
      meta.relationships.length
        ? h("ul", { class: "relations" }, meta.relationships.map((rel) => h("li", {}, h("code", {}, side(rel.from)), " → ", h("code", {}, side(rel.to)), rel.note ? `（${rel.note}）` : "")))
        : null,
    ),
    h("div", { class: "sheet-status small", id: "sheet-status" }, loaded ? `已载入引擎 · 数据截至 ${loaded.anchor_year} 年` : client.state === "ready" ? "载入中……" : "引擎就绪后自动载入"),
  );
}

function renderUploadSummary(summary) {
  const target = clear($("#upload-summary"));
  if (summary.error) {
    target.append(h("div", { class: "upload-error" }, summary.error));
    return;
  }
  target.append(schemaTables(summary.tables.map((table) => ({ ...table, description: `来自 ${table.source}` })), { open: true }));
}

async function importUpload(files) {
  const status = $("#upload-status");
  status.textContent = client.state === "ready" ? `导入 ${files.length} 个文件…` : "等待引擎就绪…";
  try {
    const summary = await client.upload(files);
    renderUploadSummary(summary);
    status.textContent = summary.error ? "导入失败" : `已导入 ${summary.tables.length} 张表`;
    if (!summary.error) {
      state.workspace = { id: UPLOAD, summary };
      if (state.dataset === UPLOAD) renderSuggestions(summary.suggestions);
    }
    client.reset(state.threadId);
    state.threadId = newThreadId();
    updateThreadLabel();
  } catch (error) {
    renderUploadSummary({ error: `导入失败：${error.message}` });
    status.textContent = "";
  }
}

function handleFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  const tooLarge = files.find((file) => file.size > MAX_UPLOAD_BYTES);
  if (tooLarge) {
    renderUploadSummary({ error: `${tooLarge.name} 超过 50 MB，浏览器内分析建议先抽样或只保留需要的列。` });
    return;
  }
  importUpload(files);
}

function wireUpload() {
  const zone = $("#dropzone");
  $("#file-input").addEventListener("change", (event) => handleFiles(event.target.files));
  zone.addEventListener("dragover", (event) => {
    event.preventDefault();
    zone.classList.add("is-over");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("is-over"));
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    zone.classList.remove("is-over");
    handleFiles(event.dataTransfer?.files);
  });
}

// ------------------------------------------------------------------ 看板

function loadBoard() {
  try {
    const cards = JSON.parse(localStorage.getItem(BOARD_KEY) || "[]");
    return Array.isArray(cards) ? cards : [];
  } catch {
    return [];
  }
}

function saveBoard() {
  try {
    localStorage.setItem(BOARD_KEY, JSON.stringify(state.board));
    return true;
  } catch {
    return false; // 隐私模式或存储已满：看板只在本次页面里有效
  }
}

function pinResult(view, result, button) {
  const card = {
    id: `${Date.now().toString(36)}${newThreadId()}`,
    dataset: view.context.dataset,
    title: datasetMeta(view.context.dataset).title,
    question: result.question,
    sql: result.safe_sql || result.sql,
    mode: view.context.mode,
    columns: result.columns,
    rows: result.rows.slice(0, BOARD_ROWS),
    rowCount: result.row_count,
    chart: chartSpec(result, view.chartType || (result.chart ? "auto" : "table")),
    pinnedAt: new Date().toISOString(),
  };
  state.board = [card, ...state.board].slice(0, BOARD_LIMIT);
  const persisted = saveBoard();
  renderBoard();
  button.textContent = persisted ? "已钉到看板" : "已钉到看板（本页有效）";
  button.disabled = true;
}

function timeLabel(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

async function refreshCard(card, status) {
  if (card.dataset === UPLOAD && state.workspace?.id !== UPLOAD) {
    status.textContent = "上传的文件只在导入它的页面里，重新上传后才能刷新";
    return;
  }
  status.textContent = "执行中……";
  try {
    bootEngine();
    await client.bootPromise;
    if (card.dataset !== DEMO && card.dataset !== UPLOAD) await ensureDataset(card.dataset);
    const result = await client.runSql(card.sql, card.dataset === DEMO ? "semantic" : "workspace");
    if (result.status !== "answered") throw new Error(result.message);
    Object.assign(card, { columns: result.columns, rows: result.rows.slice(0, BOARD_ROWS), rowCount: result.row_count, refreshedAt: new Date().toISOString() });
    saveBoard();
    renderBoard();
  } catch (error) {
    status.textContent = `刷新失败：${error.message}`;
  }
}

function boardCard(card) {
  const body = h("div", { class: "board-body" }, renderChart(card.chart, card.columns, card.rows) || renderTable(card.columns, card.rows, { limit: 8 }));
  const status = h("span", { class: "small muted board-status" });
  const sql = h("div", { class: "board-sql", hidden: true }, sqlBlock(card.sql || "", { copy: true }));
  const when = card.refreshedAt ? `刷新于 ${timeLabel(card.refreshedAt)}` : `钉于 ${timeLabel(card.pinnedAt)}`;
  return h(
    "article",
    { class: "board-card" },
    h("header", {}, h("span", { class: "board-dataset" }, card.title), h("h3", {}, card.question)),
    body,
    sql,
    h(
      "footer",
      {},
      h("span", { class: "mono small muted" }, `${formatNumber(card.rowCount)} 行 · ${when}`),
      status,
      h("span", { class: "spacer" }),
      h("button", { class: "link-btn", type: "button", onClick: () => refreshCard(card, status) }, "刷新"),
      h("button", { class: "link-btn", type: "button", onClick: () => (sql.hidden = !sql.hidden) }, "SQL"),
      h(
        "button",
        {
          class: "link-btn",
          type: "button",
          onClick: () => {
            state.board = state.board.filter((item) => item.id !== card.id);
            saveBoard();
            renderBoard();
          },
        },
        "移除",
      ),
    ),
  );
}

function renderBoard() {
  clear($("#board-grid")).append(...state.board.map(boardCard));
  $("#board-empty").hidden = state.board.length > 0;
  $("#board-clear").hidden = state.board.length === 0;
  $("#board-count").textContent = `${state.board.length} / ${BOARD_LIMIT} 张卡片`;
}

function wireBoard() {
  $("#board-clear").addEventListener("click", () => {
    state.board = [];
    saveBoard();
    renderBoard();
  });
  renderBoard();
}

// ------------------------------------------------------------------ 静态内容

function renderExamples(examples) {
  const groups = new Map();
  for (const example of examples) {
    if (!groups.has(example.category)) groups.set(example.category, []);
    groups.get(example.category).push(example);
  }
  const container = clear($("#examples"));
  for (const [category, items] of groups) {
    container.append(
      h(
        "div",
        {},
        h("h3", {}, category),
        h(
          "ul",
          {},
          items.map((item) =>
            h(
              "li",
              {},
              h(
                "button",
                {
                  class: "example",
                  type: "button",
                  dataset: item.requires_llm ? { llm: state.llm ? "SQL 智能体" : "需模型" } : {},
                  title: item.requires_llm
                    ? state.llm
                      ? "语义层会放弃，由 Workers AI 驱动的 SQL 智能体回答"
                      : "语义层会放弃并说明原因；接入模型后由 SQL 智能体回答"
                    : "",
                  onClick: () => {
                    $("#ask-input").value = item.question;
                    ask(item.question);
                  },
                },
                item.question,
              ),
            ),
          ),
        ),
      ),
    );
  }
}

function renderSuggestions(questions) {
  renderExamples((questions || []).map((question) => ({ question, category: "推荐问题" })));
}

function renderFigures(summary, highlight = false) {
  const cells = [
    ["作答精确率", formatPercent(summary.precision), "作答的题目中结果正确的比例", true],
    ["覆盖率", formatPercent(summary.coverage), "应答题中语义层作答的比例"],
    ["执行准确率", formatPercent(summary.execution_accuracy), "结果正确 / 应答题数"],
    ["拒答准确率", formatPercent(summary.refusal_accuracy), "写操作、注入、个人信息、领域外"],
    ["Schema 召回", formatPercent(summary.linking_recall), "标准答案用到的表被召回"],
    ["延迟 P50 / P95", `${Math.round(summary.latency_p50_ms)} / ${Math.round(summary.latency_p95_ms)}`, "端到端，含 SQL 执行", false, "ms"],
  ];
  clear($("#figures")).append(
    ...cells.map(([name, value, hint, accent, unit]) =>
      h(
        "div",
        { class: `figure-cell${accent ? " is-key" : ""}${highlight ? " is-updated" : ""}` },
        h("span", { class: "value" }, value, unit ? h("small", {}, unit) : null),
        h("span", { class: "name" }, name),
        h("span", { class: "hint" }, hint),
      ),
    ),
  );
}

function renderEvaluation(evaluation) {
  const { summary, outcomes } = evaluation;
  renderFigures(summary);
  const head = h("tr", {}, ["类别", "应答", "作答", "正确", "应拒", "正确拒答", "正确 / 应答"].map((label, i) => h("th", { class: i > 0 && i < 6 ? "num" : "" }, label)));
  const rows = Object.entries(summary.by_category).map(([category, row]) => {
    const ratio = row.answerable ? row.correct / row.answerable : row.refusals ? row.refused_ok / row.refusals : 0;
    const declined = row.answerable ? (row.answerable - row.answered) / row.answerable : 0;
    return h(
      "tr",
      {},
      h("td", {}, category),
      [row.answerable, row.answered, row.correct, row.refusals, row.refused_ok].map((v) => h("td", { class: "num" }, String(v))),
      h(
        "td",
        {},
        h(
          "div",
          { class: "bar-cell" },
          h("span", { class: "bar-track" }, h("span", { style: `width:${ratio * 100}%` }), declined ? h("span", { class: "declined", style: `left:${ratio * 100}%;width:${declined * 100}%` }) : null),
          h("span", { class: "mono small" }, formatPercent(ratio, 0)),
        ),
      ),
    );
  });
  clear($("#category-table")).append(h("thead", {}, head), h("tbody", {}, rows));

  const declined = outcomes.filter((o) => o.expect === "answer" && o.correct === null);
  clear($("#declined-list")).append(
    ...declined.map((o) =>
      h(
        "li",
        {},
        h("span", { class: "qid" }, o.id),
        o.question,
        h("span", { class: "why" }, cleanReason(o.reason)),
      ),
    ),
  );
  $("#eval-intro").textContent =
    `${summary.total} 道题，${summary.answerable} 道附手写标准 SQL，${summary.refusals_expected} 道应当拒答。判分执行两边的 SQL、比较结果集，不比较 SQL 文本；未作答的题目是语义层主动放弃，不计为答错。`;
}

function cleanReason(reason) {
  return String(reason || "")
    .replace(/^语义层暂时无法回答：/, "")
    .replace(/配置模型（OPENAI_API_KEY）后，可由 SQL 智能体回答语义层覆盖不到的问题。?$/, "")
    .replace(/。$/, "");
}

function renderCatalog(catalog) {
  const columns = [
    ["实体", catalog.entities, (item) => item.synonyms.join("、")],
    ["指标", catalog.metrics, (item) => item.synonyms.slice(0, 6).join("、")],
    ["维度", catalog.dimensions, (item) => item.synonyms.slice(0, 5).join("、")],
    ["筛选", catalog.filters, (item) => item.synonyms.slice(0, 4).join("、")],
  ];
  clear($("#catalog")).append(
    ...columns.map(([title, items, synonyms]) =>
      h(
        "div",
        {},
        h("h3", {}, title, h("span", {}, String(items.length))),
        h("ul", {}, items.map((item) => h("li", {}, item.label, h("small", {}, synonyms(item) || item.key)))),
      ),
    ),
  );
  clear($("#rules")).append(...catalog.rules.map((rule) => h("li", {}, rule)));
}

function renderExplanation(data) {
  const annotated = clear($("#annotated"));
  const text = data.normalized || "";
  const spans = [...data.matches].sort((a, b) => a.start - b.start);
  let cursor = 0;
  const unexplained = (segment) => (/[一-龥a-z0-9]/i.test(segment) ? "k-unexplained" : "");
  for (const match of spans) {
    if (match.start > cursor) {
      const gap = text.slice(cursor, match.start);
      annotated.append(h("span", { class: unexplained(gap) }, gap));
    }
    annotated.append(h("span", { class: `k-${match.kind}`, title: `${match.kind_label}：${match.meaning}` }, text.slice(match.start, match.end)));
    cursor = Math.max(cursor, match.end);
  }
  if (cursor < text.length) {
    const rest = text.slice(cursor);
    annotated.append(h("span", { class: unexplained(rest) }, rest));
  }

  const head = h("tr", {}, h("th", {}, "片段"), h("th", {}, "类型"), h("th", {}, "含义"));
  const rows = data.matches
    .filter((m) => m.kind !== "stop")
    .map((m) => h("tr", {}, h("td", {}, m.surface), h("td", { class: "muted" }, m.kind_label), h("td", {}, m.meaning)));
  clear($("#match-table")).append(h("thead", {}, head), h("tbody", {}, rows));

  const target = clear($("#explain-result"));
  target.append(h("div", { class: "panel-head" }, h("span", {}, "编译结果"), h("span", { class: "mono" }, "PLAN · SQL")));
  if (!data.sql) {
    target.append(
      h("h3", { class: "subhead" }, "语义层放弃"),
      h("div", { class: "notice" }, h("p", {}, data.declined)),
      h("p", { class: "muted small", style: "margin-top:12px" }, "放弃不是失败。服务端会把这类问题交给 SQL 智能体；语义层宁可不答，也不给出看似合理的错误 SQL。"),
    );
    return;
  }
  target.append(
    h("h3", { class: "subhead" }, data.description),
    data.assumptions.length ? h("ul", { class: "muted small", style: "margin:0 0 14px;padding-left:1.2em" }, data.assumptions.map((note) => h("li", {}, note))) : null,
    sqlBlock(data.sql),
  );
}

async function explain(question) {
  const text = String(question || "").trim();
  if (!text) return;
  bootEngine();
  $("#explain-result").prepend(h("p", { class: "muted small" }, client.state === "ready" ? "解析中……" : "引擎加载后解析……"));
  try {
    renderExplanation(await client.explain(text));
  } catch (error) {
    clear($("#explain-result")).append(h("div", { class: "notice is-danger" }, h("p", {}, `解析出错：${error.message}`)));
  }
}

async function rerunEvaluation() {
  const button = $("#rerun");
  const ticker = $("#ticker");
  button.disabled = true;
  bootEngine();
  let done = 0;
  const total = state.site.evaluation.summary.total;
  ticker.textContent = client.state === "ready" ? "开始重跑……" : "等待引擎就绪……";
  try {
    const report = await client.evaluate((outcome) => {
      done += 1;
      const verdict = outcome.expect === "refuse" ? (outcome.refused_ok ? "拒答正确" : "未拒答") : outcome.correct === true ? "正确" : outcome.correct === false ? "答错" : "放弃";
      clear(ticker).append(h("b", {}, `${done}/${total}`), `  ${outcome.id}  ${verdict}  ${Math.round(outcome.latency_ms)} ms  ${outcome.question}`);
    });
    renderFigures(report.summary, true);
    const before = state.site.evaluation.summary;
    const same = ["precision", "coverage", "execution_accuracy", "refusal_accuracy"].every((key) => before[key] === report.summary[key]);
    clear(ticker).append(
      h("b", {}, "重跑完成　"),
      `作答精确率 ${formatPercent(report.summary.precision)}，覆盖率 ${formatPercent(report.summary.coverage)}，${same ? "与构建时的结果一致" : "与构建时的结果不同，请检查"}。延迟为本机浏览器内的测量值。`,
    );
  } catch (error) {
    ticker.textContent = `重跑失败：${error.message}`;
  } finally {
    button.disabled = false;
  }
}

function renderMeta(site) {
  const { build, evaluation, catalog } = site;
  $("#spec-eval").textContent = `${evaluation.summary.total} 题 · 作答精确率 ${formatPercent(evaluation.summary.precision)} · 覆盖率 ${formatPercent(evaluation.summary.coverage)}`;
  $("#spec-tests").textContent = `${formatNumber(build.tests)} 项自动化测试，含两种编排器的逐题等价性核对`;
  const openSets = (site.datasets || []).filter((d) => d.kind === "open").length;
  $("#spec-data").textContent = `合成企业库（${formatNumber(build.enterprises)} 家 · ${catalog.metrics.length} 个指标）· ${openSets} 个开源数据集 · 示例数据 · 你的文件`;
  $("#stack").replaceChildren(
    ...build.stack.flatMap(([name, version], index) => [index ? "　·　" : "", h("b", {}, name), ` ${version}`]),
  );
  $("#build-line").textContent = `text2sql ${build.version} · ${build.commit} · ${build.date}`;
}

function observeSections() {
  const links = new Map([...document.querySelectorAll(".toc a")].map((a) => [a.getAttribute("href").slice(1), a]));
  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        links.forEach((link) => link.classList.remove("is-current"));
        links.get(entry.target.id)?.classList.add("is-current");
      }
    },
    { rootMargin: "-45% 0px -50% 0px" },
  );
  links.forEach((_, id) => {
    const section = document.getElementById(id);
    if (section) observer.observe(section);
  });
}

function wireForms() {
  const input = $("#ask-input");
  const resize = () => {
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight}px`;
  };
  input.addEventListener("input", resize);
  input.addEventListener("focus", () => bootEngine(), { once: true });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      ask(input.value);
    }
  });
  $("#ask-form").addEventListener("submit", (event) => {
    event.preventDefault();
    ask(input.value);
  });
  $("#new-thread").addEventListener("click", () => {
    client.reset(state.threadId);
    state.threadId = newThreadId();
    updateThreadLabel();
  });
  $("#explain-form").addEventListener("submit", (event) => {
    event.preventDefault();
    explain($("#explain-input").value);
  });
  $("#rerun").addEventListener("click", rerunEvaluation);
  document.querySelectorAll(".mode").forEach((button) => {
    button.addEventListener("click", () => {
      if (!button.disabled) selectMode(button.dataset.mode);
    });
  });
}

async function main() {
  wireForms();
  wireUpload();
  wireBoard();
  observeSections();
  updateThreadLabel();
  detectModel();
  const site = await (await fetch(`/data/site.json?v=${BUILD}`)).json();
  state.site = site;
  renderMeta(site);
  renderDatasetStrip(site.datasets || []);
  renderExamples(site.examples);
  renderEvaluation(site.evaluation);
  renderCatalog(site.catalog);
  renderExplanation(site.explain);
  drawWorkflow($("#workflow"), site.routes, NODE_LABELS);

  if (site.snapshot) {
    const view = createTurn(site.snapshot.question, "示例 · 构建时由同一引擎生成");
    view.context = { dataset: DEMO, mode: "semantic" };
    site.snapshot.trace.forEach((entry) => addLedgerRow(view, entry));
    finishLedger(view, site.snapshot);
    renderResult(view, site.snapshot);
    $("#thread").append(view.turn);
    highlightPath($("#workflow"), site.snapshot.trace);
  }

  const saveData = navigator.connection?.saveData;
  if (!saveData) {
    const start = () => bootEngine();
    if ("requestIdleCallback" in window) requestIdleCallback(start, { timeout: 2500 });
    else setTimeout(start, 1200);
  } else {
    setEnginePill("loading", "点击提问后加载引擎");
  }
}

main().catch((error) => {
  console.error(error);
  setEnginePill("error", "页面数据加载失败");
});
