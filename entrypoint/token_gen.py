"""
Deterministic-ish user token generator.

Recipe:
    raw  = secret || eth_id || iso_timestamp || os_random(8)
    hash = SHA-256(raw)
    token = eth_id + "-" + base32(hash[:15])   # 24 chars of entropy, human readable

Das macht:
- An den eth_id gebunden (pseudo-prefix, damit man beim Log direkt sieht wem der Token gehört)
- Nicht raten-bar ohne Secret (OS-Random + Secret)
- Kollision praktisch unmöglich (128 Bit Entropie)
"""
import os
import base64
import hashlib
from datetime import datetime


def _secret() -> bytes:
    s = os.getenv("TOKEN_SECRET", "change-me-in-dotenv")
    return s.encode("utf-8")


def generate_token(eth_id: str) -> str:
    """Return a new token for the given ETH-ID."""
    now = datetime.utcnow().isoformat().encode("utf-8")
    rnd = os.urandom(8)
    raw = _secret() + eth_id.encode("utf-8") + now + rnd
    digest = hashlib.sha256(raw).digest()
    short = base64.b32encode(digest[:15]).decode("ascii").rstrip("=").lower()
    return f"{eth_id}-{short}"
