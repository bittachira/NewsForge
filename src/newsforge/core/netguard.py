"""OPS security — SSRF guard for outbound HTTP fetches.

A single choke-point for every external fetch NewsForge performs. The guard runs
AFTER DNS resolution (a hostname is allowed only if NONE of its resolved addresses
is loopback/private/link-local/metadata) and is re-run on every redirect hop, so a
public URL can never tunnel a request into an internal network.

Nothing here trusts string blocklists (e.g. ``"localhost"``): classification is
always done on the resolved IP literals via :mod:`ipaddress`. Legitimate public
fetching is unaffected — only loopback, RFC1918/CGNAT, link-local (incl. the cloud
metadata address 169.254.169.254), multicast and other non-global networks are
refused. Non-http(s) schemes are rejected outright.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

# Networks NewsForge must never contact. An explicit list keeps classification
# stable across Python versions (ipaddress.is_private / is_global semantics moved
# between 3.9 and 3.13).
_BLOCKED_NETS: tuple[ipaddress._BaseNetwork, ...] = (
    # IPv4
    ipaddress.ip_network("0.0.0.0/8"),        # "this network"
    ipaddress.ip_network("10.0.0.0/8"),       # RFC1918
    ipaddress.ip_network("100.64.0.0/10"),    # CGNAT (RFC6598)
    ipaddress.ip_network("127.0.0.0/8"),      # loopback
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),    # RFC1918
    ipaddress.ip_network("192.0.0.0/24"),     # IETF protocol assignments
    ipaddress.ip_network("192.0.2.0/24"),     # TEST-NET-1
    ipaddress.ip_network("192.168.0.0/16"),   # RFC1918
    ipaddress.ip_network("198.18.0.0/15"),    # benchmarking
    ipaddress.ip_network("198.51.100.0/24"),  # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),   # TEST-NET-3
    ipaddress.ip_network("224.0.0.0/4"),      # multicast
    ipaddress.ip_network("240.0.0.0/4"),      # reserved
    ipaddress.ip_network("255.255.255.255/32"),
    # IPv6
    ipaddress.ip_network("::/128"),           # unspecified
    ipaddress.ip_network("::1/128"),          # loopback
    ipaddress.ip_network("64:ff9b:1::/48"),   # local-use NAT64
    ipaddress.ip_network("100::/64"),         # discard-only
    ipaddress.ip_network("2001:db8::/32"),    # documentation
    ipaddress.ip_network("fc00::/7"),         # unique-local (private)
    ipaddress.ip_network("fe80::/10"),        # link-local
    ipaddress.ip_network("ff00::/8"),         # multicast
)


class SSRFError(RuntimeError):
    """The target resolves to a network NewsForge refuses to contact."""


def is_blocked_ip(ip_literal: str | None) -> bool:
    """True iff ``ip_literal`` belongs to a non-public network.

    IPv4-mapped IPv6 (``::ffff:127.0.0.1``) is unwrapped and evaluated as IPv4.
    Unparseable literals are refused (fail closed: better to drop a fetch than to
    reach an unclassified network)."""
    raw = (ip_literal or "").split("%", 1)[0].strip()
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return True
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    for net in _BLOCKED_NETS:
        try:
            if addr in net:
                return True
        except TypeError:
            continue
    return False


async def _resolve_host(host: str, port: int) -> tuple[str, ...]:
    """Resolve ``host`` to every candidate address literal (A/AAAA), deduplicated."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:  # DNS failure / NXDOMAIN
        return ()
    return tuple({str(info[4][0]) for info in infos})


async def assert_public_target(url: str) -> None:
    """Raise :class:`SSRFError` unless ``url`` is http(s) AND resolves only to public IPs.

    DNS is resolved *here* — the hostname is never trusted as a bare string — so
    ``localhost``, ``127.0.0.1``, RFC1918 names, ``169.254.169.254``, IPv6 unique-local
    / link-local hosts and any host that (re-)resolves to a private address are all
    refused. Literal public IPs bypass DNS and are allowed."""
    parsed = urlsplit(str(url))
    if parsed.scheme not in ("http", "https"):
        raise SSRFError(f"refusing non-http(s) scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise SSRFError("refusing target without a host")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise SSRFError("refusing target with an invalid port") from None

    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        # Literal address: DNS already happened — classify the address directly.
        if is_blocked_ip(host):
            raise SSRFError(f"refusing non-public address: {host}")
        return

    addrs = await _resolve_host(host, port)
    if not addrs:
        raise SSRFError(f"refusing unresolvable host: {host}")
    for addr in sorted(addrs):
        if is_blocked_ip(addr):
            raise SSRFError(f"host {host!r} resolves to a non-public address: {addr}")