/* ===== Shared helpers for the admin panel ===== */

const API = "/admin/api";

async function apiFetch(path, opts = {}) {
    const res = await fetch(API + path, {
        credentials: "same-origin",
        headers: opts.body instanceof FormData ? {} : {"Content-Type": "application/json"},
        ...opts,
    });
    if (res.status === 401) {
        // Not logged in — redirect to login (unless we are already there)
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

// Relative-time "vor X Minuten / Stunden / Tagen"
function relTime(isoDate) {
    if (!isoDate) return "–";
    const d = new Date(isoDate + (isoDate.endsWith("Z") ? "" : "Z"));
    const diffSec = Math.max(0, (Date.now() - d.getTime()) / 1000);
    if (diffSec < 60) return "gerade eben";
    if (diffSec < 3600) return `vor ${Math.floor(diffSec / 60)} Minuten`;
    if (diffSec < 86400) return `vor ${Math.floor(diffSec / 3600)} Stunden`;
    return `vor ${Math.floor(diffSec / 86400)} Tagen`;
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
                <h1>USRP Benchmark</h1>
                <small>Admin Panel</small>
            </div>
            <nav>
                <a href="/dashboard.html" data-page="dashboard">📊 Dashboard</a>
                <a href="/users.html"     data-page="users">👥 Users</a>
                <a href="/logs.html"      data-page="logs">📜 Logs</a>
                <a href="/settings.html"  data-page="settings">⚙️ Settings</a>
            </nav>
            <div class="footer">
                <div id="session-info">Signed in</div>
                <button id="logout-btn" class="btn btn-sm">Logout</button>
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
    // Populate username
    apiFetch("/me").then(me => {
        const el = document.getElementById("session-info");
        if (el) el.textContent = "👤 " + me.username;
    }).catch(() => {});
}

// Bulk-auth-check on page load: redirect to login if not authenticated
async function requireAuth() {
    try { await apiFetch("/me"); } catch { /* handled by apiFetch */ }
}

// ---- Modal helpers ----
function openModal(id) {
    document.getElementById(id).classList.add("open");
}
function closeModal(id) {
    document.getElementById(id).classList.remove("open");
}
