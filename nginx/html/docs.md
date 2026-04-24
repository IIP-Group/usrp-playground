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

Das installiert das Python-Package `usrp_benchmark` mit der Klasse `USRPClient` sowie das CLI-Tool `usrp-client`.

Empfohlen: frische virtuelle Umgebung.

---

## 3. Dein Token

Du hast per Mail ein personalisiertes Token bekommen (beginnt mit deinem ETH-Kürzel). Damit authentifizierst du dich am Server.

**Wichtig:** nicht weitergeben. Wenn dein Token weg ist oder missbraucht wird: melde dich beim Assistenten.

---

## 4. Minimal-Beispiel (Python)

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

## 7. CLI-Tool (ohne Python-Code)

Wenn du das Signal aus einer anderen Sprache erzeugst (MATLAB, C, GNU Radio) oder einfach nur schnell ein File durchjagen willst — nach `pip install` steht `usrp-client` im Terminal bereit.

**Format der Datei:** `.f32` — interleaved Float32, Reihenfolge `real, imag, real, imag, ...`. Das ist dasselbe Format das auch GNU Radio und viele SDR-Tools verwenden.

**Aufruf:**

```bash
usrp-client -i input.f32 -o output.f32 \
            -s 129.132.24.210:80 \
            -t DEIN-TOKEN-HIER
```

**Argumente:**

| Flag | Bedeutung | Default |
|---|---|---|
| `-i`, `--input` | Pfad zur Eingabedatei (`.f32`, interleaved IQ) | — (required) |
| `-o`, `--output` | Pfad für die empfangene Datei | `output.f32` |
| `-s`, `--server` | Server-Adresse `host:port` | `localhost:8000` |
| `-t`, `--token` | Auth-Token aus deiner E-Mail | default-bench-token |

Während der Ausführung bekommst du Status-Updates auf der Konsole: `[upload]`, `[queued]`, `[waiting]`, `[running]`, `[done]`, `[result]`.

**Beispiel: F32 aus numpy erzeugen** (falls du in Python ein Signal hast und es via CLI schicken willst):

```python
import numpy as np
sig = np.exp(1j * 2 * np.pi * 1e6 * np.arange(100_000) / 25e6).astype(np.complex64)
raw = np.empty(len(sig)*2, dtype=np.float32)
raw[0::2] = sig.real
raw[1::2] = sig.imag
raw.tofile("input.f32")
```

Und zurücklesen:

```python
import numpy as np
raw = np.fromfile("output.f32", dtype=np.float32)
rx = raw[0::2] + 1j * raw[1::2]
```

**MATLAB:**

```matlab
% schreiben
sig = exp(1i*2*pi*1e6*(0:99999)/25e6);
fid = fopen('input.f32','wb');
fwrite(fid, [real(sig); imag(sig)], 'float32');   % interleaved
fclose(fid);

% lesen
fid = fopen('output.f32','rb');
raw = fread(fid, Inf, 'float32');
fclose(fid);
rx  = raw(1:2:end) + 1i*raw(2:2:end);
```

---

## 8. Häufige Fehler

| Fehler | Bedeutung | Fix |
|---|---|---|
| `auth_failed` | Falsches Token | Token aus der E-Mail nochmal kopieren |
| `queue_full` | Zu viele Tasks in der Queue | Warten, später probieren |
| `file_too_large` | Signal zu gross (>200 MB) | Kleineres Signal oder mehrere kleinere Bursts |
| `SLEEPING ZZZZ` | Server ist gerade ausgeschaltet (Wartung) | Später probieren |
| `Duty cycle limit` | TX-Zeit-Quote aufgebraucht | Server wartet automatisch |
| `Listen Before Talk failed` | Kanal war 10× in Folge belegt | Später probieren, vielleicht ist gerade jemand sehr aktiv |

---

## 9. Fragen / Bugs

GitHub Issues: [github.com/RaresBares/USRP-Benchmark-System/issues](https://github.com/RaresBares/USRP-Benchmark-System/issues)

Oder direkt an den Assistenten (siehe deine Token-Mail).

---

<p class="text-dim">Letzte Änderung: April 2026 · <a href="https://github.com/RaresBares/USRP-Benchmark-System">Source auf GitHub</a></p>
