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
  const cellText = (value) => value === null ? "NULL（空值）" :
    value === undefined ? "（未提供）" : value === "" ? "（空字符串）" : value;

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
    const details = el("details", "evidence");
    details.append(el("summary", "", "查看依据与查询信息"));
    box.append(el("p", "meta", `采集于 ${fact.captured_at}`));
    const meta = [`来源 ${fact.tool_id}`, `目标 ${fact.target_id}`];
    details.append(el("p", "meta", `[${fact.evidence_id}] ${meta.join(" · ")}`));
    if (fact.truncated) box.append(el("p", "note", "结果已截断，当前显示的不是全部结果"));
    if (fact.note) box.append(el("p", "note", `说明：${fact.note}`));
    for (const [key, value] of Object.entries(fact.metadata || {})) {
      // 查询/续取信息折叠；其余获准字段完整显示，嵌套值由服务端编码为 JSON 文本。
      const technical = ["sql", "next_cursor", "row_count", "elapsed_ms"].includes(key);
      (technical ? details : box).append(el("p", "content", `${key}: ${cellText(value)}`));
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
          tr.append(el("td", value === null ? "null-value" : "", cellText(value)));
        }
      }
      const wrap = el("div", "table-wrap");
      wrap.append(table);
      box.append(wrap);
      if (!fact.rows.length) box.append(el("p", "content", "未返回数据行"));
    }
    if (fact.result_json !== null && fact.result_json !== undefined) {
      details.append(el("p", "meta", "本次结果原文（仅含获准展示字段；分页或截断范围见上方提示）"));
      details.append(el("pre", "content original", fact.result_json));
    }
    box.append(details);
    return box;
  };

  const render = (item, data) => {
    item.replaceChildren(el("p", "question", item.dataset.question || ""));
    item.className = `turn ${data.state || ""}`;
    const label = { accepted: "已接收", running: "处理中", completed: "已回复", failed: "失败",
      interrupted: "已中断" }[data.state] || "错误";
    item.append(el("p", "meta", `${label} · 编号 ${item.dataset.requestId}`));
    if (data.error) item.append(el("p", "content", data.error));
    if (data.delivery) {
      if (data.delivery.facts.length) {
        for (const fact of data.delivery.facts) item.append(factTable(fact));
        if (data.delivery.analysis.length) {
          const analysis = el("section", "analysis");
          analysis.append(el("p", "meta", "分析与建议（模型推断）"));
          for (const inference of data.delivery.analysis) {
            analysis.append(el("p", "content", inference.text));
            analysis.append(el("p", "meta", `依据：${inference.evidence_ids.join(", ")}`));
          }
          item.append(analysis);
        }
      } else if (data.delivery.web_text !== null && data.delivery.web_text !== undefined) {
        item.append(el("p", "meta", data.delivery.content.split("\n", 1)[0]));
        item.append(el("p", "content reply-text", data.delivery.web_text));
      } else {
        item.append(el("p", "content", data.delivery.content));
      }
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

  const addTurn = (requestId, question, mode = "query") => {
    const item = el("li", "turn");
    item.dataset.requestId = requestId;
    item.dataset.question = question;
    item.dataset.mode = mode;
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
    const item = addTurn(requestId, message, mode);
    remember([...remembered(), { requestId, question: message, mode }]);
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

  for (const { requestId, question, mode } of remembered()) {
    if (typeof requestId === "string" && typeof question === "string") {
      poll(addTurn(requestId, question, mode === "diagnose" ? mode : "query"));
    }
  }
})();
