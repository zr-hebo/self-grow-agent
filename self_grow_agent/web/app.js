"use strict";

const state = {
  managementKey: "",
  requirements: [],
  routes: [],
  selectedRequirementId: null,
  linkedRouteId: null,
};

const elements = {
  connectionForm: document.querySelector("#connection-form"),
  managementKey: document.querySelector("#management-key"),
  healthDot: document.querySelector("#health-dot"),
  healthLabel: document.querySelector("#health-label"),
  refreshAll: document.querySelector("#refresh-all"),
  newRequirement: document.querySelector("#new-requirement"),
  requirementCount: document.querySelector("#requirement-count"),
  requirementList: document.querySelector("#requirement-list"),
  requirementForm: document.querySelector("#requirement-form"),
  requirementId: document.querySelector("#requirement-id"),
  linkedRouteId: document.querySelector("#linked-route-id"),
  requirementTitle: document.querySelector("#requirement-title"),
  routeProject: document.querySelector("#route-project"),
  routeMethod: document.querySelector("#route-method"),
  routePath: document.querySelector("#route-path"),
  requirementInstruction: document.querySelector("#requirement-instruction"),
  instructionLength: document.querySelector("#instruction-length"),
  editorStatus: document.querySelector("#editor-status"),
  linkedRouteNote: document.querySelector("#linked-route-note"),
  linkedRouteLabel: document.querySelector("#linked-route-label"),
  unlinkRoute: document.querySelector("#unlink-route"),
  rebaseRoute: document.querySelector("#rebase-route"),
  formMessage: document.querySelector("#form-message"),
  saveRequirement: document.querySelector("#save-requirement"),
  implementRequirement: document.querySelector("#implement-requirement"),
  implementLabel: document.querySelector("#implement-label"),
  eventList: document.querySelector("#event-list"),
  routeCount: document.querySelector("#route-count"),
  routeList: document.querySelector("#route-list"),
  requestForm: document.querySelector("#request-form"),
  requestMethod: document.querySelector("#request-method"),
  requestPath: document.querySelector("#request-path"),
  requestBody: document.querySelector("#request-body"),
  responseStatus: document.querySelector("#response-status"),
  responseOutput: document.querySelector("#response-output"),
  toast: document.querySelector("#toast"),
};

const statusLabels = {
  draft: "草稿",
  implementing: "实现中",
  active: "已发布",
  failed: "失败",
};

function createElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) {
    element.className = className;
  }
  if (text !== undefined) {
    element.textContent = text;
  }
  return element;
}

function clearElement(element) {
  element.replaceChildren();
}

function formatTime(value) {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

function setHealth(online, label) {
  elements.healthDot.className = `status-dot ${online ? "status-online" : "status-offline"}`;
  elements.healthLabel.textContent = label;
}

function setBusy(busy, label = "生成并热加载") {
  elements.saveRequirement.disabled = busy;
  elements.implementRequirement.disabled = busy;
  elements.rebaseRoute.disabled = busy;
  elements.implementLabel.textContent = busy ? "正在实现…" : label;
}

function showMessage(message, kind = "success") {
  elements.formMessage.textContent = message;
  elements.formMessage.className = `form-message ${kind}`;
  elements.formMessage.hidden = false;
}

function hideMessage() {
  elements.formMessage.hidden = true;
  elements.formMessage.textContent = "";
}

let toastTimer = null;
function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.hidden = false;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    elements.toast.hidden = true;
  }, 3200);
}

async function parseResponse(response) {
  const contentType = response.headers.get("content-type") || "";
  let payload;
  if (contentType.includes("application/json")) {
    payload = await response.json();
  } else {
    payload = await response.text();
  }
  if (!response.ok) {
    const detail = payload && typeof payload === "object" ? payload.detail : payload;
    const error = new Error(detail || `请求失败 (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

async function managementRequest(path, options = {}) {
  if (!state.managementKey) {
    throw new Error("请先输入管理密钥并连接");
  }
  const headers = new Headers(options.headers || {});
  headers.set("X-Management-Key", state.managementKey);
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, {...options, headers});
  return parseResponse(response);
}

async function checkHealth() {
  try {
    const response = await fetch("/healthz", {cache: "no-store"});
    const payload = await parseResponse(response);
    const health = payload.data;
    setHealth(health?.status === "ok", health?.status === "ok" ? "运行时在线" : "运行时异常");
  } catch (_error) {
    setHealth(false, "运行时离线");
  }
}

function statusChip(status) {
  return createElement(
    "span",
    `status-chip status-${status}`,
    statusLabels[status] || status,
  );
}

function renderRequirements() {
  clearElement(elements.requirementList);
  elements.requirementCount.textContent = String(state.requirements.length);

  if (!state.requirements.length) {
    const empty = createElement("div", "empty-state compact");
    empty.append(
      createElement("span", "empty-glyph", "⌁"),
      createElement("p", "", state.managementKey ? "还没有需求，创建第一个功能" : "连接后查看本地需求记录"),
    );
    elements.requirementList.append(empty);
    return;
  }

  for (const requirement of state.requirements) {
    const button = createElement(
      "button",
      `requirement-card${requirement.id === state.selectedRequirementId ? " selected" : ""}`,
    );
    button.type = "button";
    button.dataset.requirementId = requirement.id;
    const top = createElement("span", "card-topline");
    top.append(createElement("strong", "", requirement.title), statusChip(requirement.status));
    const route = requirement.route_id
      ? `[${requirement.project}] ${requirement.method} ${requirement.path} · v${requirement.route_version}`
      : `[${requirement.project}] ${requirement.method} ${requirement.path} · 未发布`;
    button.append(top, createElement("small", "", route));
    button.addEventListener("click", () => selectRequirement(requirement.id));
    elements.requirementList.append(button);
  }
}

function renderRoutes() {
  clearElement(elements.routeList);
  elements.routeCount.textContent = String(state.routes.length);
  if (!state.routes.length) {
    const empty = createElement("div", "empty-state compact");
    empty.append(
      createElement("span", "empty-glyph", "◇"),
      createElement("p", "", "还没有动态路由"),
    );
    elements.routeList.append(empty);
    return;
  }

  let currentProject = null;
  for (const route of state.routes) {
    if (route.project !== currentProject) {
      currentProject = route.project;
      elements.routeList.append(createElement("h3", "route-project-heading", currentProject));
    }
    const card = createElement("article", "route-card");
    const header = createElement("div", "route-card-header");
    header.append(
      createElement("span", "method-chip", route.method),
      createElement("small", "", `v${route.version}`),
    );
    card.append(header, createElement("strong", "", route.path));
    card.append(createElement("p", "", route.description || "动态处理器"));

    const actions = createElement("div", "route-card-actions");
    const iterate = createElement("button", "text-button", "继续开发");
    iterate.type = "button";
    iterate.addEventListener("click", () => startFromRoute(route));
    const call = createElement("button", "text-button", "在 Request Lab 调用");
    call.type = "button";
    call.addEventListener("click", () => prepareRequest(route));
    actions.append(iterate, call);
    card.append(actions);
    elements.routeList.append(card);
  }
}

function renderEvents(events) {
  clearElement(elements.eventList);
  if (!events.length) {
    elements.eventList.append(createElement("li", "empty-event", "还没有实现事件"));
    return;
  }
  for (const event of events) {
    const item = createElement("li", "event-item");
    item.append(
      createElement("span", "", event.message),
      createElement("time", "", formatTime(event.created_at)),
    );
    elements.eventList.append(item);
  }
}

function updateLinkedRouteNote() {
  const route = state.routes.find((item) => item.route_id === state.linkedRouteId);
  const requirement = state.requirements.find(
    (item) => item.id === state.selectedRequirementId,
  );
  elements.linkedRouteNote.hidden = !route;
  const baseVersion = requirement && requirement.route_id === route?.route_id
    ? requirement.route_version
    : null;
  elements.linkedRouteLabel.textContent = route && baseVersion !== null
    ? `[${route.project}] ${route.method} ${route.path} · 当前 v${route.version} / 需求基线 v${baseVersion}`
    : route
      ? `[${route.project}] ${route.method} ${route.path} · v${route.version}`
      : "";
  elements.routeProject.disabled = Boolean(route);
  elements.routeMethod.disabled = Boolean(route);
  elements.routePath.disabled = Boolean(route);
  elements.unlinkRoute.hidden = Boolean(requirement);
  elements.rebaseRoute.hidden = !(
    route
    && requirement
    && requirement.status !== "implementing"
    && requirement.route_version !== route.version
  );
}

function resetEditor() {
  state.selectedRequirementId = null;
  state.linkedRouteId = null;
  elements.requirementForm.reset();
  elements.requirementId.value = "";
  elements.linkedRouteId.value = "";
  elements.routeMethod.value = "GET";
  elements.routeProject.value = "default";
  elements.routePath.value = "/hello";
  elements.instructionLength.textContent = "0";
  elements.editorStatus.className = "status-chip status-draft";
  elements.editorStatus.textContent = "新需求";
  elements.routeProject.disabled = false;
  elements.routeMethod.disabled = false;
  elements.routePath.disabled = false;
  hideMessage();
  updateLinkedRouteNote();
  renderEvents([]);
  renderRequirements();
  elements.requirementTitle.focus();
}

async function selectRequirement(requirementId) {
  const requirement = state.requirements.find((item) => item.id === requirementId);
  if (!requirement) {
    return;
  }
  state.selectedRequirementId = requirement.id;
  state.linkedRouteId = requirement.route_id;
  elements.requirementId.value = requirement.id;
  elements.linkedRouteId.value = requirement.route_id || "";
  elements.requirementTitle.value = requirement.title;
  elements.routeProject.value = requirement.project;
  elements.routeMethod.value = requirement.method;
  elements.routePath.value = requirement.path;
  elements.requirementInstruction.value = requirement.instruction;
  elements.instructionLength.textContent = String(requirement.instruction.length);
  elements.editorStatus.className = `status-chip status-${requirement.status}`;
  elements.editorStatus.textContent = statusLabels[requirement.status] || requirement.status;
  hideMessage();
  updateLinkedRouteNote();
  renderRequirements();
  try {
    const events = await managementRequest(
      `/api/v1/manage/requirements/${encodeURIComponent(requirement.id)}/events`,
    );
    if (state.selectedRequirementId === requirement.id) {
      renderEvents(events);
    }
  } catch (error) {
    renderEvents([]);
    showMessage(error.message, "error");
  }
}

function startFromRoute(route) {
  resetEditor();
  state.linkedRouteId = route.route_id;
  elements.linkedRouteId.value = route.route_id;
  elements.requirementTitle.value = `迭代 ${route.method} ${route.path}`;
  elements.routeProject.value = route.project;
  elements.routeMethod.value = route.method;
  elements.routePath.value = route.path;
  elements.requirementInstruction.value = "";
  elements.instructionLength.textContent = "0";
  updateLinkedRouteNote();
  elements.requirementInstruction.focus();
}

function prepareRequest(route) {
  elements.requestMethod.value = route.method;
  elements.requestPath.value = route.path;
  elements.requestBody.value = "";
  elements.requestPath.focus();
}

async function refreshData({preserveSelection = true} = {}) {
  const [requirements, routes] = await Promise.all([
    managementRequest("/api/v1/manage/requirements"),
    managementRequest("/api/v1/manage/routes"),
  ]);
  state.requirements = requirements;
  state.routes = routes;
  renderRequirements();
  renderRoutes();
  updateLinkedRouteNote();
  if (preserveSelection && state.selectedRequirementId) {
    const selected = state.requirements.find((item) => item.id === state.selectedRequirementId);
    if (selected) {
      await selectRequirement(selected.id);
    } else {
      resetEditor();
    }
  }
}

function draftPayload() {
  const payload = {
    title: elements.requirementTitle.value.trim(),
    instruction: elements.requirementInstruction.value.trim(),
    project: elements.routeProject.value.trim(),
    method: elements.routeMethod.value,
    path: elements.routePath.value.trim(),
  };
  if (state.linkedRouteId) {
    payload.route_id = state.linkedRouteId;
  }
  return payload;
}

async function saveRequirement() {
  if (!elements.requirementForm.reportValidity()) {
    return null;
  }
  hideMessage();
  const payload = draftPayload();
  let saved;
  if (state.selectedRequirementId) {
    saved = await managementRequest(
      `/api/v1/manage/requirements/${encodeURIComponent(state.selectedRequirementId)}`,
      {
        method: "PATCH",
        body: JSON.stringify({title: payload.title, instruction: payload.instruction}),
      },
    );
  } else {
    saved = await managementRequest("/api/v1/manage/requirements", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.selectedRequirementId = saved.id;
  }
  await refreshData();
  showMessage("需求已保存到本地 SQLite。", "success");
  return saved;
}

async function implementRequirement() {
  setBusy(true);
  hideMessage();
  try {
    const saved = await saveRequirement();
    if (!saved) {
      return;
    }
    const requirementId = state.selectedRequirementId;
    showMessage("生成后端正在处理需求，完成后会自动校验并热加载。", "success");
    const implemented = await managementRequest(
      `/api/v1/manage/requirements/${encodeURIComponent(requirementId)}/implement`,
      {method: "POST"},
    );
    await refreshData();
    await selectRequirement(implemented.id);
    showMessage(
      `已发布 [${implemented.project}] ${implemented.method} ${implemented.path} · v${implemented.route_version}`,
      "success",
    );
    showToast("功能已生成并热加载，可以立即调用业务 API。");
  } catch (error) {
    try {
      await refreshData();
    } catch (_refreshError) {
      // Preserve the original implementation failure for the user.
    }
    showMessage(error.message, "error");
  } finally {
    setBusy(false);
  }
}

async function rebaseRequirement() {
  if (!state.selectedRequirementId) {
    return;
  }
  setBusy(true);
  hideMessage();
  try {
    const rebased = await managementRequest(
      `/api/v1/manage/requirements/${encodeURIComponent(state.selectedRequirementId)}/rebase`,
      {method: "POST"},
    );
    await refreshData();
    await selectRequirement(rebased.id);
    showMessage(`已同步到当前路由 v${rebased.route_version}，可以继续实现。`, "success");
  } catch (error) {
    showMessage(error.message, "error");
  } finally {
    setBusy(false);
  }
}

async function sendBusinessRequest() {
  if (!elements.requestForm.reportValidity()) {
    return;
  }
  const method = elements.requestMethod.value;
  const path = elements.requestPath.value.trim();
  const options = {method, headers: {Accept: "application/json"}};
  const bodyText = elements.requestBody.value.trim();
  if (bodyText && !["GET", "DELETE"].includes(method)) {
    try {
      JSON.parse(bodyText);
    } catch (_error) {
      elements.responseStatus.textContent = "INVALID JSON";
      elements.responseOutput.textContent = "请求体必须是有效 JSON。";
      return;
    }
    options.headers["Content-Type"] = "application/json";
    options.body = bodyText;
  }

  elements.responseStatus.textContent = "SENDING";
  elements.responseOutput.textContent = "请求执行中…";
  try {
    const response = await fetch(path, options);
    const text = await response.text();
    let output = text;
    try {
      output = JSON.stringify(JSON.parse(text), null, 2);
    } catch (_error) {
      // Keep non-JSON business responses readable as plain text.
    }
    elements.responseStatus.textContent = `${response.status} ${response.statusText}`;
    elements.responseOutput.textContent = output || "<empty response>";
  } catch (error) {
    elements.responseStatus.textContent = "NETWORK ERROR";
    elements.responseOutput.textContent = error.message;
  }
}

elements.connectionForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  state.managementKey = elements.managementKey.value;
  try {
    await refreshData({preserveSelection: false});
    setHealth(true, "控制台已连接");
    showToast("管理连接已建立；密钥仅保存在当前页面内存。");
  } catch (error) {
    state.managementKey = "";
    setHealth(false, error.status === 401 ? "管理密钥无效" : "连接失败");
    showMessage(error.message, "error");
  }
});

elements.refreshAll.addEventListener("click", async () => {
  try {
    await refreshData();
    showToast("需求与运行时状态已刷新。");
  } catch (error) {
    showMessage(error.message, "error");
  }
});

elements.newRequirement.addEventListener("click", resetEditor);
elements.unlinkRoute.addEventListener("click", resetEditor);
elements.rebaseRoute.addEventListener("click", rebaseRequirement);
elements.requirementInstruction.addEventListener("input", () => {
  elements.instructionLength.textContent = String(elements.requirementInstruction.value.length);
});
elements.requirementForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  setBusy(true);
  try {
    await saveRequirement();
  } catch (error) {
    showMessage(error.message, "error");
  } finally {
    setBusy(false);
  }
});
elements.implementRequirement.addEventListener("click", implementRequirement);
elements.requestForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await sendBusinessRequest();
});

checkHealth();
