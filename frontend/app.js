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

async function getJson(path) {
  let response;
  try {
    response = await fetch(path, { headers: { Accept: "application/json" } });
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", "서버에 연결하지 못했습니다. 서버가 실행 중인지 확인하세요.");
  }

  let body = null;
  try {
    body = await response.json();
  } catch {
    // JSON이 아닌 응답은 아래에서 처리
  }

  if (!response.ok) {
    const detail = body && typeof body.detail === "object" ? body.detail : null;
    throw new ApiError(
      response.status,
      detail?.code || "HTTP_ERROR",
      detail?.message || `요청이 실패했습니다. (HTTP ${response.status})`,
    );
  }
  if (!body) throw new ApiError(response.status, "INVALID_RESPONSE", "서버 응답을 해석하지 못했습니다.");
  return body;
}

// 상태 영역: loading / empty / error / 숨김(데이터 있음)
function setStatus(statusEl, kind, message, code) {
  statusEl.replaceChildren();
  statusEl.className = "status";
  if (!kind) return;
  statusEl.classList.add(`status-${kind}`);
  statusEl.append(el("span", message));
  if (code) statusEl.append(el("span", code, "status-code"));
}

function setCount(id, state, value) {
  const node = $(id);
  node.className = "card-value";
  if (state === "loading") node.textContent = "…";
  else if (state === "error") {
    node.textContent = "오류";
    node.classList.add("card-error");
  } else node.textContent = String(value);
}

// ---------- 탭 ----------

const tabs = document.querySelectorAll(".tabs [role=tab]");
tabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    tabs.forEach((t) => {
      const selected = t === tab;
      t.setAttribute("aria-selected", String(selected));
      $(`tab-${t.dataset.tab}`).hidden = !selected;
    });
  });
});

// ---------- 대시보드 ----------

async function loadContext() {
  const status = $("context-status");
  const info = $("context-info");
  info.hidden = true;
  setStatus(status, "loading", "계정 정보를 조회하는 중…");
  try {
    const ctx = await getJson("/api/context");
    $("ctx-account").textContent = ctx.account_id;
    $("ctx-region").textContent = ctx.region;
    $("ctx-arn").textContent = ctx.caller_arn;
    setStatus(status, null);
    info.hidden = false;
  } catch (err) {
    setStatus(status, "error", err.message, err.code);
  }
}

// ---------- 목록 공통 ----------

async function loadList({ path, statusId, countId, container, emptyMessage, loadingMessage, render }) {
  const status = $(statusId);
  container.hidden = true;
  setStatus(status, "loading", loadingMessage);
  setCount(countId, "loading");
  try {
    const data = await getJson(path);
    setCount(countId, "ok", data.count);
    if (data.items.length === 0) {
      setStatus(status, "empty", emptyMessage);
      return;
    }
    render(data.items);
    setStatus(status, null);
    container.hidden = false;
  } catch (err) {
    setCount(countId, "error");
    setStatus(status, "error", err.message, err.code);
  }
}

// ---------- EC2 ----------

function renderEc2(items) {
  const tbody = $("ec2-table").querySelector("tbody");
  tbody.replaceChildren(
    ...items.map((inst) => {
      const tr = el("tr");
      const state = el("span", inst.state ?? "-", `badge state-${inst.state ?? "unknown"}`);
      const stateCell = el("td");
      stateCell.append(state);
      tr.append(
        el("td", inst.name),
        el("td", inst.instance_id, "mono"),
        stateCell,
        el("td", inst.instance_type ?? "-"),
        el("td", inst.private_ip ?? "-", "mono"),
        el("td", inst.public_ip ?? "-", "mono"),
      );
      return tr;
    }),
  );
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

async function refreshAll() {
  if (refreshing) return;
  refreshing = true;
  const button = $("refresh-btn");
  button.disabled = true;
  button.textContent = "조회 중…";

  await Promise.allSettled([
    loadContext(),
    loadList({
      path: "/api/ec2",
      statusId: "ec2-status",
      countId: "count-ec2",
      container: $("ec2-table"),
      loadingMessage: "EC2 인스턴스를 조회하는 중…",
      emptyMessage: "이 리전에 EC2 인스턴스가 없습니다.",
      render: renderEc2,
    }),
    loadList({
      path: "/api/s3",
      statusId: "s3-status",
      countId: "count-s3",
      container: $("s3-list"),
      loadingMessage: "S3 버킷을 조회하는 중…",
      emptyMessage: "이 계정에 S3 버킷이 없습니다.",
      render: renderS3,
    }),
    loadList({
      path: "/api/security-groups",
      statusId: "sg-status",
      countId: "count-sg",
      container: $("sg-list"),
      loadingMessage: "보안 그룹을 조회하는 중…",
      emptyMessage: "이 리전에 보안 그룹이 없습니다.",
      render: renderSecurityGroups,
    }),
  ]);

  $("last-updated").textContent = new Date().toLocaleString("ko-KR");
  button.disabled = false;
  button.textContent = "조회";
  refreshing = false;
}

$("refresh-btn").addEventListener("click", refreshAll);
refreshAll();
