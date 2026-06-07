"""Egress connectivity probe for an account's proxy.

Replicates exactly the bot's per-account egress: a request sent through the
account's proxy (or direct, if no proxy) reveals the public IP / ISP that
Instagram would see for that account. Used by the "Testa connessione" button.

Pure/sync (requests) so it can be unit-tested with mocked requests and run in a
thread from the async API layer.
"""
import requests

_IPIFY = "https://api.ipify.org"
_IPAPI = "http://ip-api.com/json/{ip}?fields=query,isp,as,mobile,country,city"


def _classify_error(exc: Exception) -> str:
    name = exc.__class__.__name__
    if "Proxy" in name:
        return ("Proxy non raggiungibile: USB/tethering scollegato, Every Proxy spento, "
                "o IP del proxy cambiato (verifica l'IP del tether).")
    if "Timeout" in name:
        return "Timeout: il proxy non risponde entro il tempo limite (probabilmente giù)."
    if "ConnectionError" in name:
        return "Connessione rifiutata/assente verso l'host del proxy."
    return f"{name}: {str(exc)[:140]}"


def probe_egress(proxy: str | None, timeout: float = 12.0) -> dict:
    """Return the public egress for the given proxy (or direct if proxy is None).

    Result keys:
      ok (bool), via ('proxy'|'direct'), proxy (str|None),
      egress_ip, isp, asn, mobile, country, city (present when ok),
      error (present when not ok).
    """
    via = "proxy" if proxy else "direct"
    proxies = {"http": proxy, "https": proxy} if proxy else None

    try:
        ip = requests.get(_IPIFY, proxies=proxies, timeout=timeout).text.strip()
    except Exception as exc:  # network/proxy failure → report cleanly, no raise
        return {"ok": False, "via": via, "proxy": proxy, "error": _classify_error(exc)}

    result = {
        "ok": True, "via": via, "proxy": proxy, "egress_ip": ip,
        "isp": None, "asn": None, "mobile": None, "country": None, "city": None,
    }
    # Geo/ASN enrichment is best-effort: an IP alone already proves the egress.
    try:
        meta = requests.get(_IPAPI.format(ip=ip), proxies=proxies, timeout=timeout).json()
        result["isp"] = meta.get("isp")
        result["asn"] = meta.get("as")
        result["mobile"] = meta.get("mobile")
        result["country"] = meta.get("country")
        result["city"] = meta.get("city")
    except Exception:
        pass
    return result
