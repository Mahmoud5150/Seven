"""Web Bot Auth: our signatures must verify exactly the way a store checks them."""

import base64
import json
import re

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from seven.botauth import Signer, jwk_thumbprint, signer_from_env
from seven.http import PoliteClient


def test_keyid_matches_rfc8037_example():
    # RFC 8037 Appendix A.3: the published thumbprint of its example Ed25519 key.
    jwk = {"kty": "OKP", "crv": "Ed25519", "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"}
    assert jwk_thumbprint(jwk) == "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k"


def verify(signer: Signer, headers: dict, authority: str) -> bool:
    """Rebuild the signature base from the headers and check it with the PUBLIC key only."""
    params = headers["Signature-Input"].removeprefix("sig1=")
    sig = base64.b64decode(re.fullmatch(r"sig1=:(.+):", headers["Signature"]).group(1))
    if params.startswith('("@authority";req)'):
        base = f'"@authority";req: {authority}\n"@signature-params": {params}'
    else:
        base = (f'"@authority": {authority}\n"signature-agent": {headers["Signature-Agent"]}\n'
                f'"@signature-params": {params}')
    try:
        signer.key.public_key().verify(sig, base.encode())
        return True
    except Exception:
        return False


def test_request_signature_verifies_and_is_bound_to_the_host():
    s = Signer(Ed25519PrivateKey.generate(), "https://seven.albahrani.dev/")
    h = s.request_headers("alephksa.com", now=1_800_000_000)
    assert h["Signature-Agent"] == '"https://seven.albahrani.dev"'
    assert h["Signature-Input"].startswith('sig1=("@authority" "signature-agent");created=1800000000;')
    assert f'keyid="{s.keyid}"' in h["Signature-Input"] and 'tag="web-bot-auth"' in h["Signature-Input"]
    assert "expires=1800000060" in h["Signature-Input"]
    assert verify(s, h, "alephksa.com")
    assert not verify(s, h, "gazzaz.com.sa")                   # can't be replayed on another store


def test_client_signs_every_request_including_redirect_hops():
    s = Signer(Ed25519PrivateKey.generate(), "https://seven.example")
    seen = []

    def net(req):
        seen.append(req)
        if req.url.host == "old.sa":
            return httpx.Response(301, headers={"Location": "https://new.sa/"})
        return httpx.Response(200, text="ok")

    c = PoliteClient(min_interval=0, transport=httpx.MockTransport(net), signer=s, group_intervals={})
    c.respect_robots = False
    c.get("https://old.sa/")
    assert [r.url.host for r in seen] == ["old.sa", "new.sa"]
    assert all(verify(s, dict(r.headers.items()) | {"Signature-Agent": r.headers["signature-agent"],
                                                     "Signature-Input": r.headers["signature-input"],
                                                     "Signature": r.headers["signature"]}, r.url.host)
               for r in seen)


def test_unsigned_without_key_or_public_https_domain(tmp_path):
    key = tmp_path / "k.pem"
    Signer.create_key_file(str(key))
    assert signer_from_env({}) is None
    assert signer_from_env({"BOT_AUTH_KEY": str(key), "PUBLIC_BASE_URL": "http://localhost:8000"}) is None
    s = signer_from_env({"BOT_AUTH_KEY": str(key), "PUBLIC_BASE_URL": "https://seven.example"})
    assert s is not None and "d" not in s.jwk                     # never publish the private part
    assert PoliteClient(min_interval=0).signer is None             # default: no signing


def test_key_directory_is_published_and_signed(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from seven.web.app import app
    monkeypatch.setenv("DATABASE_URL", str(tmp_path / "w.db"))
    c = TestClient(app)
    assert c.get("/.well-known/http-message-signatures-directory").status_code == 404   # no key yet
    key = tmp_path / "k.pem"
    Signer.create_key_file(str(key))
    monkeypatch.setenv("BOT_AUTH_KEY", str(key))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://seven.example")
    r = c.get("/.well-known/http-message-signatures-directory", headers={"host": "seven.example"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/http-message-signatures-directory+json")
    jwk = json.loads(r.text)["keys"][0]
    s = Signer.from_file(str(key), "https://seven.example")
    assert jwk == s.jwk and 'tag="http-message-signatures-directory"' in r.headers["signature-input"]
    assert verify(s, {"Signature-Input": r.headers["signature-input"], "Signature": r.headers["signature"]},
                  "seven.example")


def test_botkey_command_creates_the_key_once(tmp_path, capsys, monkeypatch):
    from seven.cli import main
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://seven.example")
    path = tmp_path / "secrets" / "bot_key.pem"
    main(["botkey", "--path", str(path)])
    first = capsys.readouterr().out
    assert path.exists() and "created" in first and "keyid:" in first
    main(["botkey", "--path", str(path)])
    assert "created" not in capsys.readouterr().out               # never overwrites the key
