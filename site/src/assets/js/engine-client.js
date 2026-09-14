// 页面与 Web Worker 之间的请求/事件通道。Worker 内部串行执行，这里只负责配对请求与回调。

export class EngineClient extends EventTarget {
  constructor(workerUrl) {
    super();
    this.workerUrl = workerUrl;
    this.worker = null;
    this.pending = new Map();
    this.nextId = 1;
    this.state = "idle";
    this.info = null;
    this.bootPromise = null;
  }

  boot() {
    if (this.bootPromise) return this.bootPromise;
    this.worker = new Worker(this.workerUrl, { type: "module" });
    this.worker.addEventListener("message", (event) => this.#onMessage(event.data));
    this.worker.addEventListener("error", (event) => this.#fail(event.message || "Worker 加载失败"));
    this.#setState("loading");
    this.bootPromise = this.#request("init").then((info) => {
      this.info = info;
      this.#setState("ready");
      return info;
    });
    this.bootPromise.catch((error) => this.#fail(error.message));
    return this.bootPromise;
  }

  async ask(question, threadId, onEvent, mode = "semantic") {
    await this.boot();
    return this.#request("ask", { question, threadId, mode }, onEvent);
  }

  async explain(question) {
    await this.boot();
    return this.#request("explain", { question });
  }

  async evaluate(onProgress) {
    await this.boot();
    return this.#request("evaluate", {}, onProgress);
  }

  async reset(threadId) {
    if (this.state !== "ready") return;
    return this.#request("reset", { threadId });
  }

  async upload(fileList) {
    await this.boot();
    const files = await Promise.all(
      Array.from(fileList, async (file) => ({ name: file.name, buffer: await file.arrayBuffer() })),
    );
    // ArrayBuffer 以可转移对象交给 Worker，大文件不复制
    return this.#request("upload", { files }, undefined, files.map((file) => file.buffer));
  }

  async loadDataset(dataset) {
    await this.boot();
    return this.#request("dataset", { dataset });
  }

  async runSql(sql, mode) {
    await this.boot();
    return this.#request("sql", { sql, mode });
  }

  async remember(question, sql, mode) {
    await this.boot();
    return this.#request("remember", { question, sql, mode });
  }

  #request(type, payload = {}, onEvent, transfer = []) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject, onEvent });
      this.worker.postMessage({ id, type, ...payload }, transfer);
    });
  }

  #onMessage(message) {
    if (message.type === "progress") {
      this.dispatchEvent(new CustomEvent("progress", { detail: message }));
      return;
    }
    const entry = this.pending.get(message.id);
    if (!entry) return;
    if (message.type === "event") {
      entry.onEvent?.(message.event);
    } else if (message.type === "done") {
      this.pending.delete(message.id);
      entry.resolve(message.payload);
    } else if (message.type === "error") {
      this.pending.delete(message.id);
      entry.reject(new Error(message.message));
    }
  }

  #fail(message) {
    this.#setState("error", message);
    for (const entry of this.pending.values()) entry.reject(new Error(message));
    this.pending.clear();
  }

  #setState(state, message) {
    this.state = state;
    this.dispatchEvent(new CustomEvent("state", { detail: { state, message } }));
  }
}
