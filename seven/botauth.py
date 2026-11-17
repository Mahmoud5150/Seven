"""Web Bot Auth: cryptographically sign our requests so stores can tell who we are.

Shopify throttles unsigned bots most strictly (we were blocked for 8+ hours after
~25 requests). Its agent page asks server-side bots to sign requests with Web Bot
Auth; signed bots get higher limits. No Cloudflare enrollment needed.

How it works (RFC 9421 HTTP Message Signatures, Cloudflare's Web Bot Auth profile):
  1. We own an Ed25519 key pair. The PRIVATE key stays on our server (a .pem file).
  2. Our PUBLIC key is published at
       https://<our domain>/.well-known/http-message-signatures-directory
     (served by seven/web/app.py), so anyone can check our signatures.
  3. Every outgoing request gets three headers:
       Signature-Agent: "https://<our domain>"              <- where to find our key
       Signature-Input: sig1=("@authority" "signature-agent");created=..;keyid=..;...
       Signature:       sig1=:<base64 Ed25519 signature>:
     The signature covers the target host and our Signature-Agent value, so it
     can't be replayed against another site or claimed by someone else.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SIGNATURE_LIFETIME = 60          # seconds; short expiry limits replay (Cloudflare: ~a minute is enough)


def b64url(raw: bytes) -> str:
    """base64url without '=' padding, as JWK and Web Bot Auth require."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def jwk_thumbprint(jwk: dict) -> str:
    """RFC 7638 thumbprint: SHA-256 of the key's required members, sorted, no spaces.
    For Ed25519 (RFC 8037) those members are crv, kty, x. This is our `keyid`."""
    canonical = json.dumps({"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"]},
                           separators=(",", ":"), sort_keys=True)
    return b64url(hashlib.sha256(canonical.encode()).digest())


class Signer:
    def __init__(self, private_key: Ed25519PrivateKey, agent_url: str) -> None:
        self.key = private_key
        self.agent_url = agent_url.rstrip("/")
        raw_pub = private_key.public_key().public_bytes(serialization.Encoding.Raw,
                                                        serialization.PublicFormat.Raw)
        # The public half as a JWK. Never put the private part ("d") in here.
        self.jwk = {"kty": "OKP", "crv": "Ed25519", "x": b64url(raw_pub)}
        self.keyid = jwk_thumbprint(self.jwk)

    # ---------- loading / creating the key ----------
    @classmethod
    def from_file(cls, path: str, agent_url: str) -> "Signer":
        key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
        return cls(key, agent_url)

    @staticmethod
    def create_key_file(path: str) -> None:
        """Make a new private key file, readable only by its owner. Never commit it."""
        pem = Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        Path(path).write_bytes(pem)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass                                       # Windows: permissions work differently

    # ---------- signing ----------
    def _params(self, components: str, tag: str, now: int) -> str:
        # Only parameters Cloudflare supports: created, keyid, alg, expires, nonce, tag.
        nonce = base64.b64encode(secrets.token_bytes(32)).decode()
        return (f'{components};created={now};keyid="{self.keyid}";alg="ed25519"'
                f';expires={now + SIGNATURE_LIFETIME};nonce="{nonce}";tag="{tag}"')

    def _sign(self, base: str) -> str:
        return base64.b64encode(self.key.sign(base.encode("ascii"))).decode()

    def request_headers(self, authority: str, now: int | None = None) -> dict[str, str]:
        """Headers for one outgoing request to `authority` (host, or host:port)."""
        now = int(time.time()) if now is None else now
        agent = f'"{self.agent_url}"'                  # a structured-field string: quotes included
        params = self._params('("@authority" "signature-agent")', "web-bot-auth", now)
        # The signature base (RFC 9421 section 2.5): one line per signed component, then the params.
        base = (f'"@authority": {authority.lower()}\n'
                f'"signature-agent": {agent}\n'
                f'"@signature-params": {params}')
        return {"Signature-Agent": agent, "Signature-Input": f"sig1={params}",
                "Signature": f"sig1=:{self._sign(base)}:"}

    def directory(self, authority: str, now: int | None = None) -> tuple[str, dict[str, str]]:
        """(body, headers) for our public key directory. The response is itself signed,
        so a verifier knows the directory really belongs to the key holder."""
        now = int(time.time()) if now is None else now
        params = self._params('("@authority";req)', "http-message-signatures-directory", now)
        base = f'"@authority";req: {authority.lower()}\n"@signature-params": {params}'
        headers = {"Content-Type": "application/http-message-signatures-directory+json",
                   "Cache-Control": "max-age=86400",
                   "Signature-Input": f"sig1={params}", "Signature": f"sig1=:{self._sign(base)}:"}
        return json.dumps({"keys": [self.jwk]}), headers


def signer_from_env(env: dict | None = None) -> Signer | None:
    """BOT_AUTH_KEY = path to our private key, PUBLIC_BASE_URL = our https domain.
    Both are needed: without a public domain nobody can fetch our key, so we don't sign."""
    env = os.environ if env is None else env
    path, base = env.get("BOT_AUTH_KEY"), env.get("PUBLIC_BASE_URL", "")
    if not path or not base.startswith("https://") or not Path(path).exists():
        return None
    return Signer.from_file(path, base)
