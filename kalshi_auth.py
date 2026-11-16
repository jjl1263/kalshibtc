#!/usr/bin/env python3
"""
kalshi_auth.py -- request signing for the Kalshi API.

Kalshi has no simple API-key header. Every authenticated request carries an
RSA-PSS signature over (timestamp_ms + HTTP_METHOD + path). Market data is
public and needs no signing, so the recorder works without keys -- but you
need keys the moment you place an order, so set them up now.

Env vars:
    KALSHI_KEY_ID       the UUID Kalshi gives you after uploading your public key
    KALSHI_PRIVATE_KEY  path to your private key .pem
"""

import base64
import os
import time

BASE = "https://api.elections.kalshi.com"
API = "/trade-api/v2"


def load_private_key(path=None):
    from cryptography.hazmat.primitives import serialization
    path = path or os.environ.get("KALSHI_PRIVATE_KEY", "kalshi_private_key.pem")
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def sign(private_key, ts_ms, method, path):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    msg = f"{ts_ms}{method}{path}".encode("utf-8")
    sig = private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


def auth_headers(method, path, key_id=None, private_key=None):
    """
    `path` must be the path only -- no query string.
    e.g. auth_headers("GET", "/trade-api/v2/portfolio/balance")
    """
    key_id = key_id or os.environ.get("KALSHI_KEY_ID")
    if not key_id:
        raise RuntimeError("KALSHI_KEY_ID not set")
    private_key = private_key or load_private_key()
    ts = str(int(time.time() * 1000))
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sign(private_key, ts, method, path),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def check():
    """Verify keys work by hitting the balance endpoint."""
    import requests
    path = f"{API}/portfolio/balance"
    r = requests.get(BASE + path, headers=auth_headers("GET", path), timeout=15)
    print(r.status_code, r.text[:400])
    return r.status_code == 200


if __name__ == "__main__":
    ok = check()
    print("AUTH OK" if ok else "AUTH FAILED -- check KALSHI_KEY_ID and key file")