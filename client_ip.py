"""The visitor's real IP address, without trusting headers an attacker can set.

Requests arrive Cloudflare -> Railway edge -> app. On production the address
the app sees (request.remote_addr, after ProxyFix) is Railway's edge node
(e.g. 79.127.178.x, CDN77), shared by every visitor -- not Cloudflare and not
the visitor. Keying rate limits on it would put all visitors in one bucket.
Cloudflare puts the visitor's address in CF-Connecting-IP, so that header is
used whenever it holds a valid address.

Trade-off: someone calling the Railway address directly (bypassing
Cloudflare) can invent the header and so dodge per-IP rate limits. That only
loosens limits for an attacker who skips Cloudflare; it never locks real
visitors out. Closing the direct Railway address (or ADMIN_EDGE_SECRET-style
edge checks) is the proper fix for that route.
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
    forwarded = (request.headers.get('CF-Connecting-IP') or '').strip()
    if forwarded:
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return peer or '127.0.0.1'
