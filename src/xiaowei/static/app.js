"use strict";
// 只用 textContent 与 DOM API 渲染；服务端字符串从不作为 HTML 解析。
(() => {
  const POLL_MS = 1000;
  const POLL_LIMIT = 600;
  const STORE_KEY = "xiaowei.turns";
  const turns = document.getElementById("turns");
  const form = document.getElementById("composer");
  const notice = document.getElementById("notice");

  // 双向文字控制符会让显示顺序与实际内容不一致：显示为可见的转义，而不是让它生效。
  const BIDI = /[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g;
  const visible = (value) =>
    String(value).replace(BIDI, (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`);

  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = visible(text);
    return node;
  };

  const remembered = () => {
    try {
      const value = JSON.parse(sessionStorage.getItem(STORE_KEY) || "[]");
      return Array.isArray(value) ? value : [];
    } catch {
      return [];
    }
  };
  const remember = (list) => {
    try {
      sessionStorage.setItem(STORE_KEY, JSON.stringify(list.slice(-50)));
    } catch {
      // 浏览器禁止存储时只影响刷新后的列表恢复。
    }
  };

  const factTable = (fact) => {
    const box = el("div", "fact");
    const meta = [`来源 ${fact.tool_id}`, `目标 ${fact.target_id}`, `采集于 ${fact.captured_at}`];
    if (fact.truncated) meta.push("结果已截断，只显示获准的前若干行");
    box.append(el("p", "meta", meta.join(" · ")));
    if (fact.note) box.append(el("p", "note", `说明：${fact.note}`));
    for (const [key, value] of Object.entries(fact.metadata || {})) {
      box.append(el("p", "meta", `${key}: ${value}`));
    }
    if (fact.columns.length) {
      const table = el("table");
      const head = table.createTHead().insertRow();
      for (const column of fact.columns) head.append(el("th", "", column));
      const body = table.createTBody();
      for (const row of fact.rows) {
        const tr = body.insertRow();
        for (const column of fact.columns) {
          const value = row[column];
          tr.append(el("td", "", value === null || value === undefined ? "" : value));
        }
      }
      const wrap = el("div", "table-wrap");
      wrap.append(table);
      box.append(wrap);
    }
    return box;
  };

  const render = (item, data) => {
    item.replaceChildren(el("p", "question", item.dataset.question || ""));
    item.className = `turn ${data.state || ""}`;
    const label = { accepted: "已接收", running: "处理中", completed: "已完成", failed: "失败",
      interrupted: "已中断" }[data.state] || "错误";
    item.append(el("p", "meta", `${label} · 编号 ${item.dataset.requestId}`));
    if (data.error) item.append(el("p", "content", data.error));
    if (data.delivery) {
      item.append(el("p", "content", data.delivery.content));
      for (const fact of data.delivery.facts) item.append(factTable(fact));
    }
  };

  const fetchJson = async (url, options) => {
    const response = await fetch(url, { credentials: "same-origin", ...options });
    let data;
    try {
      data = await response.json();
    } catch {
      data = { error: `服务返回 ${response.status}` };
    }
    return { status: response.status, data };
  };

  const poll = async (item) => {
    for (let i = 0; i < POLL_LIMIT; i += 1) {
      const { status, data } = await fetchJson(`/api/turns/${encodeURIComponent(item.dataset.requestId)}`);
      render(item, data);
      if (status !== 202) return;
      await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    }
  };

  // http://内网IP 不是安全上下文，浏览器不提供 crypto.randomUUID；getRandomValues 在任何页面可用。
  const newRequestId = () =>
    Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");

  const addTurn = (requestId, question) => {
    const item = el("li", "turn");
    item.dataset.requestId = requestId;
    item.dataset.question = question;
    item.append(el("p", "question", question), el("p", "meta", `处理中 · 编号 ${requestId}`));
    turns.append(item);
    return item;
  };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = document.getElementById("message").value;
    const mode = document.getElementById("mode").value;
    if (!message.trim()) return;
    const requestId = newRequestId();
    const item = addTurn(requestId, message);
    remember([...remembered(), { requestId, question: message }]);
    form.reset();
    const { status, data } = await fetchJson("/api/turns", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: requestId, mode, message }),
    });
    render(item, data);
    if (status === 202) await poll(item);
  });

  document.getElementById("new-session").addEventListener("click", async () => {
    const { status, data } = await fetchJson("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (status === 200) {
      remember([]);
      turns.replaceChildren();
      notice.textContent = "已新建会话；之前的问题与查询许可不会带入。";
    } else {
      notice.textContent = data.error || `新建失败（${status}）`;
    }
  });

  for (const { requestId, question } of remembered()) {
    if (typeof requestId === "string" && typeof question === "string") {
      poll(addTurn(requestId, question));
    }
  }
})();
