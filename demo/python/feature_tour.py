"""Feature-Tour: probiert alle Client-Funktionen durch und plottet alles.

Testet der Reihe nach:
  1. send()            - SISO, Legacy-Pfad (kein Handshake)
  2. send_siso(ch=0/1) - SISO mit Channel-Picker
  3. send_mimo()       - 2 Kanaele gleichzeitig (Ton pro Kanal verschieden,
                         damit man sie im Spektrum auseinanderhalten kann)
  4. listen_siso()     - nur empfangen, waehlbarer Kanal
  5. listen_mimo()     - nur empfangen, alle Kanaele

Jeder Test wird einzeln gefangen: was der Server (noch) nicht kann,
erscheint als FAIL in der Zusammenfassung, der Rest laeuft weiter.
Am Ende: ein Plot pro Test (Leistungs-Envelope + Spektrum).

Benutzung:  python feature_tour.py [--save tour.png]
"""
import sys
import argparse
import numpy as np
import matplotlib.pyplot as plt

from usrp_benchmark import USRPClient

HOST = "129.132.24.210"
PORT = 80
TOKEN = "gschwan-arxqpos5vxy642jlbstcmg2k"

N_SEND = 100_000          # Samples pro TX-Signal
N_LISTEN = 200_000        # Samples fuer Listen-Tests
F_TONE_CH0 = 100e3        # Ton Kanal 0 (SISO nutzt auch diesen)
F_TONE_CH1 = -150e3       # Ton Kanal 1 (negativ -> im Spektrum links)


def make_tone(n, fs, f):
    t = np.arange(n) / fs
    return (0.8 * np.exp(2j * np.pi * f * t)).astype(np.complex64)


def run_test(name, fn, results):
    print(f"--- {name} ...", flush=True)
    try:
        rx = fn()
        shape = np.asarray(rx).shape
        print(f"    OK   rx shape={shape}")
        results.append((name, rx, None))
    except Exception as e:
        print(f"    FAIL {e}")
        results.append((name, None, str(e)))


def plot_result(ax_env, ax_spec, rx, fs):
    """Envelope (geglaettete Leistung, dB) + Spektrum, pro Kanal eine Linie."""
    rx = np.asarray(rx)
    chans = [rx] if rx.ndim == 1 else [rx[:, i] for i in range(rx.shape[1])]
    for i, x in enumerate(chans):
        label = f"ch{i}" if len(chans) > 1 else None
        # Envelope: 1-ms-Glaettung, dB
        k = max(int(fs / 1000), 1)
        env = np.convolve(np.abs(x) ** 2, np.ones(k) / k, mode="same")
        t_ms = np.arange(len(x)) / fs * 1e3
        ax_env.plot(t_ms, 10 * np.log10(env + 1e-15), lw=0.8, label=label)
        # Spektrum: Hanning-FFT, auf Peak normiert
        w = np.hanning(len(x))
        spec = np.abs(np.fft.fftshift(np.fft.fft(x * w)))
        freqs = np.fft.fftshift(np.fft.fftfreq(len(x), d=1 / fs))
        s_db = 20 * np.log10(spec + 1e-15)
        ax_spec.plot(freqs / 1e3, s_db - s_db.max(), lw=0.8, label=label)
    ax_env.set_xlabel("Zeit [ms]")
    ax_env.set_ylabel("Leistung [dB]")
    ax_spec.set_xlabel("Frequenz-Offset [kHz]")
    ax_spec.set_ylabel("Magnitude [dB]")
    ax_spec.set_ylim(-90, 5)
    if len(chans) > 1:
        ax_env.legend(loc="upper right", fontsize=8)
        ax_spec.legend(loc="upper right", fontsize=8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", help="Plot als PNG speichern statt anzeigen")
    args = ap.parse_args()

    USRPClient.setup(host=HOST, port=PORT, token=TOKEN)

    print("Server OK:", USRPClient.check())
    info = USRPClient.info()
    fs = info["sample_rate_hz"]
    n_hw = len(info.get("channels", [])) or 1
    print(f"fs={fs/1e6:.1f} MHz | MIMO={info.get('mimo_enabled')} | "
          f"{n_hw} Kanal/Kanaele | Guard "
          f"{info.get('begin_guard_min_sec')}s/{info.get('end_guard_min_sec')}s")

    tone0 = make_tone(N_SEND, fs, F_TONE_CH0)
    tone1 = make_tone(N_SEND, fs, F_TONE_CH1)
    mimo_tx = np.stack([tone0, tone1], axis=1)

    results = []
    run_test("send() SISO legacy", lambda: USRPClient.send(tone0), results)
    run_test("send_siso(channel=0)",
             lambda: USRPClient.send_siso(tone0, channel=0), results)
    if n_hw > 1:
        run_test("send_siso(channel=1)",
                 lambda: USRPClient.send_siso(tone0, channel=1), results)
        run_test("send_mimo() 2 Kanaele (ch0: +100 kHz, ch1: -150 kHz)",
                 lambda: USRPClient.send_mimo(mimo_tx), results)
    run_test(f"listen_siso({N_LISTEN}, channel=0)",
             lambda: USRPClient.listen_siso(N_LISTEN, channel=0), results)
    if n_hw > 1:
        run_test(f"listen_siso({N_LISTEN}, channel=1)",
                 lambda: USRPClient.listen_siso(N_LISTEN, channel=1), results)
        run_test(f"listen_mimo({N_LISTEN})",
                 lambda: USRPClient.listen_mimo(N_LISTEN), results)

    # ---- Zusammenfassung -------------------------------------------------
    print("\n=== Zusammenfassung ===")
    for name, rx, err in results:
        print(f"  {'OK  ' if err is None else 'FAIL'}  {name}"
              + (f"  ({err})" if err else ""))

    # ---- Plots -----------------------------------------------------------
    ok = [(n, rx) for n, rx, e in results if e is None]
    if not ok:
        print("Nichts zu plotten - alle Tests fehlgeschlagen.")
        sys.exit(1)
    fig, axes = plt.subplots(len(ok), 2, figsize=(13, 2.6 * len(ok)),
                             squeeze=False)
    for row, (name, rx) in enumerate(ok):
        plot_result(axes[row][0], axes[row][1], rx, fs)
        axes[row][0].set_title(f"{name} - Envelope", fontsize=9, loc="left")
        axes[row][1].set_title("Spektrum", fontsize=9, loc="left")
    fig.tight_layout()
    if args.save:
        fig.savefig(args.save, dpi=110)
        print(f"Plot gespeichert: {args.save}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
