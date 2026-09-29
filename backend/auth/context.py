from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address, ip_network


@dataclass(frozen=True)
class AuthRequestContext:
    client_ip: str | None = None
    user_agent: str | None = None


def resolve_client_ip(
    *,
    peer: str | None,
    forwarded_for: str | None,
    trusted_proxy_cidrs: tuple[str, ...],
) -> str | None:
    try:
        peer_address = ip_address(peer) if peer else None
    except ValueError:
        return None
    if peer_address is None:
        return None

    trusted_networks = tuple(
        ip_network(cidr, strict=False) for cidr in trusted_proxy_cidrs
    )
    if not any(peer_address in network for network in trusted_networks):
        return str(peer_address)
    if not forwarded_for:
        return str(peer_address)

    forwarded_addresses = []
    try:
        for value in forwarded_for.split(","):
            forwarded_addresses.append(ip_address(value.strip()))
    except ValueError:
        return str(peer_address)

    for address in reversed([*forwarded_addresses, peer_address]):
        if any(address in network for network in trusted_networks):
            continue
        return str(address)
    return str(forwarded_addresses[0]) if forwarded_addresses else str(peer_address)
