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

// 조회 결과(메모리). state: idle(조회 전) / loading / ok / error. 로그아웃 시 모두 지운다.
const RESOURCE_NAMES = ["context", "ec2", "s3", "sg", "actions", "check"];
const resources = {};

function resetResources() {
  for (const name of RESOURCE_NAMES) resources[name] = { state: "idle", data: null, error: null };
}
resetResources();

// 응답을 받지 못한 제어 요청: instance_id → { instanceId, action, requestId, name }. 같은 키를 재사용한다.
const lostRequests = new Map();
// 제어를 보낸 뒤 아직 EC2를 새로고침하지 않은 인스턴스. 표의 상태가 오래된 값이므로 버튼을 막는다.
const staleInstances = new Set();
let controlBusy = false;

/**
 * 모든 API 요청은 이 함수를 거친다.
 * - Authorization 헤더에 메모리의 앱 토큰을 붙인다.
 * - 401(앱 토큰 거절)이면 토큰 입력 화면으로 돌아간다.
 * - 503 AWS_LOGIN_REQUIRED(AWS 로그인 만료)면 앱 안에 안내 배너를 띄운다. 앱 토큰은 유지한다.
 * - 실패는 항상 ApiError로 던진다. 호출한 쪽은 이를 '0개'나 '발견 없음'으로 바꾸지 않는다.
 */
async function api(path, { method = "GET", body } = {}) {
  const sid = sessionId;
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
    const err = new ApiError(
      response.status,
      detail?.code || (response.status === 422 ? "VALIDATION_ERROR" : "HTTP_ERROR"),
      detail?.message || `요청이 실패했습니다. (HTTP ${response.status})`,
    );
    if (sid === sessionId) {
      if (err.status === 401) showLogin(err.message, err.code);
      else if (err.code === "AWS_LOGIN_REQUIRED") $("aws-login-banner").hidden = false;
    }
    throw err;
  }
  if (!data) throw new ApiError(response.status, "INVALID_RESPONSE", "서버 응답을 해석하지 못했습니다.");
  // AWS를 거치는 요청이 성공했으면 AWS 로그인은 유효하다.
  if (sid === sessionId && path !== "/api/actions") $("aws-login-banner").hidden = true;
  return data;
}

// 상태 영역: loading / empty / error / success / 숨김
function setStatus(statusEl, kind, message, code) {
  statusEl.replaceChildren();
  statusEl.className = "status";
  if (!kind) return;
  statusEl.classList.add(`status-${kind}`);
  statusEl.append(el("span", message));
  if (code) statusEl.append(el("span", code, "status-code"));
}

function formatTime(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? String(iso) : date.toLocaleString("ko-KR");
}

function stateBadge(state) {
  return el("span", state ?? "-", `badge state-${state ?? "unknown"}`);
}

function cell(...children) {
  const td = el("td");
  td.append(...children);
  return td;
}

/**
 * 목록형 자원의 공통 상태 표시. 데이터가 있어 표를 그려야 하면 true.
 * 조회 전·로딩·실패·빈 목록을 서로 다른 문구로 보여 준다.
 */
function showListState(res, statusEl, container, { loading, empty }) {
  container.hidden = true;
  if (res.state === "idle") setStatus(statusEl, null);
  else if (res.state === "loading") setStatus(statusEl, "loading", loading);
  else if (res.state === "error") setStatus(statusEl, "error", res.error.message, res.error.code);
  else if (res.data.items.length === 0) setStatus(statusEl, "empty", empty);
  else {
    setStatus(statusEl, null);
    container.hidden = false;
    return true;
  }
  return false;
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

function renderContext() {
  const res = resources.context;
  const info = $("context-info");
  info.hidden = true;
  if (res.state === "loading") setStatus($("context-status"), "loading", "계정 정보를 조회하는 중…");
  else if (res.state === "error") setStatus($("context-status"), "error", res.error.message, res.error.code);
  else setStatus($("context-status"), null);
  if (res.state !== "ok") return;
  $("ctx-account").textContent = res.data.account_id;
  $("ctx-region").textContent = res.data.region;
  $("ctx-arn").textContent = res.data.caller_arn;
  info.hidden = false;
}

function unresolvedActions() {
  const res = resources.actions;
  if (res.state !== "ok") return null;
  return res.data.items.filter((a) => a.status === "REQUESTED" || a.status === "UNKNOWN");
}

// 요약 숫자는 실제 조회 결과로만 계산한다. 실패하면 '확인 불가'.
function summaryValue(res, compute) {
  if (res.state === "idle") return { value: "-", sub: "조회 전" };
  if (res.state === "loading") return { value: "…", sub: "조회 중" };
  if (res.state === "error") return { value: "확인 불가", sub: res.error.code, error: true };
  return compute(res.data);
}

function renderSummary() {
  const ec2Count = (state) => (data) => ({ value: data.items.filter((i) => i.state === state).length });
  const cards = [
    ["EC2 전체", summaryValue(resources.ec2, (d) => ({ value: d.count }))],
    ["실행 중 (running)", summaryValue(resources.ec2, ec2Count("running"))],
    ["중지됨 (stopped)", summaryValue(resources.ec2, ec2Count("stopped"))],
    ["S3 버킷", summaryValue(resources.s3, (d) => ({ value: d.count }))],
    ["보안 그룹 (리전 전체)", summaryValue(resources.sg, (d) => ({ value: d.count }))],
    [
      "미확정 제어 이력",
      summaryValue(resources.actions, () => ({ value: unresolvedActions().length, sub: "최근 50건 중 REQUESTED·UNKNOWN" })),
    ],
    [
      "보안 점검 검토 필요",
      resources.check.state === "idle"
        ? { value: "-", sub: "점검 실행 전" }
        : summaryValue(resources.check, (d) => ({ value: d.finding_count, sub: `점검 ${formatTime(d.checked_at)}` })),
    ],
  ];
  $("summary-cards").replaceChildren(
    ...cards.map(([label, v]) => {
      const card = el("div", null, "card");
      card.append(el("span", label, "card-label"), el("span", v.value, v.error ? "card-value card-error" : "card-value"));
      if (v.sub) card.append(el("span", v.sub, "card-sub"));
      return card;
    }),
  );
}

// ---------- EC2 ----------

const ACTION_LABEL = { start: "시작", stop: "중지" };
const REQUIRED_STATE = { start: "stopped", stop: "running" };

function disabledReason(inst, action, unresolvedIds) {
  if (controlBusy) return "다른 제어 요청을 처리하는 중입니다.";
  if (!inst.controllable) return "서버 허용 목록에 없는 인스턴스입니다.";
  if (staleInstances.has(inst.instance_id)) return "제어 요청 후 아직 새로고침하지 않아 현재 상태를 알 수 없습니다.";
  if (unresolvedIds.has(inst.instance_id)) return "결과가 확정되지 않은 이력(REQUESTED/UNKNOWN)이 있습니다.";
  if (inst.state !== REQUIRED_STATE[action]) return `${REQUIRED_STATE[action]} 상태에서만 가능합니다. 현재: ${inst.state}`;
  return null;
}

function renderEc2() {
  const res = resources.ec2;
  const table = $("ec2-table");
  const filter = $("ec2-filter").value;
  $("ec2-filter-info").textContent = "";
  if (!showListState(res, $("ec2-status"), table, { loading: "EC2 인스턴스를 조회하는 중…", empty: "이 리전에 EC2 인스턴스가 없습니다." })) {
    table.querySelector("tbody").replaceChildren();
    return;
  }

  // 필터는 이미 조회한 목록에만 적용한다. 서버를 다시 호출하지 않는다.
  const all = res.data.items;
  const items = filter ? all.filter((i) => i.state === filter) : all;
  $("ec2-filter-info").textContent = `전체 ${all.length}대 중 ${items.length}대 표시`;
  if (items.length === 0) {
    table.hidden = true;
    setStatus($("ec2-status"), "empty", `상태가 ${filter}인 인스턴스가 없습니다. (조회된 전체 ${all.length}대)`);
    return;
  }

  const unresolvedIds = new Set((unresolvedActions() ?? []).map((a) => a.instance_id));
  table.querySelector("tbody").replaceChildren(
    ...items.map((inst) => {
      const actionCell = el("td", null, "actions-cell");
      for (const action of ["start", "stop"]) {
        const button = el("button", ACTION_LABEL[action], action === "stop" ? "danger" : "primary");
        button.type = "button";
        const reason = disabledReason(inst, action, unresolvedIds);
        button.disabled = reason !== null;
        if (reason) button.title = reason;
        button.addEventListener("click", () => confirmAndSend(inst, action));
        actionCell.append(button);
      }
      if (staleInstances.has(inst.instance_id)) actionCell.append(el("span", "새로고침 필요", "badge state-pending"));
      if (lostRequests.has(inst.instance_id)) actionCell.append(el("span", "응답 미확인", "badge action-UNKNOWN"));

      const tr = el("tr");
      tr.append(
        el("td", inst.name),
        el("td", inst.instance_id, "mono"),
        cell(stateBadge(inst.state)),
        el("td", inst.instance_type ?? "-"),
        el("td", inst.private_ip ?? "-", "mono"),
        el("td", inst.public_ip ?? "-", "mono"),
        cell(inst.controllable ? el("span", "허용", "badge allowed") : el("span", "조회만", "badge")),
        actionCell,
      );
      return tr;
    }),
  );
}

$("ec2-filter").addEventListener("change", renderEc2);

// ---------- 실행 이력 ----------

function renderActions() {
  const res = resources.actions;
  const table = $("actions-table");
  if (!showListState(res, $("actions-status"), table, { loading: "실행 이력을 조회하는 중…", empty: "실행 이력이 없습니다." })) {
    table.querySelector("tbody").replaceChildren();
    return;
  }
  const ec2 = resources.ec2;
  const states = ec2.state === "ok" ? new Map(ec2.data.items.map((i) => [i.instance_id, i])) : null;

  table.querySelector("tbody").replaceChildren(
    ...res.data.items.map((a) => {
      const inst = states?.get(a.instance_id);
      let current;
      if (inst) current = stateBadge(inst.state);
      else if (ec2.state === "ok") current = el("span", "목록에 없음", "muted");
      else if (ec2.state === "error") current = el("span", "확인 불가", "muted");
      else current = el("span", "EC2 조회 전", "muted");

      const instCell = el("td");
      instCell.append(el("div", inst?.name ?? a.instance_id), el("div", a.instance_id, "mono muted small"));
      const tr = el("tr");
      tr.append(
        el("td", formatTime(a.created_at)),
        instCell,
        el("td", ACTION_LABEL[a.action] ?? a.action),
        cell(el("span", a.status, `badge action-${a.status}`)),
        cell(current),
        el("td", a.message ?? "-", "wrap"),
        el("td", a.aws_request_id ?? "-", "mono small"),
        el("td", a.request_id, "mono small"),
      );
      return tr;
    }),
  );
}

// ---------- EC2 제어 ----------

const ACCEPTED_NOTICE = "AWS 요청 접수 — 현재 상태는 새로고침으로 확인";

function askConfirm(inst, action, isResend) {
  const dialog = $("confirm-dialog");
  const ctx = resources.context.data;
  $("confirm-account").textContent = ctx.account_id;
  $("confirm-region").textContent = ctx.region;
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
    button.addEventListener("click", () => {
      const items = resources.ec2.data?.items ?? [];
      const inst = items.find((i) => i.instance_id === lost.instanceId) ?? { instance_id: lost.instanceId, name: lost.name };
      confirmAndSend(inst, lost.action);
    });
    notice.append(button);
  }
}

function replayNotice(inst, action, result) {
  const label = `${inst.name} ${ACTION_LABEL[action]}`;
  if (result.status === "ACCEPTED") return ["success", `${label}: 이미 접수된 요청입니다. ${ACCEPTED_NOTICE}`];
  if (result.status === "FAILED") return ["error", `${label}: 이 요청은 실패(FAILED)로 기록되어 있습니다.`];
  return ["error", `${label}: 결과가 확정되지 않았습니다(${result.status}). AWS 콘솔에서 실제 상태를 확인하세요.`];
}

async function confirmAndSend(inst, action) {
  if (controlBusy) return;
  if (resources.context.state !== "ok") {
    showControlNotice("error", "계정 정보를 확인하지 못해 제어할 수 없습니다. 먼저 '전체 조회'를 하세요.");
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
    staleInstances.add(inst.instance_id);
    if (result.status === "ACCEPTED" && !reuse && result.request_id === requestId) {
      showControlNotice("success", `${inst.name} ${ACTION_LABEL[action]}: ${ACCEPTED_NOTICE}`, result.request_id);
    } else {
      const [kind, message] = replayNotice(inst, action, result);
      showControlNotice(kind, message, result.request_id);
    }
  } catch (err) {
    if (sid !== sessionId) return; // 401이면 api()가 이미 입력 화면으로 돌려보냈다.
    if (err.status === 0) {
      // 응답 유실: 키를 보관하고 자동으로 다시 보내지 않는다.
      const entry = { instanceId: inst.instance_id, action, requestId, name: inst.name };
      lostRequests.set(inst.instance_id, entry);
      showControlNotice("error", `${err.message} 처리 여부가 확인되지 않았습니다. 다시 보낼 때는 같은 request_id를 사용합니다.`, err.code, entry);
    } else {
      lostRequests.delete(inst.instance_id);
      // AWS 호출 후 결과가 불확실하거나 기록에 실패했으면 표의 상태를 믿을 수 없다.
      if (err.code === "AWS_RESULT_UNKNOWN" || err.code === "DB_UPDATE_FAILED") staleInstances.add(inst.instance_id);
      showControlNotice("error", err.message, err.code);
    }
  } finally {
    if (sid === sessionId) {
      controlBusy = false;
      renderEc2();
      renderSummary();
    }
  }
}

// ---------- S3 ----------

function renderS3() {
  const list = $("s3-list");
  if (!showListState(resources.s3, $("s3-status"), list, { loading: "S3 버킷을 조회하는 중…", empty: "이 계정에 S3 버킷이 없습니다." })) {
    list.replaceChildren();
    return;
  }
  list.replaceChildren(...resources.s3.data.items.map((bucket) => el("li", bucket.name, "mono")));
}

// ---------- 보안 그룹 ----------

function formatProtocol(proto) {
  if (proto === "-1") return "전체 프로토콜";
  if (proto === "6") return "TCP";
  if (proto === "17") return "UDP";
  return String(proto).toUpperCase();
}

function formatPorts(rule) {
  const proto = String(rule.IpProtocol);
  if (proto === "-1" || rule.FromPort === undefined) return "전체";
  // ICMP의 숫자는 포트가 아니라 type/code다.
  if (proto === "icmp" || proto === "1" || proto === "icmpv6" || proto === "58") {
    return rule.FromPort === -1 ? "전체 type" : `type ${rule.FromPort}${rule.ToPort === -1 ? "" : `, code ${rule.ToPort}`}`;
  }
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

function renderSecurityGroups() {
  const list = $("sg-list");
  if (!showListState(resources.sg, $("sg-status"), list, { loading: "보안 그룹을 조회하는 중…", empty: "이 리전에 보안 그룹이 없습니다." })) {
    list.replaceChildren();
    return;
  }
  list.replaceChildren(
    ...resources.sg.data.items.map((sg) => {
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
      hr.append(el("th", "프로토콜"), el("th", "포트 범위"), el("th", "소스"));
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

// ---------- 보안 점검 ----------

const CHECK_RESULT_TEXT = "공개 관리 포트 규칙: 검토 필요";

function formatCheckRange(finding) {
  if (finding.protocol === "-1" || finding.from_port === null || finding.from_port === undefined) return "전체";
  return finding.from_port === finding.to_port ? String(finding.from_port) : `${finding.from_port}-${finding.to_port}`;
}

function connectionCells(c) {
  return [el("td", c.instance_name), el("td", c.instance_id, "mono"), cell(stateBadge(c.state)), el("td", c.network_interface_id, "mono")];
}

function renderCheck() {
  const res = resources.check;
  const findingsTable = $("check-findings");
  const groupsTable = $("check-groups");
  $("check-result").hidden = true;
  findingsTable.hidden = true;
  groupsTable.hidden = true;
  findingsTable.querySelector("tbody").replaceChildren();
  groupsTable.querySelector("tbody").replaceChildren();
  setStatus($("check-summary"), null);

  if (res.state === "idle") return setStatus($("check-status"), null);
  if (res.state === "loading") return setStatus($("check-status"), "loading", "AWS에서 EC2와 연결 보안 그룹을 새로 조회하는 중…");
  if (res.state === "error") {
    // 실패는 '발견 없음'이 아니다. 결과 영역은 비운 채 오류만 보여 준다.
    return setStatus($("check-status"), "error", `점검 실패: ${res.error.message} 점검 결과가 없습니다.`, res.error.code);
  }
  setStatus($("check-status"), null);

  const result = res.data;
  $("check-time").textContent = `${formatTime(result.checked_at)} (UTC ${result.checked_at})`;
  $("check-scope").textContent =
    `EC2 ${result.checked_instance_count}대 · 연결 보안 그룹 ${result.checked_group_count}개 · 검토 필요 규칙 ${result.finding_count}건`;

  // 검토 필요 규칙: 연결 대상(인스턴스·ENI)마다 한 줄로 펼친다. 건수(finding_count)는 규칙 단위 그대로다.
  const findingRows = [];
  for (const f of result.findings) {
    for (const c of f.connections.length ? f.connections : [{}]) {
      const tr = el("tr");
      tr.append(
        cell(el("span", f.result ?? CHECK_RESULT_TEXT, "badge action-FAILED")),
        ...connectionCells(c),
        el("td", f.group_id, "mono"),
        el("td", f.group_name),
        el("td", f.ip_family),
        el("td", f.cidr, "mono"),
        el("td", formatProtocol(f.protocol)),
        el("td", formatCheckRange(f), "mono"),
        el("td", f.management_ports.join(", "), "mono"),
      );
      findingRows.push(tr);
    }
  }
  findingsTable.querySelector("tbody").replaceChildren(...findingRows);

  const perGroup = new Map();
  for (const f of result.findings) perGroup.set(f.group_id, (perGroup.get(f.group_id) ?? 0) + 1);
  const groupRows = [];
  for (const g of result.checked_groups) {
    for (const c of g.connections) {
      const count = perGroup.get(g.group_id) ?? 0;
      const tr = el("tr");
      tr.append(el("td", g.group_id, "mono"), el("td", g.group_name), el("td", count ? `${count}건` : "없음"), ...connectionCells(c));
      groupRows.push(tr);
    }
  }
  groupsTable.querySelector("tbody").replaceChildren(...groupRows);

  if (result.checked_group_count === 0) {
    setStatus($("check-summary"), "empty", "점검 대상 EC2 연결 보안 그룹 없음");
  } else if (result.finding_count === 0) {
    setStatus($("check-summary"), "success", "이번 점검 조건에 해당하는 규칙 없음");
  } else {
    setStatus($("check-summary"), "error", `${CHECK_RESULT_TEXT} ${result.finding_count}건`);
  }
  findingsTable.hidden = result.finding_count === 0;
  groupsTable.hidden = result.checked_group_count === 0;
  $("check-result").hidden = false;
}

async function runSecurityCheck() {
  if (resources.check.state === "loading" || !appToken) return;
  const sid = sessionId;
  const button = $("check-run-btn");
  button.disabled = true;
  button.textContent = "점검 중…";
  // 이전 결과를 지워 새 점검 결과로 오인하지 않게 한다.
  resources.check = { state: "loading", data: null, error: null };
  renderCheck();
  renderSummary();
  try {
    const data = await api("/api/security-checks");
    if (sid !== sessionId) return;
    resources.check = { state: "ok", data, error: null };
  } catch (err) {
    if (sid !== sessionId) return;
    resources.check = { state: "error", data: null, error: err };
  }
  button.disabled = false;
  button.textContent = "점검 실행";
  renderCheck();
  renderSummary();
}

$("check-run-btn").addEventListener("click", runSecurityCheck);

// ---------- 조회 ----------

const PATHS = { context: "/api/context", ec2: "/api/ec2", s3: "/api/s3", sg: "/api/security-groups", actions: "/api/actions" };

function renderAll() {
  renderContext();
  renderSummary();
  renderEc2();
  renderActions();
  renderS3();
  renderSecurityGroups();
  renderCheck();
}

async function load(name, sid) {
  resources[name] = { state: "loading", data: null, error: null };
  renderAll();
  try {
    const data = await api(PATHS[name]);
    if (sid !== sessionId) return;
    resources[name] = { state: "ok", data, error: null };
    // 새 EC2 목록을 받았으므로 '새로고침 필요' 표시를 지운다.
    if (name === "ec2") staleInstances.clear();
  } catch (err) {
    if (sid !== sessionId) return;
    resources[name] = { state: "error", data: null, error: err };
  }
  renderAll();
}

let refreshing = false;
const REFRESH_BUTTONS = ["refresh-btn", "ec2-refresh-btn", "actions-refresh-btn"];

async function runRefresh(names, { stamp = false } = {}) {
  if (refreshing || !appToken) return;
  const sid = sessionId;
  refreshing = true;
  for (const id of REFRESH_BUTTONS) $(id).disabled = true;
  await Promise.allSettled(names.map((name) => load(name, sid)));
  if (sid !== sessionId) return;
  if (stamp) $("last-updated").textContent = new Date().toLocaleString("ko-KR");
  refreshing = false;
  for (const id of REFRESH_BUTTONS) $(id).disabled = false;
}

$("refresh-btn").addEventListener("click", () => runRefresh(["context", "ec2", "s3", "sg", "actions"], { stamp: true }));
// 실행 이력과 EC2 버튼은 서로를 참조하므로(현재 상태, 미확정 이력) 함께 새로고침한다.
$("ec2-refresh-btn").addEventListener("click", () => runRefresh(["ec2", "actions"]));
$("actions-refresh-btn").addEventListener("click", () => runRefresh(["actions", "ec2"]));

// ---------- 연결·로그아웃 ----------

function clearData() {
  resetResources();
  lostRequests.clear();
  staleInstances.clear();
  controlBusy = false;
  refreshing = false;
  for (const id of REFRESH_BUTTONS) $(id).disabled = false;
  $("check-run-btn").disabled = false;
  $("check-run-btn").textContent = "점검 실행";
  $("ec2-filter").value = "";
  $("aws-login-banner").hidden = true;
  setStatus($("control-notice"), null);
  for (const id of ["ctx-account", "ctx-region", "ctx-arn"]) $(id).textContent = "";
  $("last-updated").textContent = "아직 조회하지 않음";
  const dialog = $("confirm-dialog");
  if (dialog.open) dialog.close();
  renderAll();
}

function showLogin(message, code) {
  sessionId += 1;
  appToken = null;
  clearData();
  $("app-view").hidden = true;
  $("session-actions").hidden = true;
  $("login-view").hidden = false;
  $("connect-btn").disabled = false;
  $("token-input").value = "";
  if (message) setStatus($("login-status"), "error", message, code);
  else setStatus($("login-status"), null);
  $("token-input").focus();
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
    // 401이면 api()가 입력 화면에 오류를 이미 표시했다(sessionId도 바뀜).
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
  resources.context = { state: "ok", data: ctx, error: null };
  renderAll();

  // 토큰 검증에 성공했으므로 나머지 목록만 조회한다. 보안 점검은 '점검 실행'을 눌렀을 때만 한다.
  await runRefresh(["ec2", "s3", "sg", "actions"], { stamp: true });
});

$("logout-btn").addEventListener("click", () => showLogin());

showLogin();
