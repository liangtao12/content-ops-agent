const state = {
  view: "overview",
  overview: null,
  jobs: [],
  schedules: [],
  health: null,
  filter: "",
  search: "",
  activeJobId: null,
  activeJob: null,
  session: null,
  authBootstrap: null,
  resettingUserId: null,
  members: null,
};

const STATUS = {
  running: { label: "处理中", cls: "info" },
  reviewing: { label: "复审中", cls: "info" },
  awaiting_human: { label: "待审批", cls: "warning" },
  approved: { label: "待排期", cls: "info" },
  completed: { label: "已完成", cls: "success" },
  rejected: { label: "已拒绝", cls: "danger" },
  blocked: { label: "异常", cls: "danger" },
};

const TITLES = {
  overview: "运营概览",
  queue: "审批队列",
  calendar: "发布日程",
  governance: "质量与治理",
  members: "成员管理",
};

function can(permission) {
  return Boolean(state.session?.actor?.permissions?.includes(permission));
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatDate(value, withTime = true) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}),
  }).format(date);
}

function formatFullDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "long",
    day: "numeric",
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function jobTitle(job) {
  return (
    job?.final_content?.title ||
    job?.draft?.title ||
    job?.selected_topic?.title ||
    job?.objective ||
    "未命名内容任务"
  );
}

function statusPill(status) {
  const config = STATUS[status] || { label: status || "未知", cls: "neutral" };
  return `<span class="status-pill ${config.cls}">${escapeHtml(config.label)}</span>`;
}

function riskFor(job) {
  const review = job?.review_result || {};
  const violations = review.violations || [];
  if (violations.length) {
    return {
      cls: review.passed ? "warning" : "danger",
      label: review.passed ? `${violations.length} 项提示` : `${violations.length} 项风险`,
    };
  }
  if (review.passed) return { cls: "success", label: "检查通过" };
  return { cls: "neutral", label: "尚未检查" };
}

function authHeaders() {
  const headers = { "Content-Type": "application/json" };
  const token = localStorage.getItem("contentops_token");
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

// bootstrap 接口不可用时的兜底角色目录（与后端 ROLE_LABELS / ROLE_PERMISSIONS 保持一致）。
const FALLBACK_ROLE_CATALOG = [
  {
    id: "admin",
    label: "管理员",
    description: "拥有全部权限：创建内容、审批编辑、成员管理与系统治理。",
    permissions: ["查看内容", "创建任务", "审批与编辑", "成员与系统治理"],
    self_register: false,
  },
  {
    id: "reviewer",
    label: "内容审核员",
    description: "查看并审批内容，可编辑后重新复审；不能创建任务或管理成员。",
    permissions: ["查看内容", "审批与编辑"],
    self_register: true,
  },
  {
    id: "operator",
    label: "内容运营",
    description: "查看内容并创建任务；不能审批内容或管理成员。",
    permissions: ["查看内容", "创建任务"],
    self_register: true,
  },
  {
    id: "viewer",
    label: "只读观察者",
    description: "仅查看内容与报表，不能创建、审批或管理。",
    permissions: ["查看内容"],
    self_register: true,
  },
];

function getRoleCatalog() {
  const catalog = state.authBootstrap?.role_catalog;
  return Array.isArray(catalog) && catalog.length
    ? catalog
    : FALLBACK_ROLE_CATALOG;
}

function renderRoleCatalog() {
  const select = document.getElementById("registerRole");
  const help = document.getElementById("registerRoleHelp");
  if (!select || !help) return;

  const catalog = getRoleCatalog();
  const staleBackend = !state.authBootstrap?.role_catalog;
  const selectable = catalog.filter((role) => role.self_register);
  const previous = select.value;
  select.innerHTML = selectable
    .map(
      (role) =>
        `<option value="${escapeHtml(role.id)}">${escapeHtml(role.label)}</option>`,
    )
    .join("");
  select.value = selectable.some((role) => role.id === previous)
    ? previous
    : "operator";

  help.innerHTML = (staleBackend
    ? `<p class="role-catalog-notice">后端服务仍是旧版本，角色选择暂不生效；请重启服务后刷新页面。</p>`
    : "") + catalog
    .map(
      (role) => `
      <div class="role-card" data-role-card="${escapeHtml(role.id)}">
        <div class="role-card-head">
          <strong>${escapeHtml(role.label)}</strong>
          <span class="role-card-tag">${role.self_register ? "注册可选" : "仅管理员分配"}</span>
        </div>
        <p>${escapeHtml(role.description)}</p>
        <div class="role-card-perms">${(role.permissions || [])
          .map((permission) => `<span>${escapeHtml(permission)}</span>`)
          .join("")}</div>
      </div>`,
    )
    .join("");

  const highlight = () => {
    help.querySelectorAll("[data-role-card]").forEach((card) => {
      card.classList.toggle("active", card.dataset.roleCard === select.value);
    });
  };
  select.onchange = highlight;
  highlight();
}

const FIELD_LABELS = {
  username: "用户名",
  display_name: "显示名称",
  password: "密码",
  registration_code: "邀请码",
  objective: "运营目标",
  role: "角色",
  active: "账号状态",
};

function readableValidationMessage(item) {
  if (typeof item === "string") return item;
  if (!item || typeof item !== "object") return String(item ?? "请求参数错误");
  const location = Array.isArray(item.loc)
    ? item.loc.filter((part) => part !== "body" && part !== "query")
    : [];
  const field = FIELD_LABELS[location.at(-1)] || location.at(-1) || "请求参数";
  const message = item.msg || item.message || "格式不正确";
  const translations = {
    "Field required": "不能为空",
    "Input should be a valid string": "必须是文本",
    "Input should be a valid boolean": "必须是布尔值",
    "String should have at least 8 characters": "长度至少为 8 位",
    "String should have at most 128 characters": "长度不能超过 128 位",
    "String should have at least 3 characters": "长度至少为 3 位",
    "String should have at most 32 characters": "长度不能超过 32 位",
    "String should have at most 80 characters": "长度不能超过 80 个字符",
  };
  let readable = translations[message] || message;
  if (/at least 8 characters/i.test(readable)) {
    readable = "长度至少为 8 位";
  } else if (/at most 128 characters/i.test(readable)) {
    readable = "长度不能超过 128 位";
  } else if (/at least 3 characters/i.test(readable)) {
    readable = "长度至少为 3 位";
  } else if (/at most 32 characters/i.test(readable)) {
    readable = "长度不能超过 32 位";
  } else if (/at most 80 characters/i.test(readable)) {
    readable = "长度不能超过 80 个字符";
  } else if (/^Value error,\s*/i.test(readable)) {
    readable = readable.replace(/^Value error,\s*/i, "");
  }
  return `${field}：${readable}`;
}

function formatApiError(detail) {
  if (!detail) return "请求失败，请稍后重试";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail.map(readableValidationMessage);
    return [...new Set(messages)].join("；");
  }
  if (typeof detail === "object") {
    if (detail.msg || detail.message) return readableValidationMessage(detail);
    try {
      return JSON.stringify(detail);
    } catch {
      return "请求失败";
    }
  }
  return String(detail);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...authHeaders(),
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      detail = formatApiError(payload.detail ?? payload);
    } catch {
      // Keep the HTTP status when the response is not JSON.
    }
    if (response.status === 401 && !path.startsWith("/api/auth/")) {
      localStorage.removeItem("contentops_token");
      state.session = null;
      showAuth();
    }
    throw new Error(detail);
  }
  return response.json();
}

const RUN_NODE_LABELS = {
  research: "选题研究",
  write: "内容创作",
  review: "合规审核",
  human: "人工确认",
  final_check: "编辑复审",
  schedule: "排期写入",
  analytics: "数据复盘",
};

function nodeSummaryText(node, summary = {}) {
  if (node === "research") {
    return summary.selected_topic
      ? `选中「${summary.selected_topic}」，候选 ${summary.candidates || 0} 个`
      : `生成 ${summary.candidates || 0} 个候选选题`;
  }
  if (node === "write") {
    return summary.title
      ? `初稿「${summary.title}」· ${summary.body_length || 0} 字`
      : "生成初稿";
  }
  if (node === "review") {
    const verdict = summary.passed
      ? "合规通过"
      : `未通过（${summary.violations || 0} 项风险）`;
    return `${verdict} · 第 ${summary.round || 1} 轮`;
  }
  if (node === "final_check") {
    return summary.approved ? "复审通过，已签发审批令牌" : "复审未通过，回到人工确认";
  }
  if (node === "schedule") {
    return summary.schedule_id ? `已排期 ${summary.schedule_id}` : "写入排期表";
  }
  if (node === "analytics") return "已生成复盘数据";
  return "";
}

function resetRunProgress() {
  const box = document.getElementById("runProgress");
  const list = document.getElementById("runProgressList");
  if (!box || !list) return;
  list.innerHTML = "";
  box.classList.remove("hidden");
  markRunProgressHint("正在启动工作流…", "running");
}

function appendRunProgress(node, summary) {
  const list = document.getElementById("runProgressList");
  if (!list) return;
  const item = document.createElement("li");
  item.className = "run-progress-item";
  item.innerHTML = `
    <span class="run-progress-check">✓</span>
    <strong>${escapeHtml(RUN_NODE_LABELS[node] || node)}</strong>
    <small>${escapeHtml(nodeSummaryText(node, summary))}</small>`;
  list.appendChild(item);
  list.scrollTop = list.scrollHeight;
}

function markRunProgressHint(text, state = "") {
  const hint = document.getElementById("runProgressHint");
  if (!hint) return;
  hint.textContent = text;
  hint.className = state;
}

async function streamRun(payload, onEvent) {
  const response = await fetch("/api/run/stream", {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const data = await response.json();
      detail = formatApiError(data.detail ?? data);
    } catch {
      // 非 JSON 错误保留 HTTP 状态
    }
    if (response.status === 401) {
      localStorage.removeItem("contentops_token");
      state.session = null;
      showAuth();
    }
    throw new Error(detail);
  }
  // 环境不支持流式读取时退回同步接口，功能不降级
  if (!response.body) {
    return api("/api/run", { method: "POST", body: JSON.stringify(payload) });
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let final = null;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() || "";
    for (const frame of frames) {
      let event = "message";
      const dataLines = [];
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (!dataLines.length) continue;
      let data = {};
      try {
        data = JSON.parse(dataLines.join("\n"));
      } catch {
        continue;
      }
      if (event === "error") throw new Error(data.message || "流式执行失败");
      if (event === "done") final = data;
      onEvent?.(event, data);
    }
  }
  if (!final) throw new Error("流式执行结束但未收到完成事件，请重试");
  return final;
}

function toast(message, type = "success") {
  const node = document.createElement("div");
  node.className = `toast ${type === "error" ? "error" : ""}`;
  node.textContent = message;
  document.getElementById("toastStack").appendChild(node);
  window.setTimeout(() => node.remove(), 4200);
}

function openModal(id) {
  document.getElementById(id).classList.remove("hidden");
}

function closeModal(id) {
  document.getElementById(id).classList.add("hidden");
}

function showAuth() {
  document.getElementById("appShell").classList.add("hidden");
  document.getElementById("authScreen").classList.remove("hidden");
}

function showApp() {
  document.getElementById("authScreen").classList.add("hidden");
  document.getElementById("appShell").classList.remove("hidden");
}

function readableAuthError(message) {
  const text = typeof message === "string" ? message.trim() : "";
  if (
    !text ||
    text === "[object Object]" ||
    text === "Undefined" ||
    text === "null"
  ) {
    return "登录失败，请检查用户名和密码后重试；若持续出现请联系管理员。";
  }
  return text;
}

function setAuthError(message = "") {
  const element = document.getElementById("authError");
  const text = readableAuthError(message);
  element.textContent = text;
  element.classList.toggle("hidden", !text);
}

function setAuthTab(tab) {
  const firstUser = Boolean(state.authBootstrap?.first_user);
  const activeTab = firstUser ? "register" : tab;
  document.querySelectorAll("[data-auth-tab]").forEach((button) => {
    button.classList.toggle("active", button.dataset.authTab === activeTab);
  });
  document.getElementById("loginForm").classList.toggle(
    "hidden",
    activeTab !== "login",
  );
  document.getElementById("registerForm").classList.toggle(
    "hidden",
    activeTab !== "register",
  );
  document.getElementById("authKicker").textContent =
    firstUser ? "FIRST ADMIN" : activeTab === "login" ? "WELCOME BACK" : "JOIN WORKSPACE";
  document.getElementById("authTitle").textContent =
    firstUser ? "创建管理员账号" : activeTab === "login" ? "登录工作台" : "注册团队账号";
  document.getElementById("authHint").textContent =
    firstUser
      ? "这是系统的第一个账号，将自动获得管理员权限。"
      : activeTab === "login"
        ? "使用你的团队账号继续处理内容任务。"
        : "注册时可自选角色；管理员权限仅由现有管理员分配。";
  document.getElementById("registrationCodeField").classList.toggle(
    "hidden",
    !state.authBootstrap?.registration_code_required,
  );
  // 首个账号固定为管理员，不开放角色选择；其余账号可自选非管理员角色。
  ["registerRoleField", "registerRoleHelp"].forEach((id) => {
    const node = document.getElementById(id);
    if (node) node.classList.toggle("hidden", firstUser);
  });
  document.getElementById("registerButton").textContent =
    firstUser ? "创建管理员账号" : "注册账号";
  document.getElementById("authFootnote").textContent =
    firstUser
      ? "首个账号必须使用真实管理身份，后续账号由管理员管理。"
      : state.authBootstrap?.registration_code_required
        ? "当前工作区需要管理员提供的邀请码。"
        : "下方列出各角色功能；如需管理员权限，请联系现有管理员在成员管理中调整。若已无可用管理员，用后端 `python -m app.admin_cli promote <用户名>` 恢复。";
  setAuthError();
}

async function bootstrapAuth() {
  try {
    state.authBootstrap = await api("/api/auth/bootstrap");
    renderRoleCatalog();
    const token = localStorage.getItem("contentops_token");
    if (token) {
      try {
        await loadSession();
        showApp();
        await refreshAll(false);
        return;
      } catch {
        localStorage.removeItem("contentops_token");
      }
    }
    setAuthTab(state.authBootstrap.first_user ? "register" : "login");
    showAuth();
  } catch (error) {
    renderRoleCatalog();
    setAuthTab("login");
    showAuth();
    setAuthError(`无法连接登录服务：${error.message}`);
  }
}

const USERNAME_RULE =
  "用户名需为 3-32 位字母、数字、下划线、点或短横线";

function validateAuthInput({ username, password, displayName = null, strictPassword = false }) {
  const problems = [];
  if (!username) {
    problems.push("用户名：不能为空");
  } else if (!/^[a-zA-Z0-9_.-]{3,32}$/.test(username)) {
    problems.push(USERNAME_RULE);
  }
  if (displayName !== null) {
    if (!displayName) {
      problems.push("显示名称：不能为空");
    } else if (displayName.length > 80) {
      problems.push("显示名称：长度不能超过 80 个字符");
    }
  }
  if (!password) {
    problems.push("密码：不能为空");
  } else if (password.length < 8 || password.length > 128) {
    problems.push("密码：长度必须为 8-128 位");
  } else if (strictPassword && (!/[a-zA-Z]/.test(password) || !/\d/.test(password))) {
    problems.push("密码：必须同时包含字母和数字");
  }
  return problems;
}

async function submitLogin(event) {
  event.preventDefault();
  setAuthError();
  const username = document.getElementById("loginUsername").value.trim();
  const password = document.getElementById("loginPassword").value;
  const problems = validateAuthInput({ username, password });
  if (problems.length) {
    setAuthError(problems.join("；"));
    return;
  }
  try {
    const result = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    localStorage.setItem("contentops_token", result.token);
    state.session = result.session;
    showApp();
    renderSession();
    await refreshAll(false);
    toast(`欢迎回来，${result.user.display_name}（${result.user.role_label}）`);
  } catch (error) {
    setAuthError(readableAuthError(error.message));
  }
}

async function submitRegister(event) {
  event.preventDefault();
  setAuthError();
  const username = document.getElementById("registerUsername").value.trim();
  const displayName = document.getElementById("registerDisplayName").value.trim();
  const password = document.getElementById("registerPassword").value;
  const registrationCode = document.getElementById("registrationCode").value.trim();
  const role = document.getElementById("registerRole").value || "operator";
  const problems = validateAuthInput({ username, password, displayName, strictPassword: true });
  if (problems.length) {
    setAuthError(problems.join("；"));
    return;
  }
  try {
    const result = await api("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({
        username,
        display_name: displayName,
        password,
        registration_code: registrationCode,
        role,
      }),
    });
    localStorage.setItem("contentops_token", result.token);
    state.session = result.session;
    state.authBootstrap = await api("/api/auth/bootstrap");
    renderRoleCatalog();
    showApp();
    renderSession();
    await refreshAll(false);
    toast(`账号创建成功，当前角色：${result.user.role_label}`);
  } catch (error) {
    setAuthError(readableAuthError(error.message));
  }
}

async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST", body: "{}" });
  } catch {
    // The local session is cleared even if the server session already expired.
  }
  localStorage.removeItem("contentops_token");
  state.session = null;
  state.jobs = [];
  state.overview = null;
  state.schedules = [];
  document.querySelectorAll(".modal-backdrop, .drawer-backdrop").forEach(
    (element) => element.classList.add("hidden"),
  );
  setAuthTab("login");
  renderRoleCatalog();
  showAuth();
}

function switchView(view) {
  state.view = view;
  document.querySelectorAll(".view").forEach((el) => {
    el.classList.toggle("active", el.id === `view-${view}`);
  });
  document.querySelectorAll(".nav-item").forEach((el) => {
    el.classList.toggle("active", el.dataset.view === view);
  });
  document.getElementById("pageTitle").textContent = TITLES[view] || "运营概览";
  if (view === "members") loadMembers();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function emptyRow(colspan, message = "暂无内容") {
  return `<tr class="loading-row"><td colspan="${colspan}">${escapeHtml(message)}</td></tr>`;
}

function rowForJob(job, large = false) {
  const risk = riskFor(job);
  const account = job.account || {};
  return `
    <tr>
      <td class="primary-cell">${escapeHtml(jobTitle(job))}</td>
      <td>
        <div class="account-cell">
          <span class="mini-avatar">${escapeHtml((account.name || "内").slice(0, 1))}</span>
          <span>
            <span style="display:block;color:#354155;font-size:9px">${escapeHtml(account.name || "未绑定账号")}</span>
            ${large ? `<span style="display:block;color:#9aa3b2;font-size:8px;margin-top:2px">${escapeHtml(account.platform || "—")}</span>` : ""}
          </span>
        </div>
      </td>
      <td>${statusPill(job.status)}</td>
      ${large ? `<td><span class="status-pill ${risk.cls}">${escapeHtml(risk.label)}</span></td>` : ""}
      <td>${escapeHtml(job.operator || "—")}</td>
      <td>${escapeHtml(formatDate(job.updated_at))}</td>
      <td><button class="row-button" data-open-job="${escapeHtml(job.id)}" type="button">${job.status === "awaiting_human" ? "去审批" : "查看"}</button></td>
    </tr>`;
}

function renderOverview() {
  const overview = state.overview || {};
  document.getElementById("metricPending").textContent = overview.awaiting_human ?? 0;
  document.getElementById("metricActive").textContent = overview.active_jobs ?? 0;
  document.getElementById("metricCompletion").textContent = `${overview.completion_rate ?? 0}%`;
  document.getElementById("metricScheduled").textContent = overview.scheduled_total ?? 0;
  document.getElementById("metricCompletionDetail").textContent =
    overview.total_jobs ? `共 ${overview.total_jobs} 条内容任务` : "暂无已结束任务";

  const pendingCount = overview.awaiting_human ?? 0;
  const navCount = document.getElementById("pendingNavCount");
  navCount.textContent = pendingCount;
  navCount.dataset.zero = String(pendingCount === 0);

  const evaluation = overview.evaluation || {};
  document.getElementById("qualityF1").textContent =
    `${Math.round((evaluation.guardrail_f1 ?? 0.909) * 1000) / 10}`;
  document.getElementById("qualityPrecision").textContent =
    `${Math.round((evaluation.guardrail_precision ?? 0.833) * 1000) / 10}%`;
  document.getElementById("qualityRecall").textContent =
    `${Math.round((evaluation.guardrail_recall ?? 1) * 1000) / 10}%`;
  document.getElementById("qualityFlow").textContent = evaluation.flow_pass || "10/10";

  const recent = overview.recent_jobs || [];
  document.getElementById("overviewRecentJobs").innerHTML = recent.length
    ? recent.map((job) => rowForJob(job, false)).join("")
    : emptyRow(6, "暂无任务，创建第一条内容需求开始使用。");

  const pending = recent.filter((job) => job.status === "awaiting_human").slice(0, 5);
  const approvalList = document.getElementById("overviewApprovalList");
  if (!pending.length) {
    approvalList.innerHTML = `
      <div class="empty-state" style="padding:42px 12px 38px">
        <span class="empty-mark">✓</span>
        <h3>审批队列已清空</h3>
        <p>当前没有等待确认的内容。</p>
      </div>`;
  } else {
    approvalList.innerHTML = pending.map((job) => {
      const review = job.review_result || {};
      const risk = review.violations?.length || 0;
      return `
        <div class="approval-item">
          <div>
            <h4>${escapeHtml(jobTitle(job))}</h4>
            <div class="approval-meta">
              ${escapeHtml(job.account?.name || "未绑定账号")} · ${escapeHtml(job.account?.platform || "—")}
              ${risk ? `<span class="risk-label">${risk} 项需确认</span>` : ""}
            </div>
          </div>
          <button class="row-button" data-open-job="${escapeHtml(job.id)}" type="button">去审批</button>
        </div>`;
    }).join("");
  }
}

function filteredJobs() {
  const needle = state.search.trim().toLowerCase();
  return state.jobs.filter((job) => {
    if (state.filter && job.status !== state.filter) return false;
    if (!needle) return true;
    return `${jobTitle(job)} ${job.objective}`.toLowerCase().includes(needle);
  });
}

function renderQueue() {
  const jobs = filteredJobs();
  const body = document.getElementById("queueTableBody");
  const empty = document.getElementById("queueEmpty");
  body.innerHTML = jobs.length
    ? jobs.map((job) => rowForJob(job, true)).join("")
    : "";
  empty.classList.toggle("hidden", jobs.length > 0);
}

function renderCalendar() {
  const schedules = state.schedules || [];
  const now = new Date();
  const upcoming = schedules.filter((item) => new Date(item.publish_at) >= now);
  document.getElementById("calendarUpcoming").textContent = upcoming.length;
  document.getElementById("calendarTotal").textContent = schedules.length;
  document.getElementById("calendarPlatforms").textContent =
    new Set(schedules.map((item) => item.platform).filter(Boolean)).size;

  const list = document.getElementById("scheduleList");
  if (!schedules.length) {
    list.innerHTML = `
      <div class="panel empty-state">
        <span class="empty-mark">期</span>
        <h3>还没有发布排期</h3>
        <p>内容通过人工审批后会自动进入发布日程。</p>
      </div>`;
    return;
  }
  list.innerHTML = schedules.map((item) => {
    const date = new Date(item.publish_at);
    const dateLabel = Number.isNaN(date.getTime())
      ? item.publish_at
      : new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit" }).format(date);
    const timeLabel = Number.isNaN(date.getTime())
      ? ""
      : new Intl.DateTimeFormat("zh-CN", { weekday: "short", hour: "2-digit", minute: "2-digit" }).format(date);
    return `
      <article class="schedule-item">
        <div class="schedule-time"><strong>${escapeHtml(dateLabel)}</strong><span>${escapeHtml(timeLabel)}</span></div>
        <div class="schedule-content">
          <h4>${escapeHtml(item.title || "未命名内容")}</h4>
          <p>${escapeHtml(item.author_note || "已通过审批，等待发布。")}</p>
        </div>
        <div class="schedule-platform">
          <strong>${escapeHtml(item.platform || "—")}</strong>
          <span>${escapeHtml(item.schedule_id || "")}</span>
        </div>
      </article>`;
  }).join("");
}

function renderHealth() {
  const health = state.health || {};
  const mode = health.mock_mode ? "Mock 演示模式" : "DeepSeek 在线模式";
  document.getElementById("systemMode").textContent = mode;
  document.getElementById("systemModeDetail").textContent =
    state.session ? "登录会话与角色权限已启用" : "等待登录";
}

function renderSession() {
  const actor = state.session.actor;
  document.getElementById("operatorName").textContent = actor.name;
  document.getElementById("operatorRole").textContent = actor.role_label;
  document.getElementById("operatorAvatar").textContent =
    actor.name.slice(0, 2).toUpperCase();
  const canCreate = can("content.create");
  const canManage = can("settings.manage");
  document.querySelectorAll('[data-action="new-job"]').forEach((button) => {
    button.classList.toggle("hidden", !canCreate);
  });
  document.getElementById("memberNav").classList.toggle("hidden", !canManage);
  if (!canManage && state.view === "members") switchView("overview");
}

async function loadSession() {
  state.session = await api("/api/session");
  renderSession();
}

async function refreshAll(showToast = false) {
  const refreshButton = document.getElementById("refreshButton");
  refreshButton.disabled = true;
  try {
    const [overview, jobs, schedules, health] = await Promise.all([
      api("/api/overview"),
      api("/api/jobs?limit=200"),
      api("/api/schedules?limit=200"),
      api("/api/health"),
    ]);
    state.overview = overview;
    state.jobs = jobs.items || [];
    state.schedules = schedules.items || [];
    state.health = health;
    renderOverview();
    renderQueue();
    renderCalendar();
    renderHealth();
    if (showToast) toast("工作台数据已刷新");
  } catch (error) {
    toast(`加载工作台失败：${error.message}`, "error");
  } finally {
    refreshButton.disabled = false;
  }
}

function violationLabel(risk) {
  return risk === "high" ? "高风险" : risk === "medium" ? "需复核" : "提示";
}

function renderTimeline(events = []) {
  if (!events.length) return `<div class="detail-empty">暂无审计记录</div>`;
  return `<div class="timeline">${events.map((event) => `
    <div class="timeline-item">
      <strong>${escapeHtml(event.message)}</strong>
      <span>${escapeHtml(event.actor)} · ${escapeHtml(formatFullDate(event.created_at))}</span>
    </div>`).join("")}</div>`;
}

function renderResearchEvidence(evidence = []) {
  if (!evidence.length) return `<div class="detail-empty">暂无外部检索证据</div>`;
  return `<div class="candidate-list">${evidence.map((item) => {
    const results = Array.isArray(item.result) ? item.result.slice(0, 3) : [];
    return `
      <div class="candidate-item" style="grid-template-columns:1fr">
        <div>
          <h4>${escapeHtml(item.tool)}</h4>
          <p>${results.length
            ? results.map((result) =>
                escapeHtml(result.topic || result.title || result.content || "检索结果")
              ).join(" · ")
            : "工具返回异常或没有可用结果"}</p>
        </div>
      </div>`;
  }).join("")}</div>`;
}

function renderDrawer(detail) {
  const job = detail.job;
  const pending = detail.pending;
  const review = job.review_result || {};
  const content = pending?.draft || job.final_content || job.draft || {};
  const candidates = job.candidates || [];
  const researchEvidence = job.research_evidence || [];
  const brandKit = job.account?.brand_kit || {};
  const violations = review.violations || [];
  const canApprove = can("content.approve");
  const editable = Boolean(pending) && canApprove;
  const needsOverride = Boolean(pending?.requires_override_reason);

  const editor = editable
    ? `
      <div class="detail-card">
        <span class="section-kicker">CONTENT REVIEW</span>
        <h3>正文与发布信息</h3>
        <label class="editor-field"><span>标题</span><input id="detailTitle" value="${escapeHtml(content.title || "")}"></label>
        <label class="editor-field"><span>正文</span><textarea id="detailBody">${escapeHtml(content.body || "")}</textarea></label>
        <label class="editor-field"><span>标签（逗号分隔）</span><input id="detailTags" value="${escapeHtml((content.tags || []).join(", "))}"></label>
        <label class="editor-field"><span>人工放行理由（合规失败但直接通过时必填）</span><input id="detailOverrideReason" placeholder="说明为何仍允许发布"></label>
      </div>`
    : `
      <div class="detail-card">
        <span class="section-kicker">CONTENT SNAPSHOT</span>
        <h3>${escapeHtml(content.title || jobTitle(job))}</h3>
        <p style="margin:0;color:#667286;font-size:10px;line-height:1.8;white-space:pre-wrap">${escapeHtml(content.body || "内容尚未生成。")}</p>
        <div style="margin-top:12px">${(content.tags || []).map((tag) => `<span class="status-pill neutral">${escapeHtml(tag)}</span>`).join(" ")}</div>
      </div>`;
  const permissionNote = pending && !canApprove
    ? `<div class="schedule-callout">当前角色只能查看这条待审批内容。创建任务由内容运营负责，通过、编辑和拒绝由内容审核员负责。</div>`
    : "";

  const riskBox = `
    <div class="detail-card">
      <span class="section-kicker">COMPLIANCE REVIEW</span>
      <h3>合规检查</h3>
      <div class="risk-summary ${review.passed ? "safe" : ""}">
        ${review.passed
          ? "当前内容通过规则与语义检查。"
          : needsOverride
            ? "当前内容未通过合规检查。可以修改后重新复审，或填写理由人工放行。"
            : "当前内容仍有风险，请确认后再处理。"}
      </div>
      <div class="violation-list">
        ${violations.length
          ? violations.map((item) => `
            <div class="violation-item">
              <strong>${escapeHtml(item.word)} · ${escapeHtml(violationLabel(item.risk_level))}</strong>
              <span>${escapeHtml(item.category || "")}：${escapeHtml(item.advice || "")}</span>
            </div>`).join("")
          : `<div class="detail-empty">未命中规则风险项</div>`}
      </div>
    </div>`;

  const candidatesBox = candidates.length
    ? `
      <div class="detail-card">
        <span class="section-kicker">TOPIC EVIDENCE</span>
        <h3>候选选题与依据</h3>
        <div class="candidate-list">
          ${candidates.map((candidate) => `
            <div class="candidate-item">
              <div>
                <h4>${escapeHtml(candidate.title)}</h4>
                <p>${escapeHtml(candidate.reason || "")}</p>
              </div>
              <span class="score-badge">${Math.round((candidate.est_score || 0) * 100)}</span>
            </div>`).join("")}
        </div>
      </div>`
    : "";

  const scheduleBox = job.schedule?.schedule_id
    ? `<div class="schedule-callout">已生成排期 ${escapeHtml(job.schedule.schedule_id)}。审批令牌与当前内容绑定，内容被修改后不能继续使用。</div>`
    : "";
  const brandBox = `
    <div class="detail-card">
      <span class="section-kicker">BRAND GUARDRAILS</span>
      <h3>品牌约束</h3>
      <div class="violation-list">
        <div class="violation-item">
          <strong>品牌语气</strong>
          <span>${escapeHtml(brandKit.tone || "未单独配置")}</span>
        </div>
        <div class="violation-item">
          <strong>禁用表达</strong>
          <span>${escapeHtml((brandKit.banned_claims || []).join("、") || "沿用平台合规规则")}</span>
        </div>
        <div class="violation-item">
          <strong>行动号召</strong>
          <span>${escapeHtml(brandKit.cta || "未配置")}</span>
        </div>
      </div>
    </div>`;

  document.getElementById("drawerContent").innerHTML = `
    <div class="drawer-header">
      <div class="drawer-title">
        <span class="section-kicker">CONTENT WORKSPACE</span>
        <h2>${escapeHtml(jobTitle(job))}</h2>
        <p>${escapeHtml(job.account?.name || "未绑定账号")} · ${escapeHtml(job.account?.platform || "—")} · ${escapeHtml(job.objective || "")}</p>
      </div>
      <div style="text-align:right">
        ${statusPill(job.status)}
        <button class="icon-button" data-close="jobDrawer" type="button" style="margin-top:10px">×</button>
      </div>
    </div>
    <div class="drawer-body">
      <div class="detail-grid">
        <div class="detail-main">
          ${editor}
          ${permissionNote}
          ${candidatesBox}
          <div class="detail-card">
            <span class="section-kicker">RETRIEVAL EVIDENCE</span>
            <h3>热榜与案例检索证据</h3>
            ${renderResearchEvidence(researchEvidence)}
          </div>
        </div>
        <aside class="detail-side">
          ${brandBox}
          ${riskBox}
          <div class="detail-card">
            <span class="section-kicker">AUDIT TRAIL</span>
            <h3>处理记录</h3>
            ${renderTimeline(detail.events)}
          </div>
        </aside>
      </div>
      ${scheduleBox}
      ${editable ? `
        <div class="drawer-actions">
          <button class="danger-button" data-decision="rejected" type="button">拒绝</button>
          <button class="secondary-button" data-decision="edited" type="button">保存并复审</button>
          <button class="primary-button" data-decision="approved" type="button">通过并排期</button>
        </div>` : ""}
    </div>`;
}

async function openJob(jobId) {
  state.activeJobId = jobId;
  try {
    const detail = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    state.activeJob = detail;
    renderDrawer(detail);
    openModal("jobDrawer");
  } catch (error) {
    toast(`打开任务失败：${error.message}`, "error");
  }
}

async function submitNewJob(event) {
  event.preventDefault();
  const button = document.getElementById("submitJobButton");
  const payload = {
    objective: document.getElementById("jobObjective").value.trim(),
    account: {
      name: document.getElementById("accountName").value.trim(),
      platform: document.getElementById("accountPlatform").value,
      category: document.getElementById("accountCategory").value.trim(),
      persona: document.getElementById("accountPersona").value.trim(),
      brand_kit: {
        tone: document.getElementById("brandTone").value.trim(),
        banned_claims: document.getElementById("brandBannedClaims").value
          .split(",")
          .map((value) => value.trim())
          .filter(Boolean),
        required_phrases: document.getElementById("brandRequiredPhrases").value
          .split(",")
          .map((value) => value.trim())
          .filter(Boolean),
        cta: document.getElementById("brandCta").value.trim(),
      },
    },
  };
  button.disabled = true;
  button.textContent = "Agent 处理中…";
  resetRunProgress();
  let finished = false;
  try {
    const result = await streamRun(payload, (event, data) => {
      if (event === "start") {
        markRunProgressHint("工作流已启动，逐节点执行中…", "running");
      } else if (event === "node") {
        appendRunProgress(data.node, data.summary);
        markRunProgressHint("执行中…", "running");
      } else if (event === "awaiting_human") {
        markRunProgressHint("已暂停，等待人工审批", "waiting");
      } else if (event === "done") {
        finished = true;
        markRunProgressHint(
          data.status === "awaiting_human" ? "已暂停，等待人工审批" : "执行完成",
          data.status === "awaiting_human" ? "waiting" : "success",
        );
      }
    });
    await new Promise((resolve) => window.setTimeout(resolve, 450));
    closeModal("newJobModal");
    toast(
      result.status === "awaiting_human"
        ? "内容已进入审批队列，等待人工确认"
        : "内容任务已创建，选题、创作与合规初审已完成",
    );
    await refreshAll(false);
    switchView(result.status === "awaiting_human" ? "queue" : "overview");
    if (result.job?.id) await openJob(result.job.id);
  } catch (error) {
    if (!finished) markRunProgressHint(`执行失败：${error.message}`, "error");
    toast(`创建任务失败：${error.message}`, "error");
  } finally {
    button.disabled = false;
    button.textContent = "创建任务";
  }
}

async function submitDecision(action) {
  if (!state.activeJobId || !state.activeJob?.pending) {
    toast("任务当前不在审批状态", "error");
    return;
  }
  const content = action === "edited"
    ? {
        title: document.getElementById("detailTitle").value.trim(),
        body: document.getElementById("detailBody").value.trim(),
        tags: document.getElementById("detailTags").value
          .split(",")
          .map((value) => value.trim())
          .filter(Boolean),
        structure_note: state.activeJob.job.draft?.structure_note || "",
      }
    : undefined;
  const overrideReason = document.getElementById("detailOverrideReason")?.value.trim() || "";
  if (
    action === "approved" &&
    state.activeJob.pending.requires_override_reason &&
    !overrideReason
  ) {
    toast("合规未通过，直接通过前必须填写人工放行理由", "error");
    return;
  }
  try {
    const result = await api(`/api/human/${encodeURIComponent(state.activeJobId)}`, {
      method: "POST",
      body: JSON.stringify({
        action,
        content: content || null,
        override_reason: overrideReason,
      }),
    });
    toast(
      result.status === "completed"
        ? "内容已通过审批并完成排期"
        : result.status === "awaiting_human"
          ? "修改稿未通过，任务已重新进入审批"
          : result.status === "rejected"
            ? "内容已拒绝"
            : "审批已提交"
    );
    await refreshAll(false);
    if (result.job?.id) await openJob(result.job.id);
  } catch (error) {
    toast(`提交审批失败：${error.message}`, "error");
  }
}

function renderMembers() {
  const data = state.members || { items: [], summary: {} };
  document.getElementById("memberTotal").textContent = data.summary.total ?? 0;
  document.getElementById("memberActive").textContent = data.summary.active ?? 0;
  document.getElementById("memberAdmins").textContent = data.summary.admins ?? 0;
  const currentId = state.session?.actor?.id;
  const body = document.getElementById("memberTableBody");
  body.innerHTML = data.items.length
    ? data.items.map((user) => {
        const isSelf = user.id === currentId;
        return `
          <tr>
            <td class="primary-cell">${escapeHtml(user.display_name)}${isSelf ? ' <span class="status-pill info">当前账号</span>' : ""}</td>
            <td>${escapeHtml(user.username)}</td>
            <td>
              <select class="member-role-select" data-user-role="${escapeHtml(user.id)}" ${isSelf ? "disabled" : ""}>
                <option value="operator" ${user.role === "operator" ? "selected" : ""}>内容运营</option>
                <option value="reviewer" ${user.role === "reviewer" ? "selected" : ""}>内容审核员</option>
                <option value="viewer" ${user.role === "viewer" ? "selected" : ""}>只读观察者</option>
                <option value="admin" ${user.role === "admin" ? "selected" : ""}>管理员</option>
              </select>
            </td>
            <td>${user.active ? '<span class="status-pill success">启用</span>' : '<span class="status-pill danger">停用</span>'}</td>
            <td>${user.last_login_at ? escapeHtml(formatFullDate(user.last_login_at)) : "从未登录"}</td>
            <td>
              <div class="member-actions">
                <button class="row-button" data-user-toggle="${escapeHtml(user.id)}" data-user-active="${user.active}" ${isSelf ? "disabled" : ""}>${user.active ? "停用" : "启用"}</button>
                <button class="row-button" data-user-reset="${escapeHtml(user.id)}">重置密码</button>
              </div>
            </td>
          </tr>`;
      }).join("")
    : emptyRow(6, "暂无成员");
}

async function loadMembers() {
  if (!can("settings.manage")) return;
  try {
    state.members = await api("/api/admin/users");
    renderMembers();
  } catch (error) {
    toast(`加载成员失败：${error.message}`, "error");
  }
}

async function updateMemberRole(userId, role) {
  try {
    await api(`/api/admin/users/${encodeURIComponent(userId)}`, {
      method: "PATCH",
      body: JSON.stringify({ role }),
    });
    toast("成员角色已更新");
    await loadMembers();
  } catch (error) {
    toast(`更新角色失败：${error.message}`, "error");
    await loadMembers();
  }
}

async function toggleMember(userId, active) {
  try {
    await api(`/api/admin/users/${encodeURIComponent(userId)}`, {
      method: "PATCH",
      body: JSON.stringify({ active: !active }),
    });
    toast(!active ? "成员已启用" : "成员已停用");
    await loadMembers();
  } catch (error) {
    toast(`更新成员状态失败：${error.message}`, "error");
  }
}

async function submitNewMember(event) {
  event.preventDefault();
  try {
    await api("/api/admin/users", {
      method: "POST",
      body: JSON.stringify({
        display_name: document.getElementById("memberDisplayName").value.trim(),
        username: document.getElementById("memberUsername").value.trim(),
        password: document.getElementById("memberPassword").value,
        role: document.getElementById("memberRole").value,
      }),
    });
    closeModal("newMemberModal");
    document.getElementById("newMemberForm").reset();
    toast("成员创建成功");
    await loadMembers();
  } catch (error) {
    toast(`创建成员失败：${error.message}`, "error");
  }
}

async function submitResetPassword(event) {
  event.preventDefault();
  if (!state.resettingUserId) return;
  try {
    const result = await api(
      `/api/admin/users/${encodeURIComponent(state.resettingUserId)}/reset-password`,
      {
        method: "POST",
        body: JSON.stringify({
          password: document.getElementById("resetPasswordInput").value,
        }),
      },
    );
    closeModal("resetPasswordModal");
    document.getElementById("resetPasswordForm").reset();
    if (result.reset_self) {
      toast("当前账号密码已重置，请重新登录");
      await logout();
    } else {
      toast("密码已重置，该成员旧会话已失效");
      await loadMembers();
    }
  } catch (error) {
    toast(`重置密码失败：${error.message}`, "error");
  }
}

function bindEvents() {
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.addEventListener("click", () => switchView(button.dataset.view));
  });
  document.querySelectorAll("[data-goto]").forEach((button) => {
    button.addEventListener("click", () => switchView(button.dataset.goto));
  });
  document.querySelectorAll("[data-action='new-job']").forEach((button) => {
    button.addEventListener("click", () => openModal("newJobModal"));
  });
  document.querySelectorAll("[data-close]").forEach((button) => {
    button.addEventListener("click", () => closeModal(button.dataset.close));
  });
  document.querySelectorAll(".modal-backdrop").forEach((backdrop) => {
    backdrop.addEventListener("click", (event) => {
      if (event.target === backdrop) backdrop.classList.add("hidden");
    });
  });
  document.getElementById("jobDrawer").addEventListener("click", (event) => {
    if (event.target.id === "jobDrawer") closeModal("jobDrawer");
  });
  document.getElementById("newJobForm").addEventListener("submit", submitNewJob);
  document.getElementById("loginForm").addEventListener("submit", submitLogin);
  document.getElementById("registerForm").addEventListener("submit", submitRegister);
  document.getElementById("logoutButton").addEventListener("click", logout);
  document.getElementById("newMemberButton").addEventListener("click", () => openModal("newMemberModal"));
  document.getElementById("newMemberForm").addEventListener("submit", submitNewMember);
  document.getElementById("resetPasswordForm").addEventListener("submit", submitResetPassword);
  document.querySelectorAll("[data-auth-tab]").forEach((button) => {
    button.addEventListener("click", () => setAuthTab(button.dataset.authTab));
  });
  document.getElementById("refreshButton").addEventListener("click", () => refreshAll(true));
  document.getElementById("queueFilters").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-status]");
    if (!button) return;
    state.filter = button.dataset.status;
    document.querySelectorAll("#queueFilters button").forEach((item) => {
      item.classList.toggle("active", item === button);
    });
    renderQueue();
  });
  document.getElementById("jobSearch").addEventListener("input", (event) => {
    state.search = event.target.value;
    renderQueue();
  });
  document.addEventListener("click", (event) => {
    const closeButton = event.target.closest("[data-close]");
    if (closeButton) closeModal(closeButton.dataset.close);
    const openButton = event.target.closest("[data-open-job]");
    if (openButton) openJob(openButton.dataset.openJob);
    const decisionButton = event.target.closest("[data-decision]");
    if (decisionButton) submitDecision(decisionButton.dataset.decision);
    const toggleButton = event.target.closest("[data-user-toggle]");
    if (toggleButton) {
      toggleMember(
        toggleButton.dataset.userToggle,
        toggleButton.dataset.userActive === "true",
      );
    }
    const resetButton = event.target.closest("[data-user-reset]");
    if (resetButton) {
      state.resettingUserId = resetButton.dataset.userReset;
      openModal("resetPasswordModal");
    }
  });
  document.addEventListener("change", (event) => {
    const roleSelect = event.target.closest("[data-user-role]");
    if (roleSelect) {
      updateMemberRole(roleSelect.dataset.userRole, roleSelect.value);
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      document.querySelectorAll(".modal-backdrop, .drawer-backdrop").forEach((el) => el.classList.add("hidden"));
    }
  });
}

async function initialize() {
  document.getElementById("todayLabel").textContent = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "long",
    day: "numeric",
    weekday: "short",
  }).format(new Date());
  bindEvents();
  await bootstrapAuth();
}

initialize();
