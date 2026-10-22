"""The visitor's real IP address, without trusting headers an attacker can set.

Requests normally arrive Cloudflare -> Railway -> app, so the address the app
sees (request.remote_addr, after ProxyFix) can be a Cloudflare server rather
than the visitor. Cloudflare puts the visitor's address in CF-Connecting-IP.
Anyone can also send requests straight to the Railway address and invent that
header, so it is only believed when the request really came from a Cloudflare
address.
"""
import ipaddress

from flask import request

# Cloudflare's published ranges: https://www.cloudflare.com/ips/ (they change
# rarely; check about once a year).
_CLOUDFLARE_NETS = [
    ipaddress.ip_network(n) for n in (
        '173.245.48.0/20', '103.21.244.0/22', '103.22.200.0/22', '103.31.4.0/22',
        '141.101.64.0/18', '108.162.192.0/18', '190.93.240.0/20', '188.114.96.0/20',
        '197.234.240.0/22', '198.41.128.0/17', '162.158.0.0/15', '104.16.0.0/13',
        '104.24.0.0/14', '172.64.0.0/13', '131.0.72.0/22',
        '2400:cb00::/32', '2606:4700::/32', '2803:f800::/32', '2405:b500::/32',
        '2405:8100::/32', '2a06:98c0::/29', '2c0f:f248::/32',
    )
]


def is_cloudflare_ip(value: str | None) -> bool:
    try:
        ip = ipaddress.ip_address((value or '').strip())
    except ValueError:
        return False
    return any(ip in net for net in _CLOUDFLARE_NETS)


def client_ip() -> str:
    """Best address for rate limits and logs."""
    peer = request.remote_addr or ''
    if is_cloudflare_ip(peer):
        forwarded = (request.headers.get('CF-Connecting-IP') or '').strip()
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return peer or '127.0.0.1'
