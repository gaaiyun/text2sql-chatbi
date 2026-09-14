// SQL 高亮：按词法切分后逐段生成 span，行号由 CSS 计数器绘制。

import { h } from "./dom.js";

const KEYWORDS = new Set(
  (
    "SELECT FROM WHERE GROUP BY ORDER HAVING LIMIT OFFSET AS AND OR NOT IN IS NULL LIKE BETWEEN " +
    "EXISTS CASE WHEN THEN ELSE END JOIN LEFT RIGHT INNER OUTER FULL CROSS ON USING DISTINCT " +
    "UNION ALL WITH DESC ASC OVER PARTITION ROWS RANGE INTERVAL ESCAPE TRUE FALSE"
  ).split(" "),
);
const FUNCTIONS = new Set(
  (
    "COUNT SUM AVG MIN MAX ROUND SUBSTR SUBSTRING YEAR MONTH DAY COALESCE CAST CONCAT IFNULL LENGTH " +
    "LOWER UPPER TRIM ABS DATE_FORMAT STRFTIME EXTRACT LAG LEAD ROW_NUMBER RANK DENSE_RANK NTILE " +
    "DATEDIFF TIMESTAMPDIFF NOW CURRENT_DATE TRY_CAST REGEXP_MATCHES"
  ).split(" "),
);

const TOKEN =
  /(--[^\n]*)|('(?:[^'\\]|\\.|'')*')|(`[^`]*`|"(?:[^"]|"")*")|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)|(\s+)|([^\s])/g;

function tokenSpan(match) {
  const [text, comment, string, ident, number, word] = match;
  if (comment) return h("span", { class: "cm" }, text);
  if (string) return h("span", { class: "str" }, text);
  if (ident) return h("span", { class: "ident" }, text);
  if (number) return h("span", { class: "numlit" }, text);
  if (word) {
    const upper = word.toUpperCase();
    if (KEYWORDS.has(upper)) return h("span", { class: "kw" }, text);
    if (FUNCTIONS.has(upper)) return h("span", { class: "fn" }, text);
  }
  return document.createTextNode(text);
}

export function sqlBlock(sql, { copy = true } = {}) {
  const text = String(sql || "").replace(/\s+$/, "");
  const list = h("ol");
  for (const line of text.split("\n")) {
    const item = h("li");
    for (const match of line.matchAll(TOKEN)) item.append(tokenSpan(match));
    if (!line) item.append(" ");
    list.append(item);
  }
  const block = h("div", { class: "sql-block" }, list);
  if (copy) {
    const button = h("button", { class: "sql-copy", type: "button" }, "复制");
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(text);
        button.textContent = "已复制";
      } catch {
        button.textContent = "无法复制";
      }
      setTimeout(() => (button.textContent = "复制"), 1600);
    });
    block.append(button);
  }
  return block;
}
