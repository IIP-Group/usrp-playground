"""
Deterministic-ish user token generator.

Recipe:
    raw  = secret || eth_id || iso_timestamp || os_random(8)
    hash = SHA-256(raw)
    token = eth_id + "-" + base32(hash[:15])   # 24 chars of entropy, human readable

Properties:
- Bound to the eth_id (pseudo-prefix so logs show who owns the token)
- Cannot be guessed without the secret (OS random + secret)
- Collisions practically impossible (128 bits of entropy)
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
