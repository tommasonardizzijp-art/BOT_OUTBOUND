"""Unit tests per il probe egress per-account (pulsante Testa connessione)."""
import requests

import app.utils.proxy_probe as pp


class _Resp:
    def __init__(self, text=None, payload=None):
        self._text = text
        self._payload = payload

    @property
    def text(self):
        return self._text

    def json(self):
        return self._payload


def test_direct_egress_no_proxy(monkeypatch):
    calls = []

    def fake_get(url, proxies=None, timeout=0):
        calls.append((url, proxies))
        if "ipify" in url:
            return _Resp(text="1.2.3.4")
        return _Resp(payload={"query": "1.2.3.4", "isp": "Acme Fixed", "as": "AS1", "mobile": False})

    monkeypatch.setattr(pp.requests, "get", fake_get)
    r = pp.probe_egress(None)

    assert r["ok"] is True
    assert r["via"] == "direct"
    assert r["egress_ip"] == "1.2.3.4"
    assert r["mobile"] is False
    assert r["isp"] == "Acme Fixed"
    assert calls[0][1] is None  # nessun proxy passato


def test_proxy_passed_and_mobile(monkeypatch):
    seen = {}

    def fake_get(url, proxies=None, timeout=0):
        seen["proxies"] = proxies
        if "ipify" in url:
            return _Resp(text="2.195.139.187")
        return _Resp(payload={"query": "2.195.139.187", "isp": "Telecom Italia Mobile",
                              "as": "AS16232", "mobile": True})

    monkeypatch.setattr(pp.requests, "get", fake_get)
    r = pp.probe_egress("http://10.0.0.1:8080")

    assert r["ok"] is True
    assert r["via"] == "proxy"
    assert r["egress_ip"] == "2.195.139.187"
    assert r["mobile"] is True
    assert r["asn"] == "AS16232"
    assert seen["proxies"] == {"http": "http://10.0.0.1:8080", "https": "http://10.0.0.1:8080"}


def test_proxy_down_returns_clean_error(monkeypatch):
    def fake_get(url, proxies=None, timeout=0):
        raise requests.exceptions.ProxyError("cannot connect to proxy")

    monkeypatch.setattr(pp.requests, "get", fake_get)
    r = pp.probe_egress("http://10.0.0.1:8080")

    assert r["ok"] is False
    assert r["via"] == "proxy"
    assert "Proxy non raggiungibile" in r["error"]


def test_geo_failure_still_returns_ip(monkeypatch):
    def fake_get(url, proxies=None, timeout=0):
        if "ipify" in url:
            return _Resp(text="5.5.5.5")
        raise requests.exceptions.Timeout("ip-api slow")

    monkeypatch.setattr(pp.requests, "get", fake_get)
    r = pp.probe_egress(None)

    assert r["ok"] is True
    assert r["egress_ip"] == "5.5.5.5"
    assert r["isp"] is None  # geo best-effort
