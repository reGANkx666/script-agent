/* global EventSource */
const $ = (selector) => document.querySelector(selector);

const TERMINAL = new Set([
  "SUCCESS",
  "NEEDS_INPUT",
  "NO_MATERIAL",
  "TOOL_ERROR",
  "UNSATISFIABLE_DURATION",
  "UNSATISFIABLE_RULES",
  "MAX_STEPS",
  "MODEL_ERROR",
  "CANCELLED",
  "INTERRUPTED",
]);

const STATUS_CLASS = {
  RUNNING: "is-running",
  QUEUED: "is-running",
  WAITING_INPUT: "is-waiting",
  SUCCESS: "is-success",
  NO_MATERIAL: "is-error",
  TOOL_ERROR: "is-error",
  UNSATISFIABLE_DURATION: "is-error",
  UNSATISFIABLE_RULES: "is-error",
  MAX_STEPS: "is-error",
  MODEL_ERROR: "is-error",
  INTERRUPTED: "is-error",
  CANCELLED: "is-waiting",
};

const state = {
  runId: null,
  status: null,
  eventCount: 0,
  es: null,
  poll: null,
  finished: false,
  graphVisited: new Set(),
};

const FLOW_NODES = new Set([
  "validate_input",
  "agent",
  "search_materials",
  "draft_script",
  "estimate_duration",
  "check_rules",
  "revise_script",
  "ask_user",
  "finalize",
  "fail",
]);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function shortId(id) {
  return id ? id.slice(0, 8).toUpperCase() : "NO RUN";
}

function setSystemState(kind, label) {
  const node = $("#systemState");
  node.classList.remove("is-ready", "is-error");
  if (kind) node.classList.add(kind);
  node.querySelector("span").textContent = label;
}

function setStatus(status) {
  state.status = status;
  const value = $("#statusValue");
  const stage = $(".stage");
  stage.classList.remove("is-running", "is-success", "is-waiting", "is-error");
  const klass = STATUS_CLASS[status];
  if (klass) stage.classList.add(klass);
  value.textContent = status || "IDLE";
}

function setStep(step) {
  if (step !== undefined && step !== null) $("#stepValue").textContent = String(step);
}

function setToolCount(count) {
  if (count !== undefined && count !== null) $("#toolValue").textContent = String(count);
}

function setNode(node) {
  $("#nodeValue").textContent = node || "—";
  let phase = "";
  if (node === "validate_input") phase = "validate";
  else if (node === "agent") phase = "agent";
  else if (["search_materials", "draft_script", "estimate_duration", "check_rules", "revise_script", "ask_user"].includes(node)) phase = "tools";
  else if (["finalize", "fail"].includes(node)) phase = "finalize";
  const order = ["validate", "agent", "tools", "finalize"];
  const active = order.indexOf(phase);
  document.querySelectorAll(".step").forEach((item, index) => {
    item.classList.toggle("is-active", index === active);
    item.classList.toggle("is-done", active >= 0 && index < active);
  });

  if (node && FLOW_NODES.has(node)) state.graphVisited.add(node);
  document.querySelectorAll("[data-flow-node]").forEach((item) => {
    const key = item.dataset.flowNode;
    item.classList.toggle("is-active", key === node);
    item.classList.toggle("is-done", state.graphVisited.has(key) && key !== node);
    item.classList.toggle("is-error", key === node && node === "fail");
  });
  $("#graphStatus").textContent = node ? `ACTIVE / ${node}` : "WAITING";
}

async function getJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      detail = body.detail?.message || body.detail || detail;
    } catch (_) { /* keep status */ }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return response.json();
}

async function checkReady() {
  try {
    const ready = await getJson("/readyz");
    if (ready.ready) {
      setSystemState("is-ready", "SYSTEM READY");
      $("#pulseLive").textContent = "LIVE";
      $("#pulseNote").textContent = `PGVECTOR ${ready.details?.pgvector || "—"} / ${ready.details?.checkpointer || "—"}`;
    } else {
      setSystemState("is-error", "NOT READY");
      $("#pulseLive").textContent = "PAUSED";
      $("#pulseNote").textContent = (ready.errors || ["依赖未就绪"]).join(" · ");
    }
  } catch (error) {
    setSystemState("is-error", "API OFFLINE");
    $("#pulseLive").textContent = "OFFLINE";
    $("#pulseNote").textContent = "启动 API 后自动恢复";
  }
}

function clearRunView() {
  state.eventCount = 0;
  state.finished = false;
  $("#eventLog").replaceChildren();
  $("#eventCount").textContent = "0 EVENTS";
  $("#storyboard").replaceChildren();
  $("#sourceList").replaceChildren(el("p", "muted", "运行后，这里会列出每条事实的来源。"));
  $("#checkList").replaceChildren(el("p", "muted", "合规检查结果会在这里出现。"));
  $("#traceSummary").replaceChildren(el("p", "muted", "每次动作、工具结果和决策依据都会被记录。"));
  $("#traceLink").hidden = true;
  $("#failureCard").hidden = true;
  $("#eventsWrap").hidden = true;
  $("#storyboardWrap").hidden = true;
  $("#stageEmpty").hidden = false;
  $("#runId").textContent = "NO RUN";
  $("#sourceCount").textContent = "0";
  $("#checkCount").textContent = "—";
  $("#shotCount").textContent = "0 SHOTS";
  $("#stepValue").textContent = "—";
  $("#toolValue").textContent = "—";
  $("#nodeValue").textContent = "—";
  $("#graphStatus").textContent = "WAITING";
  state.graphVisited.clear();
  document.querySelectorAll(".step").forEach((item) => item.classList.remove("is-active", "is-done"));
  document.querySelectorAll("[data-flow-node]").forEach((item) => item.classList.remove("is-active", "is-done", "is-error"));
  setStatus("IDLE");
}

function openStream() {
  if (!state.runId) return;
  if (state.es) state.es.close();
  const source = new EventSource(`/v1/runs/${state.runId}/events`);
  state.es = source;
  const names = ["node.started", "node.completed", "action.selected", "tool.called", "tool.result", "run.waiting_input", "run.completed", "run.failed"];
  names.forEach((name) => {
    source.addEventListener(name, (event) => {
      let payload = {};
      try { payload = JSON.parse(event.data); } catch (_) { return; }
      handleEvent(name, payload);
    });
  });
  source.onerror = () => {
    if (state.finished) source.close();
  };
}

function eventText(type, data) {
  if (type === "node.started") return `进入节点 ${data.node || "—"}`;
  if (type === "node.completed") return `节点 ${data.node || "—"} 完成`;
  if (type === "action.selected") {
    const basis = String(data.basis || "").replace(/\s+/g, " ").slice(0, 120);
    return `模型选择 ${data.action || "—"}${basis ? ` · ${basis}` : ""}`;
  }
  if (type === "tool.called") return `调用 ${data.node || "工具"}`;
  if (type === "tool.result") {
    const sources = Array.isArray(data.source_ids) ? ` · ${data.source_ids.join(", ")}` : "";
    return `${data.node || "工具"} → ${data.status || "OK"}${sources}`;
  }
  if (type === "run.waiting_input") {
    const missing = Array.isArray(data.missing) && data.missing.length ? ` · 缺 ${data.missing.join(", ")}` : "";
    return `需要补充信息${missing}`;
  }
  if (type === "run.completed") return "运行完成";
  if (type === "run.failed") return `运行终止 · ${data.status || ""}`;
  return type;
}

function appendEvent(type, data) {
  state.eventCount += 1;
  $("#eventsWrap").hidden = false;
  $("#stageEmpty").hidden = true;
  $("#eventCount").textContent = `${state.eventCount} EVENTS`;

  const row = el("div", "event-row");
  row.append(
    el("span", "event-seq", String(data.seq || state.eventCount).padStart(2, "0")),
    el("span", "event-type", type.replace("node.", "").replace("tool.", "").toUpperCase()),
  );
  const text = el("span", "event-text", eventText(type, data));
  row.append(text);
  $("#eventLog").append(row);
  $("#eventLog").scrollTop = $("#eventLog").scrollHeight;

  if (data.step !== undefined) setStep(data.step);
  if (data.tool_calls !== undefined) setToolCount(data.tool_calls);
  if (data.node) setNode(data.node);
  if (type === "run.completed" || type === "run.failed" || type === "run.waiting_input") {
    const nextStatus = data.status || (type === "run.completed" ? "SUCCESS" : type === "run.waiting_input" ? "WAITING_INPUT" : null);
    if (nextStatus) setStatus(nextStatus);
  }
}

function handleEvent(type, data) {
  appendEvent(type, data);
  if (type === "run.completed" || type === "run.failed") {
    finish(data.status || (type === "run.completed" ? "SUCCESS" : "MODEL_ERROR"));
  }
}

function startPolling() {
  if (state.poll) clearInterval(state.poll);
  state.poll = setInterval(async () => {
    if (!state.runId || state.finished) return;
    try {
      const status = await getJson(`/v1/runs/${state.runId}`);
      setStatus(status.status);
      setNode(status.current_node);
      setStep(status.step);
      if (status.status === "WAITING_INPUT") {
        finish(status.status, status);
      } else if (TERMINAL.has(status.status)) {
        finish(status.status, status);
      }
    } catch (_) { /* SSE will continue; retry next tick */ }
  }, 1600);
}

async function finish(status, statusPayload = null) {
  if (state.finished) return;
  state.finished = true;
  if (state.poll) clearInterval(state.poll);
  if (state.es) state.es.close();
  setStatus(status);
  setStep(statusPayload?.step ?? $("#stepValue").textContent);
  if (statusPayload?.current_node) setNode(statusPayload.current_node);
  else if (status === "SUCCESS") setNode("finalize");
  else if (TERMINAL.has(status)) setNode("fail");
  if (status === "SUCCESS") {
    document.querySelectorAll(".step").forEach((item) => {
      item.classList.remove("is-active");
      item.classList.add("is-done");
    });
  }

  const result = await fetchResult();
  if (result) renderResult(result);
  else if (status === "WAITING_INPUT") renderWaiting(statusPayload);
  else renderFailure(status, statusPayload?.error);

  const trace = await fetchTrace();
  if (trace) {
    if (state.eventCount === 0) renderEventHistory(trace);
    renderTrace(trace);
  }
  if (state.runId) {
    $("#traceLink").href = `/v1/runs/${state.runId}/trace?format=jsonl`;
    $("#traceLink").hidden = false;
  }
}

async function fetchResult() {
  if (!state.runId) return null;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      return await getJson(`/v1/runs/${state.runId}/result`);
    } catch (_) {
      await new Promise((resolve) => setTimeout(resolve, 450));
    }
  }
  return null;
}

async function fetchTrace() {
  if (!state.runId) return null;
  try {
    const payload = await getJson(`/v1/runs/${state.runId}/trace`);
    return payload.trace || [];
  } catch (_) { return null; }
}

function showFailure(title, message) {
  $("#failureCard").hidden = false;
  $("#failureTitle").textContent = title;
  $("#failureMessage").textContent = message;
}

function renderFailure(status, error) {
  const messages = {
    NO_MATERIAL: "没有找到可引用的素材。系统不会编造事实，请补充资料后重试。",
    TOOL_ERROR: "检索或规则工具执行失败。系统没有把失败伪装成成功。",
    UNSATISFIABLE_DURATION: "在修订预算内仍无法满足目标时长。",
    UNSATISFIABLE_RULES: "存在无法消除的阻断级规则违规。",
    MAX_STEPS: "达到步数预算，运行被显式终止。",
    MODEL_ERROR: "模型输出无法通过结构或事实校验。",
    INTERRUPTED: "运行被中断。",
    CANCELLED: "运行已取消。",
  };
  showFailure(status.replaceAll("_", " "), error?.message || messages[status] || "运行没有产生最终结果。");
}

function renderWaiting(statusPayload) {
  const missing = statusPayload?.missing_fields || [];
  showFailure("需要补充信息", missing.length ? `缺少：${missing.join("、")}。补充后可以继续运行。` : "运行已暂停，等待用户输入。");
  const card = $("#failureCard");
  const form = el("form", "resume-form");
  const field = el("label", "field");
  field.append(el("span", "", "补充平台"));
  const input = el("input");
  input.name = "platform";
  input.value = $("#platform").value;
  field.append(input);
  const durationField = el("label", "field");
  durationField.append(el("span", "", "补充目标时长"));
  const duration = el("input");
  duration.type = "number";
  duration.name = "target_duration_sec";
  duration.value = $("#duration").value;
  durationField.append(duration);
  const button = el("button", "button button-primary", "继续运行");
  button.type = "submit";
  form.append(field, durationField, button);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const answer = {
      platform: input.value.trim(),
      target_duration_sec: Number(duration.value),
    };
    button.disabled = true;
    try {
      await getJson(`/v1/runs/${state.runId}/resume`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ answer }),
      });
      state.finished = false;
      $("#failureCard").hidden = true;
      setStatus("RUNNING");
      openStream();
      startPolling();
    } catch (error) {
      showFailure("无法恢复", error.message);
    } finally {
      button.disabled = false;
    }
  });
  card.append(form);
}

function renderResult(result) {
  $("#failureCard").hidden = true;
  if (result.script?.shots?.length) {
    $("#storyboardWrap").hidden = false;
    $("#stageEmpty").hidden = true;
    renderStoryboard(result.script.shots);
  }
  renderSources(result.sources || []);
  renderChecks(result.violations || [], result.status, result.risks || []);
  if (result.attempts) {
    setStep(result.attempts.steps);
    setToolCount(result.attempts.tool_calls);
  }
}

function renderStoryboard(shots) {
  const wrap = $("#storyboard");
  wrap.replaceChildren();
  $("#shotCount").textContent = `${shots.length} SHOTS`;
  shots.forEach((shot, index) => {
    const row = el("article", "shot");
    const number = el("span", "shot-num", String(index + 1).padStart(2, "0"));
    const main = el("div", "shot-main");
    main.append(
      el("h4", "", shot.subtitle || shot.visual || "未命名分镜"),
      el("p", "", shot.narration || ""),
      el("span", "shot-meta", shot.claim_ids?.length ? shot.claim_ids.join(" · ") : "NO CLAIM"),
    );
    const time = el("div", "shot-time");
    time.append(el("b", "", String(shot.duration_sec ?? "—")), el("span", "", "SEC"));
    row.append(number, main, time);
    wrap.append(row);
  });
}

function renderSources(sources) {
  const wrap = $("#sourceList");
  wrap.replaceChildren();
  $("#sourceCount").textContent = String(sources.length);
  if (!sources.length) {
    wrap.append(el("p", "muted", "没有来源记录。"));
    return;
  }
  sources.forEach((source) => {
    const item = el("div", "source-item");
    const top = el("div", "source-top");
    top.append(el("b", "", source.source_id || "SOURCE"), el("span", "", source.claim_id || ""));
    item.append(top, el("p", "", source.text || ""), el("small", "", source.source || ""));
    wrap.append(item);
  });
}

function renderChecks(violations, status, risks = []) {
  const wrap = $("#checkList");
  wrap.replaceChildren();
  const count = violations.length + risks.length;
  $("#checkCount").textContent = count ? `${count} ISSUES` : "CLEAR";
  if (!count) {
    const row = el("div", "check-row");
    row.append(el("span", "check-mark", "✓"), el("span", "", "规则检查通过"));
    wrap.append(row);
    if (status === "SUCCESS") {
      const sourceRow = el("div", "check-row");
      sourceRow.append(el("span", "check-mark", "✓"), el("span", "", "事实来源可追溯"));
      wrap.append(sourceRow);
    }
    return;
  }
  violations.forEach((item) => {
    const row = el("div", "check-row");
    row.append(el("span", "check-mark bad", item.severity === "block" ? "!" : "·"), el("span", "", `${item.code} · ${item.message}`));
    wrap.append(row);
  });
  risks.forEach((item) => {
    const row = el("div", "check-row");
    row.append(el("span", "check-mark", "·"), el("span", "", `${item.code} · ${item.message}`));
    wrap.append(row);
  });
}

function renderEventHistory(trace) {
  if (!trace.length) return;
  $("#eventsWrap").hidden = false;
  $("#stageEmpty").hidden = true;
  $("#eventLog").replaceChildren();
  state.eventCount = 0;
  trace.forEach((item) => {
    appendEvent(item.event_type, {
      ...item,
      status: item.observation?.status,
      source_ids: item.observation?.source_ids,
      missing: item.observation?.missing,
    });
  });
}

function markGraphVisited(trace) {
  trace.forEach((item) => {
    if (item.node && FLOW_NODES.has(item.node)) state.graphVisited.add(item.node);
  });
  document.querySelectorAll("[data-flow-node]").forEach((item) => {
    const key = item.dataset.flowNode;
    item.classList.toggle("is-done", state.graphVisited.has(key) && !item.classList.contains("is-active"));
  });
}

function renderTrace(trace) {
  const wrap = $("#traceSummary");
  wrap.replaceChildren();
  if (!trace.length) {
    wrap.append(el("p", "muted", "没有 Trace。"));
    return;
  }
  trace.filter((item) => item.action || item.event_type === "tool.result").slice(-7).forEach((item) => {
    const row = el("div", "trace-row");
    row.append(
      el("span", "trace-step", String(item.step ?? "—")),
      el("span", "trace-action", item.action || item.node || item.event_type || "—"),
    );
    if (item.basis) row.append(el("span", "trace-basis", item.basis));
    wrap.append(row);
  });
  markGraphVisited(trace);
}

async function runAgent(event) {
  event.preventDefault();
  if (!state.runId || state.finished) clearRunView();
  const topic = $("#topic").value.trim();
  if (!topic) {
    $("#topic").focus();
    return;
  }
  $("#runButton").disabled = true;
  $("#stageEmpty").hidden = false;
  setStatus("QUEUED");
  setNode("validate_input");
  const payload = {
    brief: {
      topic,
      platform: $("#platform").value,
      target_duration_sec: Number($("#duration").value),
      audience: $("#audience").value.trim(),
      style: $("#style").value.trim(),
    },
  };
  try {
    const created = await getJson("/v1/runs", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    state.runId = created.run_id;
    state.finished = false;
    localStorage.setItem("script-agent-last-run", created.run_id);
    $("#runId").textContent = shortId(created.run_id);
    $("#eventsWrap").hidden = false;
    $("#stageEmpty").hidden = true;
    setStatus("RUNNING");
    openStream();
    startPolling();
  } catch (error) {
    setStatus("TOOL_ERROR");
    showFailure("API 没有接受这次运行", error.message);
    $("#runButton").disabled = false;
  }
}

function initKeyboard() {
  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      $("#briefForm").requestSubmit();
    }
  });
}

function restoreRun(runId) {
  if (!runId) return;
  state.runId = runId;
  $("#runId").textContent = shortId(runId);
  getJson(`/v1/runs/${runId}`).then((status) => {
    setStatus(status.status);
    setNode(status.current_node);
    setStep(status.step);
    $("#stageEmpty").hidden = true;
    if (TERMINAL.has(status.status)) finish(status.status, status);
    else {
      openStream();
      startPolling();
    }
  }).catch(() => { /* stale run id */ });
}

function restoreLastRun() {
  const sharedRun = new URLSearchParams(window.location.search).get("run");
  const storedRun = localStorage.getItem("script-agent-last-run");
  restoreRun(sharedRun || storedRun);
}

/* --------------------------------------------------------------------------
   Knowledge base
   -------------------------------------------------------------------------- */
const knowledgeState = {
  documents: [],
  selected: null,
  chunks: [],
  filter: "all",
  poll: null,
};

async function fetchDocuments() {
  try {
    const documents = await getJson("/v1/knowledge/documents");
    knowledgeState.documents = documents;
    renderDocuments();
  } catch (error) {
    $("#documentList").replaceChildren(el("p", "muted", `知识库不可用：${error.message}`));
  }
}

function renderDocuments() {
  const wrap = $("#documentList");
  wrap.replaceChildren();
  $("#documentCount").textContent = String(knowledgeState.documents.length);
  if (!knowledgeState.documents.length) {
    wrap.append(el("p", "muted doc-empty", "还没有上传文档。选择 PDF、DOCX、TXT、MD、CSV 或 JSON。"));
    return;
  }
  knowledgeState.documents.forEach((doc) => {
    const item = el("button", "document-item");
    item.type = "button";
    item.dataset.documentId = doc.id;
    item.classList.toggle("is-selected", knowledgeState.selected === doc.id);
    const name = el("span", "doc-name", doc.filename);
    const status = el("span", `doc-status ${doc.status}`, doc.status);
    const meta = el("span", "doc-meta", `${doc.parent_count || 0} PARENT · ${doc.child_count || 0} CHILD · ${(doc.size_bytes / 1024).toFixed(1)} KB`);
    item.append(name, status, meta);
    if (doc.error) item.append(el("span", "doc-error", doc.error));
    item.addEventListener("click", () => selectDocument(doc));
    wrap.append(item);
  });
}

async function selectDocument(doc) {
  knowledgeState.selected = doc.id;
  renderDocuments();
  $("#chunkDocument").textContent = doc.filename.toUpperCase();
  $("#deactivateDoc").hidden = false;
  $("#deleteDoc").hidden = false;
  const wrap = $("#chunkList");
  wrap.replaceChildren(el("p", "muted", "读取切片中…"));
  try {
    const chunks = await getJson(`/v1/knowledge/documents/${doc.id}/chunks`);
    knowledgeState.chunks = chunks;
    renderChunks();
  } catch (error) {
    wrap.replaceChildren(el("p", "muted", `读取失败：${error.message}`));
  }
}

function renderChunks() {
  const wrap = $("#chunkList");
  wrap.replaceChildren();
  const filtered = knowledgeState.chunks.filter((chunk) => knowledgeState.filter === "all" || chunk.chunk_type === knowledgeState.filter);
  $("#chunkCount").textContent = `${filtered.length} CHUNKS`;
  if (!filtered.length) {
    wrap.append(el("p", "muted", "这个筛选下没有切片。"));
    return;
  }
  filtered.forEach((chunk) => {
    const item = el("article", `chunk-item ${chunk.chunk_type}`);
    const head = el("div", "chunk-item-head");
    head.append(
      el("span", `chunk-type ${chunk.chunk_type}`, chunk.chunk_type.toUpperCase()),
      el("span", "", `#${String(chunk.ordinal).padStart(2, "0")}`),
      el("span", "", chunk.heading_path?.join(" / ") || ""),
    );
    item.append(head, el("p", "", chunk.text));
    item.append(el("small", "", chunk.chunk_type === "child" ? `MATERIAL ${chunk.material_id || "—"}` : `CONTEXT · ${chunk.id}`));
    wrap.append(item);
  });
}

async function uploadKnowledgeFile(file) {
  if (!file) return;
  $("#uploadProgress").hidden = false;
  $("#uploadFile").textContent = file.name;
  setUploadProgress("UPLOADING", 4);
  const form = new FormData();
  form.append("file", file);
  try {
    const response = await fetch("/v1/knowledge/documents", { method: "POST", body: form });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail?.message || payload.detail || `${response.status}`);
    $("#uploadFile").textContent = payload.document.filename;
    if (payload.deduplicated) {
      setUploadProgress("READY / DEDUP", 100);
      await fetchDocuments();
      const doc = knowledgeState.documents.find((item) => item.id === payload.document.id);
      if (doc) selectDocument(doc);
      return;
    }
    if (payload.job?.id) pollKnowledgeJob(payload.job.id, payload.document.id);
  } catch (error) {
    setUploadProgress(`FAILED / ${error.message}`, 0);
  }
}

function setUploadProgress(stage, percent) {
  $("#uploadStage").textContent = stage;
  $("#uploadPercent").textContent = `${Math.round(percent)}%`;
  $("#progressBar").style.width = `${Math.min(100, Math.max(0, percent))}%`;
}

async function pollKnowledgeJob(jobId, documentId) {
  if (knowledgeState.poll) clearInterval(knowledgeState.poll);
  let attempts = 0;
  let timer = null;

  const poll = async () => {
    attempts += 1;
    try {
      const job = await getJson(`/v1/knowledge/jobs/${jobId}`);
      setUploadProgress(job.stage || job.status, job.progress || 0);
      if (job.status === "READY" || job.status === "FAILED") {
        if (timer) clearInterval(timer);
        knowledgeState.poll = null;
        if (job.status === "FAILED") {
          setUploadProgress(`FAILED / ${job.error || "解析失败"}`, 0);
        } else {
          setUploadProgress("READY", 100);
          await fetchDocuments();
          const doc = knowledgeState.documents.find((item) => item.id === documentId);
          if (doc) selectDocument(doc);
        }
      }
    } catch (error) {
      if (attempts > 20) {
        if (timer) clearInterval(timer);
        knowledgeState.poll = null;
        setUploadProgress(`FAILED / ${error.message}`, 0);
      }
    }
    if (attempts > 180) {
      if (timer) clearInterval(timer);
      knowledgeState.poll = null;
      setUploadProgress("FAILED / TIMEOUT", 0);
    }
  };

  timer = setInterval(poll, 1000);
  knowledgeState.poll = timer;
  await poll();
}

async function deactivateSelectedDocument() {
  if (!knowledgeState.selected) return;
  try {
    await getJson(`/v1/knowledge/documents/${knowledgeState.selected}/deactivate`, { method: "POST" });
    $("#deactivateDoc").hidden = true;
    $("#chunkDocument").textContent = "NO DOCUMENT";
    knowledgeState.chunks = [];
    renderChunks();
    await fetchDocuments();
  } catch (error) {
    $("#chunkList").replaceChildren(el("p", "muted", `下架失败：${error.message}`));
  }
}

async function deleteSelectedDocument() {
  if (!knowledgeState.selected) return;
  const doc = knowledgeState.documents.find((item) => item.id === knowledgeState.selected);
  const name = doc?.filename || "这份文档";
  if (!window.confirm(`永久删除「${name}」？切片、材料、任务和原文件都会删除，且不可恢复。`)) return;
  try {
    const response = await fetch(`/v1/knowledge/documents/${knowledgeState.selected}`, { method: "DELETE" });
    if (!response.ok) {
      let detail = `${response.status}`;
      try {
        const body = await response.json();
        detail = body.detail?.message || body.detail || detail;
      } catch (_) { /* keep status */ }
      throw new Error(detail);
    }
    knowledgeState.selected = null;
    knowledgeState.chunks = [];
    $("#deactivateDoc").hidden = true;
    $("#deleteDoc").hidden = true;
    $("#chunkDocument").textContent = "NO DOCUMENT";
    renderChunks();
    await fetchDocuments();
  } catch (error) {
    $("#chunkList").replaceChildren(el("p", "muted", `删除失败：${error.message}`));
  }
}

async function debugSearch(event) {
  event.preventDefault();
  const query = $("#debugQuery").value.trim();
  if (!query) return;
  const wrap = $("#debugResults");
  wrap.replaceChildren(el("p", "muted", "检索中…"));
  try {
    const result = await getJson("/v1/knowledge/search", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ query, top_k: 8 }),
    });
    wrap.replaceChildren();
    const summary = el("div", "debug-summary");
    summary.append(
      el("span", "", `STATUS ${result.status}`),
      el("span", "", `CANDIDATES ${result.debug?.candidate_count ?? "—"}`),
      el("span", "", `RERANK ${result.debug?.rerank_status || "—"}`),
      el("span", "", `DEGRADED ${result.degraded ? "YES" : "NO"}`),
    );
    wrap.append(summary);
    (result.results || []).forEach((hit, index) => {
      const row = el("div", "debug-hit");
      const main = el("div");
      main.append(el("p", "", hit.text));
      main.append(el("small", "", `${hit.source_id} · ${hit.source || ""}${hit.parent_id ? " · PARENT" : ""}`));
      row.append(
        el("span", "debug-rank", String(index + 1).padStart(2, "0")),
        main,
        el("span", "debug-score", hit.rerank_score != null ? hit.rerank_score.toFixed(3) : String(hit.score ?? "—")),
      );
      wrap.append(row);
    });
    (result.contexts || []).forEach((context) => {
      wrap.append(el("div", "debug-context", `PARENT ${context.parent_id}\n${context.text}`));
    });
    if (result.risk) wrap.append(el("p", "muted", `${result.risk.code} · ${result.risk.message}`));
  } catch (error) {
    wrap.replaceChildren(el("p", "muted", `检索失败：${error.message}`));
  }
}

async function runEvaluation() {
  const button = $("#runEvaluation");
  button.disabled = true;
  $("#evaluationSummary").replaceChildren(el("p", "muted", "运行 8 条 query 的 RRF / rerank 对照…"));
  $("#evaluationTable").replaceChildren();
  try {
    const result = await getJson("/v1/knowledge/evaluation?k=5");
    renderEvaluation(result);
  } catch (error) {
    $("#evaluationSummary").replaceChildren(el("p", "muted", `评估失败：${error.message}`));
  } finally {
    button.disabled = false;
  }
}

function renderEvaluation(result) {
  const summary = $("#evaluationSummary");
  summary.replaceChildren(
    evaluationGroup("RRF ONLY", result.rrf),
    evaluationGroup("WITH RERANK", result.rerank),
  );
  const table = $("#evaluationTable");
  table.replaceChildren();
  const legend = el("div", "eval-legend");
  legend.append(
    el("span", "", `DATASET ${result.dataset}`),
    el("span", "", `CASES ${result.case_count}`),
    el("span", "", `WINS ${result.comparison.rerank_wins}`),
    el("span", "", `LOSSES ${result.comparison.rerank_losses}`),
    el("span", "", `TIES ${result.comparison.ties}`),
    el("span", "", `ERRORS ${result.errors}`),
  );
  table.append(legend);
  (result.cases || []).forEach((item) => {
    const row = el("div", "eval-row");
    const query = el("div", "eval-query");
    query.append(el("b", "", item.query), el("small", "", `${item.id} · EXPECT ${item.expected.join(", ")}`));
    const rrfRank = firstRelevantRank(item.rrf.retrieved, item.expected);
    const rerankRank = firstRelevantRank(item.rerank.retrieved, item.expected);
    const rrfCell = el("div", "eval-cell");
    rrfCell.append(el("b", "", `RRF  RANK ${rrfRank ?? "MISS"}`), el("span", "", `Recall ${item.rrf.recall} · MRR ${item.rrf.mrr}`));
    const rerankCell = el("div", "eval-cell");
    const cls = rerankRank != null && (rrfRank == null || rerankRank < rrfRank) ? "win" : rerankRank != null && rrfRank != null && rerankRank > rrfRank ? "lose" : "";
    rerankCell.append(el("b", cls, `RERANK  RANK ${rerankRank ?? "MISS"}`), el("span", "", `Recall ${item.rerank.recall} · MRR ${item.rerank.mrr}`));
    row.append(query, rrfCell, rerankCell);
    table.append(row);
  });
}

function evaluationGroup(label, data) {
  const group = el("div", "eval-group");
  const head = el("div", "eval-group-head");
  head.append(el("b", "", label), el("span", "", "K=5"));
  const metrics = el("div", "eval-metrics");
  [["RECALL", data.recall], ["MRR", data.mrr], ["PRECISION", data.precision]].forEach(([name, value]) => {
    const metric = el("div", "eval-metric");
    metric.append(el("strong", "", Number(value).toFixed(2)), el("span", "", name));
    metrics.append(metric);
  });
  group.append(head, metrics);
  return group;
}

function firstRelevantRank(retrieved, expected) {
  const wanted = new Set(expected || []);
  const index = (retrieved || []).findIndex((id) => wanted.has(id));
  return index >= 0 ? index + 1 : null;
}

function initKnowledge() {
  $("#chooseFile").addEventListener("click", (event) => {
    event.stopPropagation();
    $("#fileInput").click();
  });
  $("#dropzone").addEventListener("click", () => $("#fileInput").click());
  $("#fileInput").addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    if (file) uploadKnowledgeFile(file);
    event.target.value = "";
  });
  const drop = $("#dropzone");
  ["dragenter", "dragover"].forEach((name) => drop.addEventListener(name, (event) => {
    event.preventDefault();
    drop.classList.add("is-over");
  }));
  ["dragleave", "drop"].forEach((name) => drop.addEventListener(name, (event) => {
    event.preventDefault();
    drop.classList.remove("is-over");
  }));
  drop.addEventListener("drop", (event) => {
    const file = event.dataTransfer?.files?.[0];
    if (file) uploadKnowledgeFile(file);
  });
  $("#debugSearchForm").addEventListener("submit", debugSearch);
  $("#deactivateDoc").addEventListener("click", deactivateSelectedDocument);
  $("#deleteDoc").addEventListener("click", deleteSelectedDocument);
  $("#runEvaluation").addEventListener("click", runEvaluation);
  document.querySelectorAll("[data-chunk-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      knowledgeState.filter = button.dataset.chunkFilter;
      document.querySelectorAll("[data-chunk-filter]").forEach((item) => item.classList.toggle("is-active", item === button));
      renderChunks();
    });
  });
  fetchDocuments();
}

$("#briefForm").addEventListener("submit", runAgent);
$("#heroRunLink").addEventListener("click", () => setTimeout(() => $("#topic").focus(), 320));
initKeyboard();
initKnowledge();
checkReady();
restoreLastRun();
