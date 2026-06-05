/* ===== Benchmark page — TX a complex sine, measure what comes back =====
 *
 * The browser opens the same WebSocket /ws/run that the Python client uses,
 * sends interleaved float32 IQ samples, receives a status stream + the result
 * blob. We then run a small FFT on the signal slice, compute peak frequency,
 * power, clipping headroom, sample-balance — and render two spectrum plots
 * plus a power profile on plain canvas.
 *
 * Last successful run is stashed in localStorage so reloading the page or
 * leaving and coming back keeps the most recent benchmark visible.
 */

const STORAGE_KEY = "usrp.benchmark.lastrun.v1";

// ---- Cooley–Tukey FFT, in-place, complex (separate real/imag arrays) ----
function fftInPlace(re, im) {
    const n = re.length;
    if (n & (n - 1)) throw new Error("FFT length must be a power of 2");
    // bit-reversal permutation
    for (let i = 1, j = 0; i < n; i++) {
        let bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j |= bit;
        if (i < j) {
            const tr = re[i]; re[i] = re[j]; re[j] = tr;
            const ti = im[i]; im[i] = im[j]; im[j] = ti;
        }
    }
    // butterflies
    for (let len = 2; len <= n; len <<= 1) {
        const half = len >> 1;
        const ang = -2 * Math.PI / len;
        const wlenR = Math.cos(ang), wlenI = Math.sin(ang);
        for (let i = 0; i < n; i += len) {
            let wR = 1, wI = 0;
            for (let k = 0; k < half; k++) {
                const uR = re[i + k], uI = im[i + k];
                const aR = re[i + k + half], aI = im[i + k + half];
                const vR = aR * wR - aI * wI;
                const vI = aR * wI + aI * wR;
                re[i + k] = uR + vR; im[i + k] = uI + vI;
                re[i + k + half] = uR - vR; im[i + k + half] = uI - vI;
                const nR = wR * wlenR - wI * wlenI;
                wI = wR * wlenI + wI * wlenR;
                wR = nR;
            }
        }
    }
}

function nextPow2(n) { let p = 1; while (p < n) p <<= 1; return p; }

/** Returns {f_arr, mag_db, peak_freq, peak_db, peak_idx}. fs in Hz, signal as
 *  Float32Array interleaved I/Q is given as separate re/im arrays. */
function spectrum(re, im, fs) {
    const n0 = re.length;
    const n  = nextPow2(n0);
    const reP = new Float32Array(n);
    const imP = new Float32Array(n);
    reP.set(re); imP.set(im);
    fftInPlace(reP, imP);

    // FFT-shift so that fr goes from -fs/2 to +fs/2 with f=0 in the middle.
    const half = n >> 1;
    const f_arr = new Float32Array(n);
    const mag = new Float32Array(n);
    for (let k = 0; k < n; k++) {
        const src = (k + half) % n;
        const fr = ((src + half) % n - half) * (fs / n);
        f_arr[k] = fr;
        mag[k] = Math.hypot(reP[src], imP[src]);
    }
    // dB
    const mag_db = new Float32Array(n);
    let peak_idx = 0, peak_mag = -Infinity;
    for (let k = 0; k < n; k++) {
        const v = 20 * Math.log10(mag[k] + 1e-12);
        mag_db[k] = v;
        if (mag[k] > peak_mag) { peak_mag = mag[k]; peak_idx = k; }
    }
    // parabolic interpolation
    let peak_freq = f_arr[peak_idx];
    if (peak_idx > 0 && peak_idx < n - 1) {
        const a = mag[peak_idx - 1], b = mag[peak_idx], c = mag[peak_idx + 1];
        const denom = (a - 2 * b + c) || 1e-20;
        const delta = 0.5 * (a - c) / denom;
        peak_freq = f_arr[peak_idx] + delta * (fs / n);
    }
    return { f_arr, mag_db, peak_freq, peak_db: mag_db[peak_idx], peak_idx, n };
}

/** Decimate (f, y) to ~target points by max-pooling — keeps peaks visible. */
function downsamplePlot(f, y, target = 1024) {
    const n = f.length;
    if (n <= target) return { f: Array.from(f), y: Array.from(y) };
    const step = Math.ceil(n / target);
    const fOut = []; const yOut = [];
    for (let i = 0; i < n; i += step) {
        const end = Math.min(i + step, n);
        let best = -Infinity, bestF = f[i];
        for (let j = i; j < end; j++) {
            if (y[j] > best) { best = y[j]; bestF = f[j]; }
        }
        fOut.push(bestF); yOut.push(best);
    }
    return { f: fOut, y: yOut };
}

// ---- Canvas line plot ----
// Stash inputs on the element so we can redraw on resize without re-running
// the FFT pipeline.
function drawLinePlot(canvas, x, y, opts = {}) {
    canvas.__plot = { x, y, opts };
    _drawPlotNow(canvas);
}

// Single ResizeObserver for all plots — redraws on layout changes.
let _plotResizeObs = null;
function _ensureResizeObserver(canvas) {
    if (canvas.__plotObs) return;
    if (!_plotResizeObs) {
        _plotResizeObs = new ResizeObserver(entries => {
            for (const e of entries) _drawPlotNow(e.target);
        });
    }
    _plotResizeObs.observe(canvas);
    canvas.__plotObs = true;
}

// "Nice" tick step: yields ticks like 1, 2, 5, 10, 20, ... × 10^k.
function niceStep(range, target = 6) {
    if (range <= 0) return 1;
    const rough = range / target;
    const expn = Math.floor(Math.log10(rough));
    const mant = rough / Math.pow(10, expn);
    let nice;
    if (mant < 1.5)      nice = 1;
    else if (mant < 3.5) nice = 2;
    else if (mant < 7.5) nice = 5;
    else                 nice = 10;
    return nice * Math.pow(10, expn);
}
function niceTicks(min, max, target = 6) {
    const step = niceStep(max - min, target);
    const start = Math.ceil(min / step - 1e-9) * step;
    const end   = Math.floor(max / step + 1e-9) * step;
    const out = [];
    for (let v = start; v <= end + step * 1e-6; v += step) {
        out.push(Number(v.toFixed(12)));
    }
    return { ticks: out, step };
}
function fmtTick(v, step) {
    const decs = step >= 1 ? 0 : Math.min(6, Math.max(0, -Math.floor(Math.log10(step))));
    return v.toFixed(decs);
}

function _drawPlotNow(canvas) {
    const data = canvas.__plot;
    if (!data) return;
    _ensureResizeObserver(canvas);

    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (w <= 0 || h <= 0) {
        requestAnimationFrame(() => _drawPlotNow(canvas));
        return;
    }
    canvas.width = w * dpr; canvas.height = h * dpr;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);

    const { x, y, opts } = data;

    // Padding leaves room for axis labels (left + bottom).
    const padL = 56, padR = 16, padT = 16, padB = 44;
    const plotW = Math.max(1, w - padL - padR);
    const plotH = Math.max(1, h - padT - padB);

    // axis bounds
    let xMin = opts.xMin, xMax = opts.xMax;
    if (xMin == null) xMin = Math.min(...x);
    if (xMax == null) xMax = Math.max(...x);
    let yMin = opts.yMin, yMax = opts.yMax;
    if (yMin == null) yMin = Math.min(...y);
    if (yMax == null) yMax = Math.max(...y);
    if (xMax - xMin < 1e-12) { xMax += 1; xMin -= 1; }
    if (yMax - yMin < 1e-12) { yMax += 1; yMin -= 1; }

    const X = v => padL + ((v - xMin) / (xMax - xMin)) * plotW;
    const Y = v => padT + (1 - (v - yMin) / (yMax - yMin)) * plotH;

    const xT = niceTicks(xMin, xMax, 6);
    const yT = niceTicks(yMin, yMax, 5);

    // grid
    ctx.strokeStyle = "rgba(255,255,255,0.06)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (const t of yT.ticks) { const yy = Y(t); ctx.moveTo(padL, yy); ctx.lineTo(padL + plotW, yy); }
    for (const t of xT.ticks) { const xx = X(t); ctx.moveTo(xx, padT); ctx.lineTo(xx, padT + plotH); }
    ctx.stroke();

    // frame
    ctx.strokeStyle = "rgba(255,255,255,0.18)";
    ctx.strokeRect(padL, padT, plotW, plotH);

    // tick labels
    ctx.fillStyle = "rgba(207, 220, 246, 0.7)";
    ctx.font = "11px -apple-system, system-ui, sans-serif";
    ctx.textBaseline = "middle"; ctx.textAlign = "right";
    for (const t of yT.ticks) ctx.fillText(fmtTick(t, yT.step), padL - 6, Y(t));
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    for (const t of xT.ticks) ctx.fillText(fmtTick(t, xT.step), X(t), padT + plotH + 6);

    // axis-titles
    ctx.fillStyle = "rgba(207, 220, 246, 0.85)";
    ctx.font = "11px -apple-system, system-ui, sans-serif";
    if (opts.xLabel) {
        ctx.textAlign = "center"; ctx.textBaseline = "alphabetic";
        ctx.fillText(opts.xLabel, padL + plotW / 2, h - 6);
    }
    if (opts.yLabel) {
        ctx.save();
        ctx.translate(14, padT + plotH / 2);
        ctx.rotate(-Math.PI / 2);
        ctx.textAlign = "center"; ctx.textBaseline = "alphabetic";
        ctx.fillText(opts.yLabel, 0, 0);
        ctx.restore();
    }

    // Clip subsequent draws to the plot rect — keeps the line and the
    // frequency marker strictly inside the frame.
    ctx.save();
    ctx.beginPath();
    ctx.rect(padL, padT, plotW, plotH);
    ctx.clip();

    // marker (vertical line at xMark)
    if (opts.xMark != null && opts.xMark >= xMin && opts.xMark <= xMax) {
        const mx = X(opts.xMark);
        ctx.strokeStyle = "rgba(241, 76, 76, 0.7)";
        ctx.setLineDash([4, 3]);
        ctx.beginPath(); ctx.moveTo(mx, padT); ctx.lineTo(mx, padT + plotH); ctx.stroke();
        ctx.setLineDash([]);
    }

    // line
    ctx.strokeStyle = opts.color || "#5b8def";
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    for (let i = 0; i < x.length; i++) {
        const px = X(x[i]), py = Y(y[i]);
        if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    }
    ctx.stroke();
    ctx.restore();
}

// ---- WebSocket transport mirroring usrp_benchmark.client ----
async function sendOverWS({ token, signal, channel, onStatus, onError }) {
    const url = (location.protocol === "https:" ? "wss://" : "ws://") +
                location.host + "/ws/run?auth_token=" + encodeURIComponent(token);
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";

    const result = new Promise((resolve, reject) => {
        ws.onopen = () => {
            // Text handshake selects which inventory channel to test over,
            // then exactly one binary frame with the interleaved I/Q signal.
            ws.send(JSON.stringify({ mode: "siso", channel: channel || 0 }));
            ws.send(signal.buffer);
        };
        ws.onmessage = (ev) => {
            if (typeof ev.data === "string") {
                let info;
                try { info = JSON.parse(ev.data); } catch { return; }
                if (info.error) {
                    onError?.(info);
                    reject(new Error(info.message || info.error));
                    ws.close();
                    return;
                }
                onStatus?.(info);
                return;
            }
            // binary result blob — float32 interleaved I/Q
            const arr = new Float32Array(ev.data);
            resolve(arr);
            ws.close();
        };
        ws.onerror = (e) => reject(new Error("WebSocket error"));
        ws.onclose = (ev) => {
            if (ev.code !== 1000 && ev.code !== 1005) {
                // closed before we got a binary frame
                reject(new Error(`WebSocket closed (code ${ev.code})`));
            }
        };
    });
    return result;
}

// ---- Public entry: run a benchmark and return all the metrics needed ----
async function runBenchmark({ token, toneFreqHz, nSamples, channel, onStatus }) {
    // 1) fetch /info to learn fs / fc / guards
    const infoRes = await fetch(`/info?auth_token=${encodeURIComponent(token)}`);
    if (!infoRes.ok) throw new Error("Could not fetch /info — token invalid?");
    const info = await infoRes.json();

    const fs = Number(info.sample_rate_hz);
    const fc = Number(info.carrier_frequency_hz);
    const guard_s = Number(info.begin_guard_min_sec ?? 0.1);
    const guard_samples = Math.round(guard_s * fs);

    // 2) generate complex sine: tx[n] = exp(j 2π f n / fs)
    const tx = new Float32Array(nSamples * 2);
    const w = 2 * Math.PI * toneFreqHz / fs;
    for (let n = 0; n < nSamples; n++) {
        tx[2 * n]     = Math.cos(w * n);
        tx[2 * n + 1] = Math.sin(w * n);
    }

    // 3) send / receive
    const t0 = performance.now();
    const rx = await sendOverWS({ token, signal: tx, channel, onStatus });
    const elapsed_ms = performance.now() - t0;

    // 4) split RX
    const totalSamples = rx.length / 2;
    // Guards may be randomised between min and max — derive the actual
    // total guard from the recorded sample count and just check it falls
    // inside the legal [min, max] window.
    const begin_min = Number(info.begin_guard_min_sec ?? 0.1);
    const begin_max = Number(info.begin_guard_max_sec ?? begin_min);
    const end_min   = Number(info.end_guard_min_sec   ?? 0.1);
    const end_max   = Number(info.end_guard_max_sec   ?? end_min);
    const total_guard_min_s = begin_min + end_min;
    const total_guard_max_s = begin_max + end_max;
    const total_recorded_s  = totalSamples / fs;
    const total_guard_s     = total_recorded_s - nSamples / fs;

    // rx_core = rx[guard : guard + nSamples]
    const coreRe = new Float32Array(nSamples);
    const coreIm = new Float32Array(nSamples);
    for (let i = 0; i < nSamples; i++) {
        coreRe[i] = rx[2 * (guard_samples + i)];
        coreIm[i] = rx[2 * (guard_samples + i) + 1];
    }

    // 5) FFTs
    const tx_re = new Float32Array(nSamples);
    const tx_im = new Float32Array(nSamples);
    for (let i = 0; i < nSamples; i++) { tx_re[i] = tx[2*i]; tx_im[i] = tx[2*i+1]; }
    const txSpec = spectrum(tx_re, tx_im, fs);
    const rxSpec = spectrum(coreRe, coreIm, fs);

    // 6) Power profile across the entire RX recording
    const NBLOCKS = 40;
    const blockLen = Math.floor(totalSamples / NBLOCKS);
    const profileT = new Float32Array(NBLOCKS);
    const profileP = new Float32Array(NBLOCKS);
    for (let b = 0; b < NBLOCKS; b++) {
        let sum = 0;
        for (let i = b * blockLen; i < (b + 1) * blockLen; i++) {
            const re = rx[2*i], im = rx[2*i+1];
            sum += re * re + im * im;
        }
        const avg = sum / Math.max(1, blockLen);
        profileT[b] = (b * blockLen) / fs * 1000; // ms
        profileP[b] = 10 * Math.log10(avg + 1e-20);
    }

    // 7) Metrics
    let rxMaxAbs = 0, rxPowerSum = 0, rxClips = 0;
    for (let i = 0; i < nSamples; i++) {
        const m2 = coreRe[i]*coreRe[i] + coreIm[i]*coreIm[i];
        rxPowerSum += m2;
        const m = Math.sqrt(m2);
        if (m > rxMaxAbs) rxMaxAbs = m;
        if (m > 0.95) rxClips++;
    }
    const rxAvgPower = rxPowerSum / nSamples;
    const rxAvgPowerDb = 10 * Math.log10(rxAvgPower + 1e-20);
    const txAvgPowerDb = 0; // by construction — magnitude 1

    // SNR estimate: peak power vs median spectrum (noise floor)
    const sortedMag = Array.from(rxSpec.mag_db).sort((a, b) => a - b);
    const noiseFloor = sortedMag[Math.floor(sortedMag.length * 0.5)];
    const snrSpectral = rxSpec.peak_db - noiseFloor;

    const txSampDown = downsamplePlot(txSpec.f_arr, txSpec.mag_db);
    const rxSampDown = downsamplePlot(rxSpec.f_arr, rxSpec.mag_db);

    return {
        ts: Date.now(),
        elapsed_ms,
        info,
        params: { toneFreqHz, nSamples, guard_samples },
        balance: {
            actual_samples: totalSamples,
            actual_total_sec: total_recorded_s,
            actual_guard_sec: total_guard_s,
            guard_min_sec:    total_guard_min_s,
            guard_max_sec:    total_guard_max_s,
            // Tolerate ±2 samples of rounding on each side.
            in_range: total_guard_s >= total_guard_min_s - 2/fs
                   && total_guard_s <= total_guard_max_s + 2/fs,
        },
        tx: { f: txSampDown.f, mag_db: txSampDown.y },
        rx: { f: rxSampDown.f, mag_db: rxSampDown.y },
        peak: {
            target_hz: toneFreqHz,
            measured_hz: rxSpec.peak_freq,
            offset_hz: rxSpec.peak_freq - toneFreqHz,
            peak_db: rxSpec.peak_db,
            snr_db: snrSpectral,
        },
        rx_stats: {
            max_abs: rxMaxAbs,
            avg_power_db: rxAvgPowerDb,
            tx_avg_power_db: txAvgPowerDb,
            clips: rxClips,
        },
        profile: { t_ms: Array.from(profileT), p_db: Array.from(profileP) },
        carrier_hz: fc,
        sample_rate_hz: fs,
    };
}

// ---- Persistence ----
function saveRun(run) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(run)); } catch {}
}
function loadRun() {
    try { const v = localStorage.getItem(STORAGE_KEY); return v ? JSON.parse(v) : null; }
    catch { return null; }
}

window.Benchmark = {
    run: runBenchmark,
    save: saveRun,
    load: loadRun,
    drawLinePlot,
};
