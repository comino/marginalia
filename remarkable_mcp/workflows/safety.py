"""Guards for tool parameters that reach the local machine or the network.

The server runs with the user's rights, and its callers are agents that read
untrusted content (web pages, inbox text, documents). A prompt-injected agent
must not be able to use these tools to read secrets (``~/.ssh``, tokens,
``/proc/self/environ``) and move them to the tablet or the web, or to probe
the local network. So:

- local files: regular files only, with an allowed suffix, under a size cap,
  outside sensitive locations (and inside ``REMARKABLE_ALLOWED_ROOTS`` when
  that is set: a colon-separated list of directories);
- URLs: http(s) only; hosts that resolve to loopback, private, link-local or
  otherwise non-public addresses are refused (unless
  ``REMARKABLE_ALLOW_PRIVATE_URLS=1``); redirects are re-checked hop by hop;
  downloads are capped.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from pathlib import Path
from typing import Iterable, Optional, Tuple
from urllib.parse import urljoin, urlparse


class UnsafeInput(ValueError):
    """A parameter was refused for safety reasons (message says why)."""


def _sensitive_roots() -> Tuple[Path, ...]:
    home = Path.home()
    return tuple(
        p.resolve()
        for p in (
            home / ".ssh",
            home / ".gnupg",
            home / ".aws",
            home / ".config",
            home / ".rmapi",
            home / ".netrc",
            home / ".git-credentials",
            home / ".docker",
            home / ".kube",
            home / ".local" / "share" / "keyrings",
            Path("/proc"),
            Path("/sys"),
            Path("/dev"),
            Path("/etc"),
            Path("/root"),
            Path("/var"),
        )
    )


def _allowed_roots() -> Optional[Tuple[Path, ...]]:
    raw = os.environ.get("REMARKABLE_ALLOWED_ROOTS", "").strip()
    if not raw:
        return None
    return tuple(Path(p).expanduser().resolve() for p in raw.split(os.pathsep) if p.strip())


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def check_local_file(
    path: str, suffixes: Iterable[str], max_bytes: int, what: str = "file"
) -> Path:
    """Resolve and vet a local file a tool is asked to read; returns the path."""
    if not path or "\x00" in path:
        raise UnsafeInput(f"No {what} path given.")
    p = Path(path).expanduser()
    try:
        resolved = p.resolve(strict=True)
    except (OSError, RuntimeError):
        raise UnsafeInput(f"No such {what}: {path}") from None
    allowed_suffixes = {s.lower() for s in suffixes}
    if resolved.suffix.lower() not in allowed_suffixes:
        raise UnsafeInput(
            f"Refusing {resolved.name!r}: only "
            f"{', '.join(sorted(allowed_suffixes))} files are accepted."
        )
    if not resolved.is_file():
        raise UnsafeInput(f"Not a regular file: {path}")
    for root in _sensitive_roots():
        if _within(resolved, root):
            raise UnsafeInput(f"Refusing to read from {root} (sensitive location).")
    roots = _allowed_roots()
    if roots is not None and not any(_within(resolved, r) for r in roots):
        raise UnsafeInput(f"{resolved} is outside REMARKABLE_ALLOWED_ROOTS.")
    size = resolved.stat().st_size
    if size > max_bytes:
        raise UnsafeInput(
            f"{resolved.name} is {size // 1024} KB; the limit is {max_bytes // 1024} KB."
        )
    return resolved


def check_local_dir(path: str, what: str = "directory") -> Path:
    if not path or "\x00" in path:
        raise UnsafeInput(f"No {what} given.")
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        raise UnsafeInput(f"No such {what}: {path}") from None
    if not resolved.is_dir():
        raise UnsafeInput(f"Not a directory: {path}")
    for root in _sensitive_roots():
        if _within(resolved, root):
            raise UnsafeInput(f"Refusing to use {root} (sensitive location).")
    roots = _allowed_roots()
    if roots is not None and not any(_within(resolved, r) for r in roots):
        raise UnsafeInput(f"{resolved} is outside REMARKABLE_ALLOWED_ROOTS.")
    return resolved


# --------------------------------------------------------------------------- URLs


# Shared address space (RFC 6598): carrier-grade NAT - and Tailscale's
# tailnet addresses. Not "private" to the ipaddress module, but not public.
_SHARED = ipaddress.ip_network("100.64.0.0/10")


def _public_address(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if addr.version == 4 and addr in _SHARED:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or (addr.version == 6 and addr.ipv4_mapped and not _public_address(str(addr.ipv4_mapped)))
    )


def check_url(url: str) -> str:
    """Refuse non-http(s) URLs and hosts that resolve to non-public addresses."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UnsafeInput("Only http(s) URLs with a host are accepted.")
    if os.environ.get("REMARKABLE_ALLOW_PRIVATE_URLS") == "1":
        return url
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise UnsafeInput(f"Cannot resolve {parsed.hostname}.") from None
    for info in infos:
        ip = info[4][0].split("%", 1)[0]
        if not _public_address(ip):
            raise UnsafeInput(
                f"Refusing {parsed.hostname}: it resolves to a non-public address ({ip})."
            )
    return url


def fetch_public(url: str, headers: dict, timeout: float, max_bytes: int, max_redirects: int = 5):
    """GET a public URL, re-checking every redirect hop, with a size cap.

    Returns the final ``requests.Response`` with ``_content`` filled (capped).
    """
    import requests

    current = check_url(url)
    for _ in range(max_redirects + 1):
        resp = requests.get(
            current, headers=headers, timeout=timeout, allow_redirects=False, stream=True
        )
        if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                raise UnsafeInput("Redirect without a Location header.")
            current = check_url(urljoin(current, location))
            continue
        resp.raise_for_status()
        chunks, total = [], 0
        for chunk in resp.iter_content(64 * 1024):
            total += len(chunk)
            if total > max_bytes:
                resp.close()
                raise UnsafeInput(f"The page is larger than {max_bytes // (1024 * 1024)} MB.")
            chunks.append(chunk)
        resp._content = b"".join(chunks)
        return resp
    raise UnsafeInput("Too many redirects.")
