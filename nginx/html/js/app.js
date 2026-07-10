/* ===== Shared helpers for the admin panel ===== */

const API = "/admin/api";

async function apiFetch(path, opts = {}) {
    const res = await fetch(API + path, {
        credentials: "same-origin",
        headers: opts.body instanceof FormData ? {} : {"Content-Type": "application/json"},
        ...opts,
    });
    if (res.status === 401) {
        // Not logged in - redirect to login (unless we are already there)
        if (!location.pathname.endsWith("/") && !location.pathname.endsWith("index.html")) {
            location.href = "/";
        }
        throw new Error("Not authenticated");
    }
    const ct = res.headers.get("content-type") || "";
    const data = ct.includes("application/json") ? await res.json() : await res.text();
    if (!res.ok) {
        const msg = (data && data.detail) || (data && data.message) || res.statusText;
        throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    return data;
}

// Relative-time "X minutes / hours / days ago"
function relTime(isoDate) {
    if (!isoDate) return "-";
    const d = new Date(isoDate + (isoDate.endsWith("Z") ? "" : "Z"));
    const diffSec = Math.max(0, (Date.now() - d.getTime()) / 1000);
    if (diffSec < 60) return "just now";
    if (diffSec < 120) return "1 minute ago";
    if (diffSec < 3600) return `${Math.floor(diffSec / 60)} minutes ago`;
    if (diffSec < 7200) return "1 hour ago";
    if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} hours ago`;
    if (diffSec < 172800) return "1 day ago";
    return `${Math.floor(diffSec / 86400)} days ago`;
}

// Toast notifications
let toastHost;
function toast(msg, kind = "") {
    if (!toastHost) {
        toastHost = document.createElement("div");
        toastHost.className = "toast-host";
        document.body.appendChild(toastHost);
    }
    const el = document.createElement("div");
    el.className = "toast " + kind;
    el.textContent = msg;
    toastHost.appendChild(el);
    setTimeout(() => el.remove(), 3500);
}

// Escape HTML helper
function esc(s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/[&<>"']/g, c => ({
        "&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"
    })[c]);
}

// Sidebar: inject consistent nav into every page
function initShell(activePage) {
    const side = document.getElementById("sidebar");
    if (side) {
        side.innerHTML = `
            <div class="brand">
                <h1>USRP Playground</h1>
                <small>Admin Panel</small>
            </div>
            <nav>
                <a href="/dashboard.html" data-page="dashboard">Dashboard</a>
                <a href="/users.html"     data-page="users">Users</a>
                <a href="/benchmark.html" data-page="benchmark">Benchmark</a>
                <a href="/inventory.html" data-page="inventory">Hardware</a>
                <a href="/logs.html"      data-page="logs">Logs</a>
                <a href="/settings.html"  data-page="settings">Settings</a>
            </nav>
            <div class="footer">
                <button id="logout-btn" class="btn btn-sm">Sign out</button>
            </div>
        `;
        side.querySelectorAll("nav a").forEach(a => {
            if (a.dataset.page === activePage) a.classList.add("active");
        });
        side.querySelector("#logout-btn").addEventListener("click", async () => {
            try { await apiFetch("/logout", {method: "POST"}); } catch (e) {}
            location.href = "/";
        });
    }
}

// Bulk-auth-check on page load: redirect to login if not authenticated
async function requireAuth() {
    try { await apiFetch("/me"); } catch { /* handled by apiFetch */ }
}

// ---- Modal helpers ----
function openModal(id) {
    const el = document.getElementById(id);
    el.classList.remove("closing");
    el.classList.add("open");
}
function closeModal(id) {
    const el = document.getElementById(id);
    if (!el.classList.contains("open")) return;
    el.classList.add("closing");
    // Wait for the CSS transition (260ms) before fully hiding the modal.
    setTimeout(() => {
        el.classList.remove("open", "closing");
    }, 260);
}

// ---- Password confirmation modal ----
// Returns the entered password (string) or null if cancelled.
function askPassword(message) {
    return new Promise(resolve => {
        let host = document.getElementById("__pw_modal");
        if (!host) {
            host = document.createElement("div");
            host.id = "__pw_modal";
            host.className = "modal-backdrop";
            host.innerHTML = `
                <div class="modal" style="width: 420px;">
                    <h3>Confirmation</h3>
                    <p id="__pw_msg" class="text-dim" style="margin-bottom: 14px;"></p>
                    <div class="form-row">
                        <label>Admin password</label>
                        <input type="password" id="__pw_input" autocomplete="current-password" style="width: 100%;">
                    </div>
                    <div id="__pw_err" class="notice err" style="display:none;"></div>
                    <div class="actions">
                        <button class="btn" id="__pw_cancel">Cancel</button>
                        <button class="btn btn-danger" id="__pw_ok">Confirm</button>
                    </div>
                </div>`;
            document.body.appendChild(host);
        }
        const input = document.getElementById("__pw_input");
        const err = document.getElementById("__pw_err");
        input.value = "";
        err.style.display = "none";
        document.getElementById("__pw_msg").textContent = message || "Please enter the admin password:";
        host.classList.add("open");
        input.focus();

        const ok = document.getElementById("__pw_ok");
        const cancel = document.getElementById("__pw_cancel");
        const done = (val) => {
            host.classList.remove("open");
            ok.onclick = null; cancel.onclick = null; input.onkeydown = null;
            resolve(val);
        };
        ok.onclick = () => {
            const v = input.value;
            if (!v) { err.textContent = "Password cannot be empty"; err.style.display = "block"; return; }
            done(v);
        };
        cancel.onclick = () => done(null);
        input.onkeydown = (e) => {
            if (e.key === "Enter") ok.click();
            if (e.key === "Escape") cancel.click();
        };
    });
}
