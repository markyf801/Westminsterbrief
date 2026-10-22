from flask import Flask

from client_ip import client_ip, is_cloudflare_ip


def _ip(remote_addr, cf_header=None):
    headers = {'CF-Connecting-IP': cf_header} if cf_header is not None else {}
    with Flask(__name__).test_request_context('/', headers=headers,
                                              environ_base={'REMOTE_ADDR': remote_addr}):
        return client_ip()


def test_cloudflare_ranges():
    assert is_cloudflare_ip('172.70.1.2')
    assert is_cloudflare_ip('2606:4700::1')
    assert not is_cloudflare_ip('81.2.69.160')
    assert not is_cloudflare_ip('not an ip')
    assert not is_cloudflare_ip(None)


def test_header_believed_from_cloudflare():
    assert _ip('172.70.1.2', '81.2.69.160') == '81.2.69.160'


def test_header_believed_behind_railway_edge():
    # Production: the peer is Railway's shared edge node, not Cloudflare. The
    # visitor must still get their own bucket, not share the edge's.
    assert _ip('79.127.178.81', '81.2.69.160') == '81.2.69.160'
    assert _ip('79.127.178.81', '2a02:6b67:d7f4:d701::1') == '2a02:6b67:d7f4:d701::1'


def test_no_header_uses_peer():
    assert _ip('203.0.113.9') == '203.0.113.9'


def test_bad_header_falls_back_to_peer():
    assert _ip('172.70.1.2', 'nonsense') == '172.70.1.2'
    assert _ip('172.70.1.2') == '172.70.1.2'
