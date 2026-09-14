// 轻量 DOM 构造与格式化。所有文本都走 textContent，不拼接 HTML。

export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  applyProps(el, props);
  append(el, children);
  return el;
}

const SVG_NS = "http://www.w3.org/2000/svg";

export function s(tag, props = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  applyProps(el, props);
  append(el, children);
  return el;
}

function applyProps(el, props) {
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.setAttribute("class", value);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "text") el.textContent = value;
    else el.setAttribute(key, value === true ? "" : String(value));
  }
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export const $ = (selector, root = document) => root.querySelector(selector);

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

const integer = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 });
const decimal = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });

export function isNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

export function formatNumber(value) {
  if (!isNumber(value)) return value === null || value === undefined ? "—" : String(value);
  return Number.isInteger(value) ? integer.format(value) : decimal.format(value);
}

export function formatPercent(ratio, digits = 1) {
  return isNumber(ratio) ? `${(ratio * 100).toFixed(digits)}%` : "—";
}

export function formatMs(ms) {
  if (!isNumber(ms)) return "—";
  return ms >= 100 ? `${Math.round(ms)}` : ms.toFixed(1);
}

export function truncate(text, max) {
  const value = String(text ?? "");
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

export function csvFor(columns, rows) {
  const quote = (value) => {
    if (value === null || value === undefined) return "";
    const text = String(value);
    return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
  };
  const lines = [columns.map(quote).join(",")];
  for (const row of rows) lines.push(columns.map((c) => quote(row[c])).join(","));
  return `﻿${lines.join("\n")}`;
}

export function download(filename, content, type) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = h("a", { href: url, download: filename });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
