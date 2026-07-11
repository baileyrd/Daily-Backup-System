"""Detect whether this process runs inside a named Linux network namespace.

``requires_vpn`` sources must back up through the VPN wrapper (``vpn_exec``,
default ``sudo vpn-netns exec``), which runs the command inside a dedicated
network namespace. Launched OUTSIDE that namespace, a backup's traffic exits via
the host's real IP — the exact mistake this module guards against.

Membership is checked the way ``ip netns identify`` does it: the current net
namespace (``/proc/self/ns/net``) and the named-netns bind mount
(``/run/netns/<name>``) share an inode iff this process is inside it. Everything
degrades to a safe "not in the namespace" on non-Linux or when the namespace is
absent, rather than guessing.
"""

from __future__ import annotations

import os

_NETNS_DIR = "/run/netns"


def named_netns_exists(name: str) -> bool:
    """True if a network namespace ``name`` has been created on this host."""
    return bool(name) and os.path.exists(f"{_NETNS_DIR}/{name}")


def in_named_netns(name: str) -> bool:
    """True if this process is currently inside the ``name`` network namespace.

    An empty ``name`` disables the check (returns True). A missing namespace or
    any stat error means we cannot confirm membership, so we return False.
    """
    if not name:
        return True
    try:
        return os.path.samestat(
            os.stat("/proc/self/ns/net"), os.stat(f"{_NETNS_DIR}/{name}")
        )
    except OSError:
        return False


__all__ = ["named_netns_exists", "in_named_netns"]
