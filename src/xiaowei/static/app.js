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
  const technicalKeys = new Set(["sql", "row_count", "elapsed_ms", "next_cursor", "query",
    "expression", "receipt_id", "requested_limit", "applied_limit"]);
  const factTable = (fact, label, modern = false, technical = null) => {
    const box = el("div", "fact");
    const action = fact.section === "action";
    const brief = !action && fact.view === "brief";
    const folded = !action && (modern ? brief : (brief || fact.section === "remaining" || !fact.columns.length ||
      fact.rows.length > 10 || new TextEncoder().encode(JSON.stringify(fact.rows)).length > 2048));
    // 系统区域的标识只由代码在这里添加；模型文字中的同样字样只是普通段落。
    if (modern) {
      const source = fact.target_id === "scheduled" ? "任务记录" :
        fact.target_id === "notifications" ? "通知状态" : `数据范围：${fact.target_id}`;
      box.append(el("p", "meta fact-source", `${source} · 采集于 ${fact.captured_at}`));
      if (!action && technical) {
        technical.append(el("p", "meta", `${fact.tool_id} · ${fact.evidence_id}`));
      }
    } else if (!folded) box.append(el("p", "meta fact-source", `数据 · ${fact.target_id}`));
    const details = el("details", "evidence");
    details.append(el("summary", "", `查看依据 · ${fact.target_id}`));
    if (!modern) (action ? box : details).append(el("p", "meta", `采集于 ${fact.captured_at}`));
    const meta = [`来源 ${fact.tool_id}`, `目标 ${fact.target_id}`];
    if (!modern) (action ? box : details).append(el("p", "meta", `${label} · ${meta.join(" · ")}`));
    if (modern && !action && fact.presentation) {
      const shown = fact.presentation;
      const table = (columns, rows, target) => {
        if (!target || !columns.length) return;
        const node = el("table");
        const head = node.createTHead().insertRow();
        for (const column of columns) head.append(el("th", "", column));
        const body = node.createTBody();
        for (const row of rows) {
          const tr = body.insertRow();
          for (const column of columns) tr.append(el("td", "", row[column]));
        }
        const wrap = el("div", "table-wrap");
        wrap.append(node);
        target.append(wrap);
        if (!rows.length) target.append(el("p", "content", "未返回数据行"));
      };
      if (shown.coverage) {
        box.append(el("p", "content coverage", shown.coverage));
        table(Object.keys(shown.checks[0] || {}), shown.checks, box);
        for (const text of shown.notices) box.append(el("p", "note", text));
      } else {
        if (fact.note) box.append(el("p", "note", `说明：${fact.note}`));
        if (fact.note_summary) box.append(el("p", "note", fact.note_summary));
        if (brief && fact.summary) box.append(el("p", "content fact-summary", fact.summary));
      }
      const target = folded ? technical : box;
      for (const [key, value] of Object.entries(shown.fields)) {
        if (target) target.append(el("p", "content", `${key}: ${value}`));
      }
      table(shown.columns, shown.rows, target);
      if (technical && fact.result_json != null) {
        technical.append(el("p", "meta", "本次结果原文（仅含获准展示字段）"));
        technical.append(el("pre", "content original", fact.result_json));
      }
      return box;
    }
    if (brief && fact.summary) box.append(el("p", "content fact-summary", fact.summary));
    if (fact.note) (modern || action ? box : details).append(el("p", "note", `说明：${fact.note}`));
    if (fact.note_summary) box.append(el("p", "note", fact.note_summary));
    for (const [key, value] of Object.entries(fact.metadata || {})) {
      if (modern && key === "warnings" && [null, "", "[]", "null"].includes(value)) continue;
      // 普通 metadata 完整留在依据中；动作材料保持原专用展示。
      const target = modern ? (action || (!folded && !technicalKeys.has(key)) ? box : technical) :
        (action ? box : details);
      if (target) target.append(el("p", "content", `${key}: ${cellText(value)}`));
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
      (modern && folded ? technical : folded ? details : box).append(wrap);
      if (!fact.rows.length) (modern && folded ? technical : folded ? details : box).append(el("p", "content", "未返回数据行"));
    }
    const original = modern ? (action ? null : technical) : details;
    if (original && fact.result_json !== null && fact.result_json !== undefined) {
      original.append(el("p", "meta", "本次结果原文（仅含获准展示字段；分页或截断范围见上方提示）"));
      original.append(el("pre", "content original", fact.result_json));
    }
    if (modern) {
      if (fact.truncated) box.append(el("p", "note", "工具结果已截断，不能据此声称完整。"));
      const m = fact.metadata || {};
      if (m.next_cursor != null || m.has_more || m.hasMore) {
        box.append(el("p", "note", "还有后续页，本页不是全部结果。"));
      }
    } else box.append(details);
    return box;
  };

  const render = (item, data) => {
    item.replaceChildren(el("p", "question", item.dataset.question || ""));
    item.className = `turn ${data.state || ""}`;
    const label = { accepted: "已接收", running: "处理中", completed: "已回复", failed: "失败",
      interrupted: "已中断" }[data.state] || "错误";
    const legacy = [2, 3].includes(data.delivery?.answer_version);
    item.append(el("p", "meta", legacy ? `${label} · 编号 ${item.dataset.requestId}` : label));
    if (data.error) item.append(el("p", "content", data.error));
    if (data.delivery) {
      const delivery = data.delivery;
      const modern = delivery.answer_version === 4;
      const technical = el("details", "evidence technical-details");
      technical.append(el("summary", "", "技术详情"));
      const facts = delivery.facts || [];
      const blocks = delivery.blocks || [];
      const notices = [delivery.report_notice, delivery.limit_notice, delivery.monitoring_notice, delivery.notification_notice];
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
        if (name === "action") box.append(el("p", "meta", title));
        for (const fact of chosen) box.append(factTable(fact, resultLabel(delivery, fact.evidence_id), modern, technical));
        item.append(box);
      };
      section("action", "动作（系统根据记录生成）");
      let modelLabelled = false;
      for (const block of blocks) {
        if (block.kind === "fact") {
          const fact = byId.get(block.evidence_id);
          if (fact) item.append(factTable(fact, resultLabel(delivery, fact.evidence_id), modern, technical));
          continue;
        }
        if (system && !modelLabelled) {
          item.append(el("p", "meta model-label", "AI 说明"));
          modelLabelled = true;
        }
        item.append(el("p", "content reply-text", block.text));
      }
      const placed = new Set(blocks.filter((b) => b.kind === "fact").map((b) => b.evidence_id));
      for (const fact of facts) {
        // 正文事实都应由块放置；未被放置的仍完整展示，不静默丢弃。
        if (fact.section === "body" && !placed.has(fact.evidence_id)) {
          item.append(factTable(fact, resultLabel(delivery, fact.evidence_id), modern, technical));
        }
      }
      section("remaining", "其余结果（系统补全本轮获准结果）");
      if (modern && technical.childElementCount > 1) item.append(technical);
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
    item.append(el("p", "question", question), el("p", "meta", "处理中"));
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
