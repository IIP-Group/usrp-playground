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
                <a href="/dashboard.html" data-page="dashboard">Dashboard</a>
                <a href="/users.html"     data-page="users">Users</a>
                <a href="/logs.html"      data-page="logs">Logs</a>
                <a href="/settings.html"  data-page="settings">Settings</a>
            </nav>
            <div class="footer">
                <button id="logout-btn" class="btn btn-sm">Abmelden</button>
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
    document.getElementById(id).classList.add("open");
}
function closeModal(id) {
    document.getElementById(id).classList.remove("open");
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
                    <h3>Bestätigung</h3>
                    <p id="__pw_msg" class="text-dim" style="margin-bottom: 14px;"></p>
                    <div class="form-row">
                        <label>Admin-Passwort</label>
                        <input type="password" id="__pw_input" autocomplete="current-password" style="width: 100%;">
                    </div>
                    <div id="__pw_err" class="notice err" style="display:none;"></div>
                    <div class="actions">
                        <button class="btn" id="__pw_cancel">Abbrechen</button>
                        <button class="btn btn-danger" id="__pw_ok">Bestätigen</button>
                    </div>
                </div>`;
            document.body.appendChild(host);
        }
        const input = document.getElementById("__pw_input");
        const err = document.getElementById("__pw_err");
        input.value = "";
        err.style.display = "none";
        document.getElementById("__pw_msg").textContent = message || "Bitte Admin-Passwort eingeben:";
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
            if (!v) { err.textContent = "Passwort darf nicht leer sein"; err.style.display = "block"; return; }
            done(v);
        };
        cancel.onclick = () => done(null);
        input.onkeydown = (e) => {
            if (e.key === "Enter") ok.click();
            if (e.key === "Escape") cancel.click();
        };
    });
}
