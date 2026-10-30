/* 共享 JS：AI 状态指示器 + 认证工具 + API 封装 */
const API_BASE = location.origin + "/api";

function getToken() { return localStorage.getItem("crm_token") || ""; }
function authHeaders() {
    const t = getToken();
    return t ? { "Authorization": "Bearer " + t } : {};
}

async function apiGet(path) {
    const r = await fetch(API_BASE + path, { headers: authHeaders() });
    if (!r.ok) throw new Error(await r.text());
    return r.json();
}
async function apiPost(path, body) {
    body = body || {};
    const r = await fetch(API_BASE + path, {
        method: "POST",
        headers: Object.assign({ "Content-Type": "application/json" }, authHeaders()),
        body: JSON.stringify(body),
    });
    if (!r.ok) {
        const err = await r.json().catch(function() { return {}; });
        throw new Error(err.detail || err.message || "请求失败");
    }
    return r.json();
}
function esc(s) {
    if (!s) return "";
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
}
function toast(msg, type) {
    const t = document.getElementById("toast");
    if (!t) return;
    t.textContent = msg;
    t.className = "toast show " + (type || "info");
    setTimeout(function() { t.classList.remove("show"); }, 3000);
}

/* 在 header 右侧注入 AI 状态指示器和用户信息 */
function injectHeaderExtras() {
    const actions = document.querySelector(".header-actions");
    if (!actions) return;

    const aiHtml =
        '<div class="ai-status loading" id="aiStatus" onclick="toggleAiTooltip(event)">' +
            '<span class="dot"></span>' +
            '<span id="aiStatusText">AI 检测中...</span>' +
            '<div class="ai-tooltip" id="aiTooltip">' +
                '<div><span class="label">模型:</span> <span id="aiModel">-</span></div>' +
                '<div><span class="label">状态:</span> <span id="aiMessage">-</span></div>' +
            '</div>' +
        '</div>' +
        '<span id="userBadge" style="display:none;" class="user-badge">' +
            '<span class="avatar" id="userAvatar">U</span>' +
            '<span id="userEmail">-</span>' +
        '</span>' +
        '<button class="logout-btn" id="logoutBtn" style="display:none;" onclick="doLogout()">退出</button>';

    actions.insertAdjacentHTML("afterbegin", aiHtml);

    document.addEventListener("click", function() {
        const tip = document.getElementById("aiTooltip");
        if (tip) tip.classList.remove("show");
    });
}

function toggleAiTooltip(e) {
    e.stopPropagation();
    const tip = document.getElementById("aiTooltip");
    if (tip) tip.classList.toggle("show");
}

async function loadAiStatus() {
    const el = document.getElementById("aiStatus");
    if (!el) return;
    const txt = document.getElementById("aiStatusText");
    el.className = "ai-status loading";
    txt.textContent = "AI 检测中...";
    try {
        const r = await fetch(API_BASE + "/ai/status");
        const d = await r.json();
        const modelEl = document.getElementById("aiModel");
        const msgEl = document.getElementById("aiMessage");
        if (modelEl) modelEl.textContent = d.model || "Mock";
        if (msgEl) msgEl.textContent = d.message;
        if (d.connected) {
            el.className = "ai-status ok";
            txt.textContent = "AI (DeepSeek) 已接入";
        } else if (d.mock_mode) {
            el.className = "ai-status mock";
            txt.textContent = "AI Mock 模式";
        } else {
            el.className = "ai-status error";
            txt.textContent = "AI 连接异常";
        }
    } catch(e) {
        el.className = "ai-status error";
        txt.textContent = "AI 状态未知";
        const msgEl = document.getElementById("aiMessage");
        if (msgEl) msgEl.textContent = "无法连接服务器";
    }
}

async function checkAuth() {
    const token = getToken();
    if (!token) return false;
    try {
        const r = await fetch(API_BASE + "/auth/status", { headers: authHeaders() });
        const d = await r.json();
        if (d.logged_in) {
            const badge = document.getElementById("userBadge");
            const logout = document.getElementById("logoutBtn");
            const emailEl = document.getElementById("userEmail");
            const avatar = document.getElementById("userAvatar");
            if (badge) badge.style.display = "inline-flex";
            if (logout) logout.style.display = "inline-block";
            if (emailEl) emailEl.textContent = d.email;
            if (avatar) avatar.textContent = (d.email || "U")[0].toUpperCase();
            return true;
        }
    } catch(e) {}
    return false;
}

async function doLogout() {
    try { await apiPost("/auth/logout"); } catch(e) {}
    localStorage.removeItem("crm_token");
    localStorage.removeItem("crm_email");
    location.href = "/login";
}

/* 共享样式 - 参考页面风格 v4 */
const sharedStyle = document.createElement("style");
sharedStyle.textContent =
    ".header-actions { position:relative; display:flex; align-items:center; gap:8px; }" +
    ".ai-status { display:inline-flex; align-items:center; gap:7px; padding:5px 13px; border-radius:9999px; font-size:12px; font-weight:600; cursor:pointer; transition:all .2s; white-space:nowrap; border:1px solid #b9efd9; background:#f0fff9; color:#149a68; }" +
    ".ai-status .dot { width:7px; height:7px; border-radius:50%; flex-shrink:0; background:#19b77a; }" +
    ".ai-status.ok .dot { box-shadow:0 0 0 0 rgba(25,183,122,.4); animation:pulse-green 2s infinite; }" +
    ".ai-status.mock { background:#fff7e5; color:#df9b19; border-color:#f5e0b0; } .ai-status.mock .dot { background:#df9b19; }" +
    ".ai-status.error { background:#fff0f0; color:#ef5555; border-color:#fcc; } .ai-status.error .dot { background:#ef5555; }" +
    ".ai-status.loading { color:#9aabc0; background:#f4f7fb; border-color:#e8eef5; } .ai-status.loading .dot { background:#9aabc0; animation:pulse-gray 1.5s infinite; }" +
    ".user-badge { display:inline-flex; align-items:center; gap:8px; padding:3px 12px 3px 3px; background:#f4f7fb; border-radius:9999px; font-size:12.5px; font-weight:600; color:#15233b; }" +
    ".user-badge .avatar { width:28px; height:28px; border-radius:50%; background:#4b76ed; color:#fff; display:flex; align-items:center; justify-content:center; font-size:12px; font-weight:600; }" +
    ".logout-btn { padding:7px 14px; border:1px solid #e8eef5; border-radius:10px; background:#fff; color:#7890ad; font-size:12.5px; font-weight:600; font-family:inherit; cursor:pointer; transition:all .2s; }" +
    ".logout-btn:hover { background:#fff0f0; color:#ef5555; border-color:#ef5555; }" +
    ".ai-tooltip { position:absolute; top:calc(100% + 8px); right:0; background:#fff; color:#15233b; border:1px solid #e8eef5; padding:12px 16px; border-radius:13px; font-size:12.5px; white-space:nowrap; box-shadow:0 8px 24px #203b5b12; z-index:100; display:none; line-height:1.6; }" +
    ".ai-tooltip.show { display:block; }" +
    ".ai-tooltip .label { color:#7890ad; margin-right:4px; }" +
    "@keyframes pulse-green { 0%{box-shadow:0 0 0 0 rgba(25,183,122,.4);} 70%{box-shadow:0 0 0 6px rgba(25,183,122,0);} 100%{box-shadow:0 0 0 0 rgba(25,183,122,0);} }" +
    "@keyframes pulse-gray { 0%{opacity:1;} 50%{opacity:0.4;} 100%{opacity:1;} }";
document.head.appendChild(sharedStyle);
