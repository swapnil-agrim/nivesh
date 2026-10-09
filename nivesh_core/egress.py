"""Egress-IP check (CR-2): compare our public IP with the broker-registered one. Reports, never
retries or blocks."""

import ipaddress
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class EgressResult:
    status: str  # match | mismatch | skipped | error
    ip: str | None
    message: str


def _default_client() -> httpx.Client:
    return httpx.Client(timeout=5.0)


def check_egress(
    registered_ip: str | None, url: str, client: httpx.Client | None = None
) -> EgressResult:
    if not registered_ip:
        return EgressResult("skipped", None, "no registered_ip configured; egress check skipped")
    c = client or _default_client()
    try:
        ip = str(ipaddress.ip_address(c.get(url).raise_for_status().text.strip()))
    except (httpx.HTTPError, ValueError) as e:
        return EgressResult("error", None, f"egress check failed ({type(e).__name__})")
    finally:
        if client is None:
            c.close()
    if ip == str(ipaddress.ip_address(registered_ip)):
        return EgressResult("match", ip, f"egress IP {ip} matches the registered IP")
    return EgressResult(
        "mismatch", ip, f"egress IP {ip} differs from registered IP {registered_ip}"
    )
