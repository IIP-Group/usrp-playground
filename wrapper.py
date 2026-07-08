"""
Einfacher Wrapper um die USRPClient-API.

Benutzung:

    import numpy as np
    from wrapper import send_and_receive

    tx = np.exp(1j * 2 * np.pi * 1e6 * np.arange(100000) / 25e6).astype(np.complex64)
    rx = send_and_receive(tx)
"""
import numpy as np
from usrp_benchmark import USRPClient


_initialized = False


def _init():
    global _initialized
    if _initialized:
        return
    USRPClient.setup(
        host="localhost",
        port=8000,
        token="default-bench-token-2024",
    )
    _initialized = True


def send_and_receive(signal: np.ndarray, channel: int = 0,
                     verbose: bool = False) -> np.ndarray:
    """
    Sendet ein komplexes Basisband-Signal über den USRP und gibt das empfangene
    Basisband-Signal zurück.

    Args:
        signal:  Komplexes numpy Array (IQ-Samples, wird zu complex64 konvertiert)
        channel: Hardware-Kanal fuer 1-D (SISO) Signale (Index, Standard 0)
        verbose: True → Live-Status in der Konsole

    Returns:
        Empfangenes komplexes Basisband-Signal als numpy Array (complex64)
    """
    _init()
    return USRPClient.send(np.asarray(signal, dtype=np.complex64),
                           channel=channel, verbose=verbose)


if __name__ == "__main__":
    fs = 25_000_000
    n = 100_000
    t = np.arange(n) / fs
    tx = np.exp(1j * 2 * np.pi * 1e6 * t).astype(np.complex64)

    rx = send_and_receive(tx, verbose=True)

    print(f"TX: {len(tx)} samples, RX: {len(rx)} samples")
