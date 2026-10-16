"use strict";

// ---------- 공통 ----------

const $ = (id) => document.getElementById(id);

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

// 토큰은 이 변수(메모리)에만 둔다. URL·localStorage·콘솔에 남기지 않는다.
let appToken = null;
// 로그아웃 후 늦게 도착한 응답이 화면에 그려지지 않도록 세션 번호로 구분한다.
let sessionId = 0;

// 조회 결과(메모리). 로그아웃 시 모두 지운다.
let currentContext = null;
let ec2Items = [];
let actionItems = [];
// 응답을 받지 못한 제어 요청: instance_id → { action, requestId, name }. 같은 키를 재사용하기 위해 보관한다.
const lostRequests = new Map();
let controlBusy = false;

async function api(path, { method = "GET", body } = {}) {
  const headers = { Accept: "application/json", Authorization: `Bearer ${appToken ?? ""}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";

  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
    });
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", "서버 응답을 받지 못했습니다. 서버가 실행 중인지 확인하세요.");
  }

  let data = null;
  try {
    data = await response.json();
  } catch {
    // JSON이 아닌 응답은 아래에서 처리
  }

  if (!response.ok) {
    const detail = data && typeof data.detail === "object" && !Array.isArray(data.detail) ? data.detail : null;
    throw new ApiError(
      response.status,
      detail?.code || (response.status === 422 ? "VALIDATION_ERROR" : "HTTP_ERROR"),
      detail?.message || `요청이 실패했습니다. (HTTP ${response.status})`,
    );
  }
  if (!data) throw new ApiError(response.status, "INVALID_RESPONSE", "서버 응답을 해석하지 못했습니다.");
  return data;
}

// 상태 영역: loading / empty / error / success / 숨김(데이터 있음)
function setStatus(statusEl, kind, message, code) {
  statusEl.replaceChildren();
  statusEl.className = "status";
  if (!kind) return;
  statusEl.classList.add(`status-${kind}`);
  statusEl.append(el("span", message));
  if (code) statusEl.append(el("span", code, "status-code"));
}

function setCount(id, state, value) {
  if (!id) return;
  const node = $(id);
  node.className = "card-value";
  if (state === "loading") node.textContent = "…";
  else if (state === "error") {
    node.textContent = "오류";
    node.classList.add("card-error");
  } else if (state === "reset") node.textContent = "-";
  else node.textContent = String(value);
}

function formatTime(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? String(iso) : date.toLocaleString("ko-KR");
}

// ---------- 탭 ----------

const tabs = document.querySelectorAll(".tabs [role=tab]");

function selectTab(name) {
  tabs.forEach((t) => {
    const selected = t.dataset.tab === name;
    t.setAttribute("aria-selected", String(selected));
    $(`tab-${t.dataset.tab}`).hidden = !selected;
  });
}

tabs.forEach((tab) => tab.addEventListener("click", () => selectTab(tab.dataset.tab)));

// ---------- 대시보드 ----------

function renderContext(ctx) {
  currentContext = ctx;
  $("ctx-account").textContent = ctx.account_id;
  $("ctx-region").textContent = ctx.region;
  $("ctx-arn").textContent = ctx.caller_arn;
  setStatus($("context-status"), null);
  $("context-info").hidden = false;
}

async function loadContext(sid) {
  $("context-info").hidden = true;
  setStatus($("context-status"), "loading", "계정 정보를 조회하는 중…");
  try {
    const ctx = await api("/api/context");
    if (sid !== sessionId) return;
    renderContext(ctx);
  } catch (err) {
    if (sid !== sessionId) return;
    if (handleAuthError(err)) return;
    currentContext = null;
    setStatus($("context-status"), "error", err.message, err.code);
  }
}

// ---------- 목록 공통 ----------

const LISTS = {
  ec2: {
    path: "/api/ec2",
    statusId: "ec2-status",
    countId: "count-ec2",
    containerId: "ec2-table",
    loadingMessage: "EC2 인스턴스를 조회하는 중…",
    emptyMessage: "이 리전에 EC2 인스턴스가 없습니다.",
    store: (items) => { ec2Items = items; },
    render: () => { renderEc2(); rerenderIfShown("actions"); },
    clear: () => { ec2Items = []; $("ec2-table").querySelector("tbody").replaceChildren(); },
  },
  actions: {
    path: "/api/actions",
    statusId: "actions-status",
    countId: null,
    containerId: "actions-table",
    loadingMessage: "제어 이력을 조회하는 중…",
    emptyMessage: "제어 이력이 없습니다.",
    store: (items) => { actionItems = items; },
    render: () => { renderActions(); rerenderIfShown("ec2"); },
    clear: () => { actionItems = []; $("actions-table").querySelector("tbody").replaceChildren(); },
  },
  s3: {
    path: "/api/s3",
    statusId: "s3-status",
    countId: "count-s3",
    containerId: "s3-list",
    loadingMessage: "S3 버킷을 조회하는 중…",
    emptyMessage: "이 계정에 S3 버킷이 없습니다.",
    store: () => {},
    render: renderS3,
    clear: () => $("s3-list").replaceChildren(),
  },
  sg: {
    path: "/api/security-groups",
    statusId: "sg-status",
    countId: "count-sg",
    containerId: "sg-list",
    loadingMessage: "보안 그룹을 조회하는 중…",
    emptyMessage: "이 리전에 보안 그룹이 없습니다.",
    store: () => {},
    render: renderSecurityGroups,
    clear: () => $("sg-list").replaceChildren(),
  },
};

// EC2 표와 이력 표는 서로의 데이터를 참조하므로 한쪽이 갱신되면 다른 쪽도 다시 그린다.
function rerenderIfShown(name) {
  if (!$(LISTS[name].containerId).hidden) {
    if (name === "ec2") renderEc2();
    else renderActions();
  }
}

async function loadList(name, sid) {
  const list = LISTS[name];
  const status = $(list.statusId);
  const container = $(list.containerId);
  container.hidden = true;
  setStatus(status, "loading", list.loadingMessage);
  setCount(list.countId, "loading");
  try {
    const data = await api(list.path);
    if (sid !== sessionId) return;
    setCount(list.countId, "ok", data.count);
    list.store(data.items);
    if (data.items.length === 0) {
      list.clear();
      setStatus(status, "empty", list.emptyMessage);
      return;
    }
    container.hidden = false;
    list.render(data.items);
    setStatus(status, null);
  } catch (err) {
    if (sid !== sessionId) return;
    if (handleAuthError(err)) return;
    list.clear();
    setCount(list.countId, "error");
    setStatus(status, "error", err.message, err.code);
  }
}

// ---------- EC2 ----------

const ACTION_LABEL = { start: "시작", stop: "중지" };
const REQUIRED_STATE = { start: "stopped", stop: "running" };

function unresolvedInstances() {
  return new Set(actionItems.filter((a) => a.status === "REQUESTED" || a.status === "UNKNOWN").map((a) => a.instance_id));
}

function disabledReason(inst, action, unresolved) {
  if (controlBusy) return "다른 제어 요청을 처리하는 중입니다.";
  if (!inst.controllable) return "서버 허용 목록에 없는 인스턴스입니다.";
  if (unresolved.has(inst.instance_id)) return "결과가 확정되지 않은 이력(REQUESTED/UNKNOWN)이 있습니다.";
  if (inst.state !== REQUIRED_STATE[action]) return `${REQUIRED_STATE[action]} 상태에서만 가능합니다. 현재: ${inst.state}`;
  return null;
}

function renderEc2() {
  const unresolved = unresolvedInstances();
  const tbody = $("ec2-table").querySelector("tbody");
  tbody.replaceChildren(
    ...ec2Items.map((inst) => {
      const tr = el("tr");
      const stateCell = el("td");
      stateCell.append(el("span", inst.state ?? "-", `badge state-${inst.state ?? "unknown"}`));
      const ctrlCell = el("td");
      ctrlCell.append(inst.controllable ? el("span", "허용", "badge allowed") : el("span", "조회만", "badge"));

      const actionCell = el("td", null, "actions-cell");
      for (const action of ["start", "stop"]) {
        const button = el("button", ACTION_LABEL[action], action === "stop" ? "danger" : "primary");
        button.type = "button";
        const reason = disabledReason(inst, action, unresolved);
        button.disabled = reason !== null;
        if (reason) button.title = reason;
        button.addEventListener("click", () => confirmAndSend(inst, action));
        actionCell.append(button);
      }
      if (lostRequests.has(inst.instance_id)) actionCell.append(el("span", "응답 미확인", "badge state-pending"));

      tr.append(
        el("td", inst.name),
        el("td", inst.instance_id, "mono"),
        stateCell,
        el("td", inst.instance_type ?? "-"),
        el("td", inst.private_ip ?? "-", "mono"),
        el("td", inst.public_ip ?? "-", "mono"),
        ctrlCell,
        actionCell,
      );
      return tr;
    }),
  );
}

// ---------- 제어 이력 ----------

function renderActions() {
  const states = new Map(ec2Items.map((i) => [i.instance_id, i]));
  const tbody = $("actions-table").querySelector("tbody");
  tbody.replaceChildren(
    ...actionItems.map((a) => {
      const tr = el("tr");
      const statusCell = el("td");
      statusCell.append(el("span", a.status, `badge action-${a.status}`));
      const inst = states.get(a.instance_id);
      const currentCell = el("td");
      currentCell.append(
        inst ? el("span", inst.state, `badge state-${inst.state}`) : el("span", "목록에 없음", "muted"),
      );
      const instCell = el("td");
      instCell.append(el("div", inst?.name ?? a.instance_id), el("div", a.instance_id, "mono muted small"));
      tr.append(
        el("td", formatTime(a.created_at)),
        instCell,
        el("td", ACTION_LABEL[a.action] ?? a.action),
        statusCell,
        currentCell,
        el("td", a.message ?? "-", "wrap"),
        el("td", a.request_id, "mono small"),
      );
      return tr;
    }),
  );
}

// ---------- 제어 요청 ----------

function askConfirm(inst, action, isResend) {
  const dialog = $("confirm-dialog");
  $("confirm-account").textContent = currentContext.account_id;
  $("confirm-region").textContent = currentContext.region;
  $("confirm-name").textContent = inst.name;
  $("confirm-id").textContent = inst.instance_id;
  $("confirm-action").textContent = `${ACTION_LABEL[action]} (${action})`;
  $("confirm-resend").hidden = !isResend;
  dialog.returnValue = "";
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "approve"), { once: true });
    dialog.showModal();
  });
}

function showControlNotice(kind, message, code, lost) {
  const notice = $("control-notice");
  setStatus(notice, kind, message, code);
  if (lost) {
    const button = el("button", "같은 요청 다시 보내기", "secondary");
    button.type = "button";
    button.disabled = controlBusy;
    button.addEventListener("click", () => {
      const inst = ec2Items.find((i) => i.instance_id === lost.instanceId) ?? { instance_id: lost.instanceId, name: lost.name };
      confirmAndSend(inst, lost.action);
    });
    notice.append(button);
  }
}

async function confirmAndSend(inst, action) {
  if (controlBusy) return;
  if (!currentContext) {
    showControlNotice("error", "계정 정보를 확인하지 못해 제어할 수 없습니다. 먼저 조회하세요.");
    return;
  }

  const lost = lostRequests.get(inst.instance_id);
  const reuse = lost && lost.action === action ? lost : null;
  // 확인창을 띄우는 동안 다른 클릭을 막는다.
  controlBusy = true;
  renderEc2();
  const approved = await askConfirm(inst, action, Boolean(reuse));
  if (!approved) {
    // 취소하면 POST를 보내지 않는다.
    controlBusy = false;
    renderEc2();
    return;
  }

  // 승인할 때 한 번만 키를 만든다. 응답을 못 받은 같은 요청이면 기존 키를 그대로 쓴다.
  const requestId = reuse ? reuse.requestId : crypto.randomUUID();
  const sid = sessionId;
  showControlNotice("loading", `${inst.name} ${ACTION_LABEL[action]} 요청을 보내는 중…`);

  try {
    const result = await api(`/api/ec2/${encodeURIComponent(inst.instance_id)}/actions`, {
      method: "POST",
      body: { action, request_id: requestId },
    });
    if (sid !== sessionId) return;
    lostRequests.delete(inst.instance_id);
    const kind = result.status === "ACCEPTED" ? "success" : "error";
    showControlNotice(kind, `${inst.name} ${ACTION_LABEL[action]}: ${result.status}`, result.request_id);
  } catch (err) {
    if (sid !== sessionId) return;
    if (handleAuthError(err)) return;
    if (err.status === 0) {
      // 응답 유실: 키를 보관하고 자동으로 다시 보내지 않는다.
      const entry = { instanceId: inst.instance_id, action, requestId, name: inst.name };
      lostRequests.set(inst.instance_id, entry);
      controlBusy = false;
      showControlNotice("error", `${err.message} 처리 여부가 확인되지 않았습니다. 다시 보낼 때는 같은 request_id를 사용합니다.`, err.code, entry);
      renderEc2();
      return;
    }
    lostRequests.delete(inst.instance_id);
    showControlNotice("error", err.message, err.code);
  } finally {
    if (sid === sessionId) controlBusy = false;
  }

  // 결과 확인을 위해 한 번만 다시 조회한다. 반복 폴링은 하지 않는다.
  await Promise.allSettled([loadList("ec2", sid), loadList("actions", sid)]);
}

// ---------- S3 ----------

function renderS3(items) {
  $("s3-list").replaceChildren(...items.map((bucket) => el("li", bucket.name, "mono")));
}

// ---------- 보안 그룹 ----------

function formatProtocol(proto) {
  return proto === "-1" ? "전체" : String(proto).toUpperCase();
}

function formatPorts(rule) {
  if (rule.IpProtocol === "-1" || rule.FromPort === undefined) return "전체";
  if (rule.FromPort === -1) return "전체";
  return rule.FromPort === rule.ToPort ? String(rule.FromPort) : `${rule.FromPort}-${rule.ToPort}`;
}

function ruleSources(rule) {
  const sources = [];
  for (const r of rule.IpRanges ?? []) sources.push(r.CidrIp);
  for (const r of rule.Ipv6Ranges ?? []) sources.push(r.CidrIpv6);
  for (const r of rule.UserIdGroupPairs ?? []) sources.push(r.GroupId);
  for (const r of rule.PrefixListIds ?? []) sources.push(r.PrefixListId);
  return sources.length ? sources : ["-"];
}

function renderSecurityGroups(items) {
  $("sg-list").replaceChildren(
    ...items.map((sg) => {
      const box = el("article", null, "sg");
      const head = el("header", null, "sg-head");
      head.append(el("strong", sg.GroupName), el("span", sg.GroupId, "mono muted"));
      box.append(head);

      const rules = sg.IpPermissions ?? [];
      if (rules.length === 0) {
        box.append(el("p", "인바운드 규칙 없음", "muted"));
        return box;
      }

      const table = el("table");
      const thead = el("thead");
      const hr = el("tr");
      hr.append(el("th", "프로토콜"), el("th", "포트"), el("th", "소스"));
      thead.append(hr);
      const tbody = el("tbody");
      for (const rule of rules) {
        for (const source of ruleSources(rule)) {
          const tr = el("tr");
          tr.append(
            el("td", formatProtocol(rule.IpProtocol)),
            el("td", formatPorts(rule), "mono"),
            el("td", source, "mono"),
          );
          tbody.append(tr);
        }
      }
      table.append(thead, tbody);
      const wrap = el("div", null, "table-wrap");
      wrap.append(table);
      box.append(wrap);
      return box;
    }),
  );
}

// ---------- 조회 ----------

let refreshing = false;

function setRefreshBusy(busy) {
  refreshing = busy;
  for (const id of ["refresh-btn", "ec2-refresh-btn"]) $(id).disabled = busy;
  $("refresh-btn").textContent = busy ? "조회 중…" : "조회";
}

function stampUpdated() {
  $("last-updated").textContent = new Date().toLocaleString("ko-KR");
}

async function runRefresh(tasks) {
  if (refreshing || !appToken) return;
  const sid = sessionId;
  setRefreshBusy(true);
  await Promise.allSettled(tasks(sid));
  if (sid !== sessionId) return;
  stampUpdated();
  setRefreshBusy(false);
}

const refreshAll = () =>
  runRefresh((sid) => [loadContext(sid), ...Object.keys(LISTS).map((name) => loadList(name, sid))]);
const refreshControl = () => runRefresh((sid) => [loadList("ec2", sid), loadList("actions", sid)]);

$("refresh-btn").addEventListener("click", refreshAll);
$("ec2-refresh-btn").addEventListener("click", refreshControl);

// ---------- 연결·로그아웃 ----------

function clearData() {
  for (const list of Object.values(LISTS)) {
    list.clear();
    $(list.containerId).hidden = true;
    setStatus($(list.statusId), null);
    setCount(list.countId, "reset");
  }
  currentContext = null;
  lostRequests.clear();
  controlBusy = false;
  setStatus($("control-notice"), null);
  $("ctx-account").textContent = "";
  $("ctx-region").textContent = "";
  $("ctx-arn").textContent = "";
  $("context-info").hidden = true;
  setStatus($("context-status"), null);
  $("last-updated").textContent = "아직 조회하지 않음";
  const dialog = $("confirm-dialog");
  if (dialog.open) dialog.close();
}

function showLogin(message, code) {
  sessionId += 1;
  appToken = null;
  clearData();
  setRefreshBusy(false);
  $("app-view").hidden = true;
  $("session-actions").hidden = true;
  $("login-view").hidden = false;
  $("connect-btn").disabled = false;
  $("token-input").value = "";
  if (message) setStatus($("login-status"), "error", message, code);
  else setStatus($("login-status"), null);
  $("token-input").focus();
}

// 앱 토큰이 거절되면(401) 입력 화면으로 돌아간다. AWS 로그인 만료(503)는 화면 안에서 오류로 보여준다.
function handleAuthError(err) {
  if (err.status !== 401) return false;
  showLogin(err.message, err.code);
  return true;
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("token-input");
  const token = input.value.trim();
  if (!token) {
    setStatus($("login-status"), "error", "앱 토큰을 입력하세요.");
    return;
  }

  sessionId += 1;
  const sid = sessionId;
  appToken = token;
  input.value = "";
  $("connect-btn").disabled = true;
  setStatus($("login-status"), "loading", "토큰과 AWS 계정을 확인하는 중…");

  let ctx;
  try {
    ctx = await api("/api/context");
  } catch (err) {
    if (sid !== sessionId) return;
    appToken = null;
    $("connect-btn").disabled = false;
    setStatus($("login-status"), "error", err.message, err.code);
    input.focus();
    return;
  }
  if (sid !== sessionId) return;

  setStatus($("login-status"), null);
  $("login-view").hidden = true;
  $("app-view").hidden = false;
  $("session-actions").hidden = false;
  selectTab("dashboard");
  renderContext(ctx);

  // 검증에 성공했으므로 나머지 목록만 조회한다.
  setRefreshBusy(true);
  await Promise.allSettled(Object.keys(LISTS).map((name) => loadList(name, sid)));
  if (sid !== sessionId) return;
  stampUpdated();
  setRefreshBusy(false);
});

$("logout-btn").addEventListener("click", () => showLogin());

showLogin();
