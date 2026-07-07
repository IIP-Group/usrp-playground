/* ===== Chip input - type + space/enter/comma commits a tag bubble ===== */
/* Backspace on empty input removes the last chip. × on a chip removes it.
 * Suggestion popover shows tags that already exist in the system. */

class ChipInput {
    constructor(host, opts = {}) {
        this.host = host;
        this.tags = [];
        this.placeholder = opts.placeholder || "Tag…";
        this.onChange = opts.onChange || (() => {});
        this.suggestionsFn = opts.suggestionsFn || (() => []);
        this.maxSuggestions = opts.maxSuggestions || 8;

        this.host.classList.add("chip-input");
        this.host.innerHTML = `
            <div class="chip-input-chips"></div>
            <input class="chip-input-field" autocomplete="off" autocapitalize="off"
                   spellcheck="false" placeholder="${this._esc(this.placeholder)}">
            <div class="chip-suggestions hidden"></div>
        `;
        this.chipsEl = this.host.querySelector(".chip-input-chips");
        this.input   = this.host.querySelector(".chip-input-field");
        this.suggEl  = this.host.querySelector(".chip-suggestions");

        this.host.addEventListener("click", (e) => {
            if (e.target === this.host || e.target === this.chipsEl) this.input.focus();
        });
        this.input.addEventListener("keydown", (e) => this._onKeyDown(e));
        this.input.addEventListener("input",   ()  => this._renderSuggestions());
        this.input.addEventListener("focus",   ()  => this._renderSuggestions());
        this.input.addEventListener("blur",    ()  => {
            // commit pending text on blur, then hide suggestions
            this._commit(this.input.value);
            setTimeout(() => this.suggEl.classList.add("hidden"), 120);
        });

        this._render();
    }

    static VALID_RE = /^[a-z0-9][a-z0-9_-]{0,31}$/;

    setValue(arr) {
        this.tags = (arr || [])
            .map(t => String(t).trim().toLowerCase())
            .filter((t, i, a) => t && a.indexOf(t) === i);
        this._render();
    }
    getValue() { return [...this.tags]; }
    focus()    { this.input.focus(); }
    clear()    { this.tags = []; this.input.value = ""; this._render(); }

    addTag(raw) {
        const v = String(raw || "").trim().toLowerCase();
        if (!v || !ChipInput.VALID_RE.test(v) || this.tags.includes(v)) return false;
        this.tags.push(v);
        this._render();
        this._scrollInputIntoView();
        this.onChange(this.getValue());
        return true;
    }

    _scrollInputIntoView() {
        // Keep the cursor visible at the end of the chip row
        requestAnimationFrame(() => {
            this.host.scrollLeft = this.host.scrollWidth;
        });
    }

    removeTag(t) {
        const before = this.tags.length;
        this.tags = this.tags.filter(x => x !== t);
        if (this.tags.length !== before) {
            this._render();
            this.onChange(this.getValue());
        }
    }

    _commit(value) {
        const v = String(value || "").trim();
        if (!v) return;
        // Allow pasting "a, b c;d" → 4 tags
        const parts = v.split(/[\s,;]+/).filter(Boolean);
        let any = false;
        for (const p of parts) any = this.addTag(p) || any;
        if (any) {
            this.input.value = "";
            this._renderSuggestions();
        }
    }

    _onKeyDown(e) {
        const k = e.key;
        if (k === " " || k === "Enter" || k === "," || k === ";" || k === "Tab") {
            if (this.input.value.trim()) {
                e.preventDefault();
                this._commit(this.input.value);
            } else if (k === "Enter") {
                e.preventDefault();
            }
        } else if (k === "Backspace" && this.input.value === "" && this.tags.length) {
            const last = this.tags[this.tags.length - 1];
            this.removeTag(last);
            this.input.value = last;
            // place cursor at end
            const len = this.input.value.length;
            this.input.setSelectionRange(len, len);
        } else if (k === "Escape") {
            this.suggEl.classList.add("hidden");
            this.input.blur();
        }
    }

    _render() {
        this.chipsEl.innerHTML = this.tags.map(t => `
            <span class="chip chip-removable" data-tag="${this._esc(t)}">
                ${this._esc(t)}
                <button class="chip-x" type="button" tabindex="-1" aria-label="entfernen">×</button>
            </span>`).join("");
        this.chipsEl.querySelectorAll(".chip-x").forEach(btn => {
            btn.addEventListener("mousedown", (e) => {
                e.preventDefault();   // keep input focus
                e.stopPropagation();
                this.removeTag(btn.parentElement.dataset.tag);
                this.input.focus();
            });
        });
    }

    _renderSuggestions() {
        const cur = this.input.value.trim().toLowerCase();
        const have = new Set(this.tags);
        let list = (this.suggestionsFn() || []).filter(t => !have.has(t));
        if (cur) list = list.filter(t => t.includes(cur) && t !== cur);
        if (!list.length) {
            this.suggEl.classList.add("hidden");
            return;
        }
        this.suggEl.classList.remove("hidden");
        this.suggEl.innerHTML = list.slice(0, this.maxSuggestions).map(t =>
            `<div class="chip-sugg-item" data-tag="${this._esc(t)}">${this._esc(t)}</div>`
        ).join("");
        this.suggEl.querySelectorAll(".chip-sugg-item").forEach(el => {
            el.addEventListener("mousedown", (e) => {
                e.preventDefault();
                this._commit(el.dataset.tag);
                this.input.focus();
            });
        });
    }

    _esc(s) {
        return String(s)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }
}

window.ChipInput = ChipInput;
