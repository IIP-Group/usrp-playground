/* ===== UnitInput - number on the left, unit dropdown on the right =====
 *
 * The widget always converts to/from a canonical numeric value (whatever the
 * backend stores: Hz, seconds, dB, MB, ...). Unit conversion happens at the
 * widget boundary; the rest of the app works with canonical numbers.
 *
 * Each unit spec:
 *   { key, label, mul }       mul = canonical-per-displayed
 *                             (e.g. for s/ms: ms.mul = 0.001)
 *
 * Special case - "samples":   mul = "samples"
 *   `multiplierFn()` is called to read the current sample rate (Hz). The
 *   effective mul becomes 1/fs (i.e. 1 sample == 1/fs seconds). This is the
 *   only unit where the conversion factor is dynamic.
 */

class UnitInput {
    constructor(host, opts = {}) {
        this.host          = host;
        this.units         = opts.units || [{ key: "u", label: "", mul: 1 }];
        // `persistKey` lets us remember the user's last chosen unit per
        // setting key in localStorage so it survives page reloads.
        this.persistKey    = opts.persistKey || null;
        const stored       = this.persistKey
            ? localStorage.getItem(`usrp.unit.${this.persistKey}`)
            : null;
        const valid        = stored && this.units.some(u => u.key === stored);
        this.displayUnit   = valid ? stored : (opts.defaultUnit || this.units[0].key);
        this.value         = 0;                         // canonical numeric
        this.onChange      = opts.onChange || (() => {});
        this.multiplierFn  = opts.multiplierFn || null; // for "samples"
        this.precision     = opts.precision ?? 9;
        this.step          = opts.step || "any";

        host.classList.add("unit-input-host");
        host.innerHTML = `
            <div class="unit-input">
                <input type="number" class="unit-input-num" step="${this.step}" autocomplete="off">
                <div class="unit-input-divider"></div>
                <div class="unit-input-select-wrap">
                    <select class="unit-input-sel">
                        ${this.units.map(u =>
                            `<option value="${u.key}">${u.label}</option>`).join("")}
                    </select>
                    <span class="unit-input-chevron">▾</span>
                </div>
            </div>
        `;
        this.input  = host.querySelector(".unit-input-num");
        this.select = host.querySelector(".unit-input-sel");
        this.select.value = this.displayUnit;
        if (this.units.length === 1) {
            // Single unit → hide the select, show static suffix instead.
            this.host.querySelector(".unit-input-select-wrap").innerHTML =
                `<span class="unit-input-suffix">${this._esc(this.units[0].label)}</span>`;
        }

        this.input.addEventListener("input",  () => this.onChange(this.getValue()));
        this.select.addEventListener("change", () => {
            this.displayUnit = this.select.value;
            if (this.persistKey) {
                try { localStorage.setItem(`usrp.unit.${this.persistKey}`, this.displayUnit); } catch {}
            }
            this._renderInput();
            this.onChange(this.getValue());
        });
    }

    setValue(canonicalNumber) {
        const n = Number(canonicalNumber);
        this.value = isFinite(n) ? n : 0;
        this._renderInput();
    }

    /** Returns canonical numeric value (or NaN if input is empty/invalid). */
    getValue() {
        const raw = this.input.value;
        if (raw === "" || raw === null) return NaN;
        const num = Number(raw);
        if (!isFinite(num)) return NaN;
        return num * this._mul(this.select.value);
    }

    focus() { this.input.focus(); }

    _mul(unitKey) {
        const u = this.units.find(x => x.key === unitKey);
        if (!u) return 1;
        if (u.mul === "samples" && this.multiplierFn) {
            const fs = Number(this.multiplierFn());
            if (!fs || !isFinite(fs) || fs <= 0) return 1;
            return 1 / fs;
        }
        return Number(u.mul) || 1;
    }

    _renderInput() {
        const m = this._mul(this.select.value);
        if (!m) { this.input.value = ""; return; }
        const disp = this.value / m;
        if (!isFinite(disp)) { this.input.value = ""; return; }
        this.input.value = this._fmt(disp);
    }

    _fmt(n) {
        if (n === 0) return "0";
        // Avoid silly trailing-zero noise from float math (e.g. 25.000000003).
        const fixed = Number(n.toFixed(this.precision));
        return String(fixed);
    }

    _esc(s) {
        return String(s).replace(/[&<>"']/g, c =>
            ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
    }
}

window.UnitInput = UnitInput;
