# USRP Benchmark — Student Guide

Willkommen! Hier findest du alles was du brauchst um deine Wellenformen über das gemeinsame USRP zu testen.

---

## 1. Was ist das?

Ein gemeinsamer Server, der dein Basisband-Signal über einen echten USRP X410 sendet und das empfangene Signal zurückliefert. Du arbeitest lokal (Python, MATLAB, ...), das Senden/Empfangen übernimmt der Server.

- Zwei USRPs X410, verkabelt mit 10 dB Attenuator
- Master Clock: 250 MHz
- Standard-Sample-Rate: 25 MHz (250 MHz / 10)
- Carrier: 2.4 GHz (konfigurierbar)

---

## 2. Installation

```bash
pip install git+https://github.com/RaresBares/USRP-Benchmark-System.git
```

Das installiert das Python-Package `usrp_benchmark` mit der Klasse `USRPClient`.

Empfohlen: frische virtuelle Umgebung.

---

## 3. Dein Token

Du hast per Mail ein personalisiertes Token bekommen (beginnt mit deinem ETH-Kürzel). Damit authentifizierst du dich am Server.

**Wichtig:** nicht weitergeben. Wenn dein Token weg ist oder missbraucht wird: melde dich beim Assistenten.

---

## 4. Minimal-Beispiel

```python
import numpy as np
from usrp_benchmark import USRPClient

USRPClient.setup(
    host="129.132.24.210",
    port=80,
    token="DEIN-TOKEN-HIER",
)

# Server erreichbar?
print("Server OK:", USRPClient.check())

# Radio-Info abfragen
info = USRPClient.info()
print(f"Sample Rate: {info['sample_rate_hz']/1e6} MHz")
print(f"Carrier:     {info['carrier_frequency_hz']/1e9} GHz")

# Testsignal erzeugen (1 MHz komplexer Sinus)
fs = info['sample_rate_hz']
t  = np.arange(100_000) / fs
tx = np.exp(1j * 2 * np.pi * 1e6 * t).astype(np.complex64)

# Senden & Empfangen
rx = USRPClient.send(tx, verbose=True)
print(f"Empfangen: {len(rx)} Samples")
```

---

## 5. Die wichtigsten Methoden

| Aufruf | Rückgabe |
|---|---|
| `USRPClient.setup(host, port, token)` | — Einmal pro Session |
| `USRPClient.check()` | bool — Server erreichbar? |
| `USRPClient.info()` | dict — Sample Rate, Carrier, Gains, Limits |
| `USRPClient.send(signal)` | numpy array — empfangenes Signal |
| `USRPClient.send(signal, verbose=True)` | numpy array — mit Live-Status-Ausgabe |

`signal` muss ein **komplexes numpy Array** sein (IQ-Samples). Wird intern zu `complex64` konvertiert.

---

## 6. Was passiert mit dem Signal

1. **Upload** — dein Signal geht als Binary an den Server
2. **Queue** — falls andere vor dir dran sind, wartest du
3. **Duty Cycle Check** — der Server schaut dass wir unter der TX-Zeit-Quote bleiben (10% / 60s Fenster)
4. **Listen Before Talk** — kurzer RX-Check: ist der Kanal frei? Wenn nicht: Backoff und nochmal probieren
5. **TX + RX gleichzeitig** — das Signal wird gesendet, gleichzeitig wird empfangen
6. **Download** — das empfangene Signal kommt zurück als Binary

Das Ergebnis enthält **Guard-Regionen** vor und nach deinem Signal (ca. 100 ms bei Standard-Settings). Der Grund: damit du garantiert den gesamten Burst empfängst, auch bei kleinen Timing-Abweichungen.

---

## 7. Guard-Regionen abschneiden

Wenn du nur den Teil während aktiver TX willst:

```python
fs = 25e6
guard_s = 0.1  # default begin_guard
guard_samples = int(guard_s * fs)

rx_core = rx[guard_samples : guard_samples + len(tx)]
```

Für **exaktes** Alignment verwende Cross-Correlation:

```python
from scipy.signal import correlate
corr = correlate(rx, tx, mode='valid')
peak = np.argmax(np.abs(corr))
rx_aligned = rx[peak : peak + len(tx)]
```

---

## 8. Plot-Helper

```python
import matplotlib.pyplot as plt

def plot_signal(signal, fs, title="Signal", n_time=500):
    signal = np.asarray(signal); n = len(signal); t = np.arange(n) / fs
    fig, axes = plt.subplots(1, 3, figsize=(15, 4))
    fig.suptitle(title)

    m = min(n_time, n)
    axes[0].plot(t[:m]*1e6, signal[:m].real, label="I")
    axes[0].plot(t[:m]*1e6, signal[:m].imag, label="Q", linestyle="--")
    axes[0].set_xlabel("Zeit [µs]"); axes[0].legend(); axes[0].grid(True)
    axes[0].set_title(f"Zeitbereich ({m} Samples)")

    axes[1].plot(signal[:m].real, signal[:m].imag, linewidth=0.8)
    axes[1].set_xlabel("I"); axes[1].set_ylabel("Q"); axes[1].axis("equal"); axes[1].grid(True)
    axes[1].set_title("Komplexe Ebene")

    sp = np.fft.fftshift(np.fft.fft(signal))
    fr = np.fft.fftshift(np.fft.fftfreq(n, d=1/fs))
    axes[2].plot(fr/1e6, 20*np.log10(np.abs(sp) + 1e-12))
    axes[2].set_xlabel("Frequenz [MHz]"); axes[2].set_ylabel("Magnitude [dB]")
    axes[2].set_title("Spektrum"); axes[2].grid(True)

    plt.tight_layout(); plt.show()

plot_signal(rx, fs, title="RX Signal")
```

---

## 9. Häufige Fehler

| Fehler | Bedeutung | Fix |
|---|---|---|
| `auth_failed` | Falsches Token | Token aus der E-Mail nochmal kopieren |
| `queue_full` | Zu viele Tasks in der Queue | Warten, später probieren |
| `file_too_large` | Signal zu gross (>200 MB) | Kleineres Signal oder mehrere kleinere Bursts |
| `SLEEPING ZZZZ` | Server ist gerade ausgeschaltet (Wartung) | Später probieren |
| `Duty cycle limit` | TX-Zeit-Quote aufgebraucht | Server wartet automatisch |
| `Listen Before Talk failed` | Kanal war 10× in Folge belegt | Später probieren, vielleicht ist gerade jemand sehr aktiv |

---

## 10. In Jupyter / IPython

`USRPClient.send` funktioniert auch in Jupyter (Event-Loop wird automatisch gehandhabt). Wenn ein Fehler kommt, Kernel einmal neu starten.

---

## 11. Fragen / Bugs

GitHub Issues: [github.com/RaresBares/USRP-Benchmark-System/issues](https://github.com/RaresBares/USRP-Benchmark-System/issues)

Oder direkt an den Assistenten (siehe deine Token-Mail).

---

<p class="text-dim">Letzte Änderung: April 2026 · <a href="https://github.com/RaresBares/USRP-Benchmark-System">Source auf GitHub</a></p>
