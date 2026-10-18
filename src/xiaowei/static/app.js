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

  // 用户可见编号按本次引用顺序生成，不展示内部证据编号。
  const resultLabel = (delivery, id) => {
    const index = delivery.evidence_ids.indexOf(id);
    return index < 0 ? "历史结果" : `结果 ${index + 1}`;
  };
  const factTable = (fact, label) => {
    const box = el("div", "fact");
    const brief = fact.view === "brief";
    // 系统区域的标识只由代码在这里添加；模型文字中的同样字样只是普通段落。
    box.append(el("p", "meta fact-source", `${label} · 系统根据证据生成`));
    const details = el("details", "evidence");
    details.append(el("summary", "", brief ? "查看获准原文与查询信息" : "查看依据与查询信息"));
    box.append(el("p", "meta", `采集于 ${fact.captured_at}`));
    const meta = [`来源 ${fact.tool_id}`, `目标 ${fact.target_id}`];
    (brief ? box : details).append(el("p", "meta", `${label} · ${meta.join(" · ")}`));
    if (brief && fact.summary) box.append(el("p", "content fact-summary", fact.summary));
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
      (brief ? details : box).append(wrap);
      if (!fact.rows.length) (brief ? details : box).append(el("p", "content", "未返回数据行"));
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
      const delivery = data.delivery;
      const facts = delivery.facts || [];
      const blocks = delivery.blocks || [];
      const notices = [delivery.report_notice, delivery.limit_notice, delivery.monitoring_notice];
      if (delivery.session_notice) {
        item.append(el("p", "note session-notice", delivery.session_notice));
      }
      if (!facts.length && !blocks.length) {
        // 固定回执与只有系统说明的结果：全部是代码文字。
        item.append(el("p", "content", delivery.content));
        return;
      }
      for (const text of notices) if (text) item.append(el("p", "meta system-notice", text));
      const system = facts.length > 0 || notices.some(Boolean) || Boolean(delivery.session_notice);
      const byId = new Map(facts.map((fact) => [fact.evidence_id, fact]));
      const section = (name, title) => {
        const chosen = facts.filter((fact) => fact.section === name);
        if (!chosen.length) return;
        const box = el("section", `facts ${name}`);
        box.append(el("p", "meta", title));
        for (const fact of chosen) box.append(factTable(fact, resultLabel(delivery, fact.evidence_id)));
        item.append(box);
      };
      section("action", "动作（系统根据记录生成）");
      let labelled = false;
      for (const block of blocks) {
        if (block.kind === "fact") {
          labelled = false;
          const fact = byId.get(block.evidence_id);
          if (fact) item.append(factTable(fact, resultLabel(delivery, fact.evidence_id)));
          continue;
        }
        if (system && !labelled) {
          item.append(el("p", "meta model-label", "模型说明（未经系统核实）"));
          labelled = true;
        }
        item.append(el("p", "content reply-text", block.text));
      }
      const placed = new Set(blocks.filter((b) => b.kind === "fact").map((b) => b.evidence_id));
      for (const fact of facts) {
        // 正文事实都应由块放置；未被放置的仍完整展示，不静默丢弃。
        if (fact.section === "body" && !placed.has(fact.evidence_id)) {
          item.append(factTable(fact, resultLabel(delivery, fact.evidence_id)));
        }
      }
      section("remaining", "其余结果（系统补全本轮获准结果）");
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
