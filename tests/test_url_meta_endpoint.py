"""SSRF guard for the URL-preview endpoint and the shared web-fetch validation.

The endpoint reuses ``modus.web.fetch._validate_public_url``. That helper had a
latent bug: ``NetworkPolicyError`` subclasses ``ValueError``, so the
``except ValueError`` guarding the IP-literal branch swallowed the private-IP
rejection and fell through to DNS resolution — ``127.0.0.1`` passed. These tests
pin the fix and the endpoint's behavior.
"""

import pytest

from modus.web.fetch import NetworkPolicyError, _validate_public_url


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:3000/",
    "http://localhost/",
    "http://[::1]/",
    "http://10.0.0.5/x",
    "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/",
    "ftp://example.com/",
])
def test_validate_public_url_rejects_private_and_loopback(url: str) -> None:
    with pytest.raises(NetworkPolicyError):
        _validate_public_url(url)


def test_validate_public_url_allows_public_https() -> None:
    _validate_public_url("https://example.com/path")  # no raise


def test_url_meta_endpoint_rejects_loopback(monkeypatch) -> None:
    from starlette.testclient import TestClient

    from modus.desktop import server

    with TestClient(server.app) as client:
        resp = client.get("/api/url-meta", params={"url": "http://127.0.0.1:3000/"})
        assert resp.status_code == 400
        assert "private or local" in resp.json()["error"]
