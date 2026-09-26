"""Tool parameters that reach the local machine or network are vetted."""

import asyncio
import socket
from pathlib import Path

import pytest

from remarkable_mcp.workflows import safety
from remarkable_mcp.workflows.safety import UnsafeInput, check_local_file, check_url
from test_workflows import _fake_path, _json_of, cloud  # noqa: F401


@pytest.fixture
def home(tmp_path, monkeypatch):
    fake = tmp_path / "home"
    (fake / ".ssh").mkdir(parents=True)
    (fake / ".ssh" / "notes.md").write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    (fake / "blog").mkdir()
    (fake / "blog" / "post.md").write_text("# Post\n\nText.\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake))
    return fake


def test_drafts_are_accepted(home):
    assert check_local_file(str(home / "blog" / "post.md"), (".md",), 1000).name == "post.md"


@pytest.mark.parametrize(
    "target",
    [
        ".ssh/notes.md",  # sensitive directory, even with an allowed suffix
        "/proc/self/environ",  # process environment (API keys)
        "/etc/passwd",
        "blog/missing.md",
        "blog",  # a directory
    ],
)
def test_sensitive_or_bogus_paths_are_refused(home, target):
    path = target if target.startswith("/") else str(home / target)
    with pytest.raises(UnsafeInput):
        check_local_file(path, (".md", "", ".txt"), 10_000)


def test_wrong_suffix_and_size(home):
    with pytest.raises(UnsafeInput):
        check_local_file(str(home / "blog" / "post.md"), (".pdf",), 10_000)
    with pytest.raises(UnsafeInput):
        check_local_file(str(home / "blog" / "post.md"), (".md",), 3)


def test_symlink_into_a_secret_is_refused(home):
    link = home / "blog" / "innocent.md"
    link.symlink_to(home / ".ssh" / "notes.md")
    with pytest.raises(UnsafeInput):
        check_local_file(str(link), (".md",), 10_000)


def test_allowed_roots(home, monkeypatch):
    monkeypatch.setenv("REMARKABLE_ALLOWED_ROOTS", str(home / "elsewhere"))
    with pytest.raises(UnsafeInput):
        check_local_file(str(home / "blog" / "post.md"), (".md",), 10_000)
    monkeypatch.setenv("REMARKABLE_ALLOWED_ROOTS", str(home / "blog"))
    assert check_local_file(str(home / "blog" / "post.md"), (".md",), 10_000)


def _resolve_to(monkeypatch, ip):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80))],
    )


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)", "http:///x"]
)
def test_non_http_urls_are_refused(url):
    with pytest.raises(UnsafeInput):
        check_url(url)


@pytest.mark.parametrize(
    "ip",
    ["127.0.0.1", "10.1.2.3", "192.168.1.5", "169.254.169.254", "::1", "0.0.0.0", "100.64.0.1"],
)
def test_private_addresses_are_refused(monkeypatch, ip):
    _resolve_to(monkeypatch, ip)
    with pytest.raises(UnsafeInput):
        check_url("http://some.host/")


def test_public_address_is_accepted_and_opt_out(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    assert check_url("https://example.com/a") == "https://example.com/a"
    _resolve_to(monkeypatch, "127.0.0.1")
    monkeypatch.setenv("REMARKABLE_ALLOW_PRIVATE_URLS", "1")
    assert check_url("http://localhost:8080/")


def test_redirect_to_private_address_is_refused(monkeypatch):
    import requests

    def resolve(host, *a, **k):
        ip = "127.0.0.1" if host == "internal" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)

    class Redirect:
        status_code, is_redirect = 302, True
        headers = {"Location": "http://internal/secrets"}

        def close(self):
            pass

    monkeypatch.setattr(requests, "get", lambda *a, **k: Redirect())
    with pytest.raises(UnsafeInput):
        safety.fetch_public("https://example.com/", {}, 5, 1000)


def test_download_size_is_capped(monkeypatch):
    import requests

    _resolve_to(monkeypatch, "93.184.216.34")

    class Big:
        status_code, is_redirect, headers = 200, False, {}

        def raise_for_status(self):
            pass

        def iter_content(self, n):
            for _ in range(100):
                yield b"x" * n

        def close(self):
            pass

    monkeypatch.setattr(requests, "get", lambda *a, **k: Big())
    with pytest.raises(UnsafeInput):
        safety.fetch_public("https://example.com/", {}, 5, 200_000)


def test_tools_refuse_unsafe_inputs(cloud, home, monkeypatch):  # noqa: F811
    from remarkable_mcp.workflows import form_tools, latex_tools, reading_tools, tools

    err = _json_of(
        asyncio.run(tools.remarkable_review_send(source_path=str(home / ".ssh" / "notes.md")))
    )
    assert err["_error"]["type"] == "invalid_source"
    err = _json_of(asyncio.run(tools.remarkable_review_send(source_path="/proc/self/environ")))
    assert err["_error"]["type"] == "invalid_source"
    err = _json_of(asyncio.run(latex_tools.remarkable_latex_review_send("/etc/passwd")))
    assert err["_error"]["type"] == "invalid_pdf"
    err = _json_of(
        asyncio.run(
            form_tools.remarkable_form_send(
                "x",
                [
                    {"type": "image", "label": "i", "path": "/dev/zero"},
                    {"id": "a", "type": "checkbox", "label": "a"},
                ],
            )
        )
    )
    assert err["_error"]["type"] == "invalid_form"
    err = _json_of(
        asyncio.run(
            form_tools.remarkable_form_send(
                "x",
                [
                    {"type": "image", "label": "i", "png": "/etc/passwd"},
                    {"id": "a", "type": "checkbox", "label": "a"},
                ],
            )
        )
    )
    assert err["_error"]["type"] == "invalid_form"  # a string png is ignored, path required
    _resolve_to(monkeypatch, "169.254.169.254")
    err = _json_of(asyncio.run(reading_tools.remarkable_clip(url="http://metadata.internal/")))
    assert err["_error"]["type"] == "url_refused"
