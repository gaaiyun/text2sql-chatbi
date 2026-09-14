// Web Worker：加载 Pyodide、DuckDB 与 text2sql 包，串行处理页面请求。
// 引擎在 Worker 里同步运行，事件经 postMessage 实时送回主线程，页面不会卡顿。

let pyodide = null;
let manifest = null;
let engine = null;
let booting = null;
let queue = Promise.resolve();
const loadedOptional = new Set();

const post = (message) => self.postMessage(message);
const progress = (stage, label, ratio, extra = {}) => post({ type: "progress", stage, label, ratio, ...extra });
const absolute = (path) => new URL(path, self.location.origin).href;

async function fetchBytes(url, expected, onBytes) {
  const response = await fetch(url);
  if (!response.ok || !response.body) throw new Error(`下载失败：${url}（${response.status}）`);
  const reader = response.body.getReader();
  const chunks = [];
  let received = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    received += value.byteLength;
    onBytes?.(received, expected);
  }
  const bytes = new Uint8Array(received);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

async function gunzip(bytes) {
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

async function boot() {
  const started = performance.now();
  progress("manifest", "读取清单", 0.02);
  manifest = await (await fetch("/data/manifest.json", { cache: "no-cache" })).json();

  progress("runtime", "下载 Python 运行时", 0.08);
  const { loadPyodide } = await import(manifest.pyodide.module);
  pyodide = await loadPyodide({
    indexURL: absolute(manifest.pyodide.indexURL),
    stdout: () => {},
    stderr: (line) => console.warn("[python]", line),
  });

  progress("packages", "加载 DuckDB 与 PyYAML", 0.38);
  await pyodide.loadPackage(manifest.pyodide.packages, { messageCallback: () => {} });

  progress("wheels", "安装 sqlglot 与 text2sql", 0.62);
  await pyodide.loadPackage(manifest.wheels.map((wheel) => absolute(wheel.url)), { messageCallback: () => {} });

  const db = manifest.database;
  const compressed = await fetchBytes(absolute(db.url), db.bytes, (received, total) => {
    progress("database", "下载合成演示库", 0.7 + 0.2 * (received / total), { received, total });
  });
  progress("database", "解压演示库", 0.9);
  const raw = db.gzip ? await gunzip(compressed) : compressed;
  pyodide.FS.mkdirTree("/data");
  pyodide.FS.writeFile("/data/demo.duckdb", raw);

  progress("index", "扫描值索引", 0.95);
  // 线上由 Worker 提供 /api/llm（Workers AI）；本地静态预览没有这个接口，引擎只启用语义层
  let aiAvailable = false;
  try {
    const health = await (await fetch("/api/health", { cache: "no-store" })).json();
    aiAvailable = Boolean(health.ai);
  } catch {
    aiAvailable = false;
  }
  pyodide.globals.set("AI_AVAILABLE", aiAvailable);
  pyodide.runPython(
    [
      "from text2sql.web.engine import WebEngine",
      "from text2sql.web.transport import xhr_transport",
      "engine = WebEngine('/data/demo.duckdb',",
      "    llm_transport=xhr_transport('/api/llm') if AI_AVAILABLE else None,",
      "    llm_model='Workers AI', upload_dir='/uploads')",
    ].join("\n"),
  );
  engine = pyodide.globals.get("engine");
  const info = JSON.parse(engine.info());
  info.boot_total_ms = Math.round(performance.now() - started);
  progress("ready", "就绪", 1);
  return info;
}

function extensionOf(name) {
  const match = /\.[A-Za-z0-9]+$/.exec(name || "");
  return match ? match[0].toLowerCase() : "";
}

function stageFiles(files) {
  pyodide.FS.mkdirTree("/uploads/in");
  for (const name of pyodide.FS.readdir("/uploads/in")) {
    if (name !== "." && name !== "..") pyodide.FS.unlink(`/uploads/in/${name}`);
  }
  // 文件系统里只用序号命名，原始文件名交给 Python 生成表名
  return files.map((file, index) => {
    const path = `/uploads/in/${index}${extensionOf(file.name)}`;
    pyodide.FS.writeFile(path, new Uint8Array(file.buffer));
    return [file.name, path];
  });
}

async function importFiles(files) {
  if (files.some((file) => /\.xlsx?$/i.test(file.name)) && !loadedOptional.has("excel")) {
    await pyodide.loadPackage(manifest.pyodide.optional.excel, { messageCallback: () => {} });
    loadedOptional.add("excel");
  }
  return JSON.parse(engine.load_upload(JSON.stringify(stageFiles(files))));
}

async function loadDataset(id) {
  const listed = manifest.datasets?.[id];
  if (!listed) throw new Error(`没有数据集 ${id}`);
  const total = listed.reduce((sum, file) => sum + file.bytes, 0);
  let done = 0;
  const files = [];
  for (const file of listed) {
    const bytes = await fetchBytes(absolute(file.url), file.bytes, (received) => {
      progress("dataset", "下载数据集", 0.3 + 0.6 * ((done + received) / total), { received: done + received, total });
    });
    done += file.bytes;
    files.push({ name: file.name, buffer: bytes.buffer });
  }
  progress("dataset", "导入 DuckDB 并生成语义层", 0.95);
  const summary = JSON.parse(engine.load_dataset(id, JSON.stringify(stageFiles(files))));
  progress("ready", "就绪", 1);
  return summary;
}

async function handle(message) {
  const { id, type } = message;
  try {
    if (type === "init") {
      booting ??= boot();
      post({ id, type: "done", payload: await booting });
      return;
    }
    await booting;
    const emit = (json) => post({ id, type: "event", event: JSON.parse(json) });
    let payload;
    if (type === "ask") payload = JSON.parse(engine.ask(message.question, message.threadId, emit, message.mode || "semantic"));
    else if (type === "explain") payload = JSON.parse(engine.explain(message.question));
    else if (type === "evaluate") payload = JSON.parse(engine.evaluate(emit));
    else if (type === "upload") payload = await importFiles(message.files);
    else if (type === "dataset") payload = await loadDataset(message.dataset);
    else if (type === "sql") payload = JSON.parse(engine.run_sql(message.sql, message.mode || "semantic"));
    else if (type === "remember") payload = JSON.parse(engine.remember(message.question, message.sql, message.mode || "semantic"));
    else if (type === "reset") payload = engine.reset(message.threadId) ?? null;
    else throw new Error(`未知请求：${type}`);
    post({ id, type: "done", payload });
  } catch (error) {
    const text = String(error?.message || error);
    post({ id, type: "error", message: text.split("\n").filter(Boolean).slice(-1)[0] || text });
  }
}

self.addEventListener("message", (event) => {
  queue = queue.then(() => handle(event.data));
});
