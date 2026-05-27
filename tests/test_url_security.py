"""Tests for the shared url_security module."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from earnings_analyzer.url_security import safe_join_url, sanitize_url


# --- sanitize_url: valid inputs ---


def test_accepts_https():
    assert sanitize_url("https://example.com/page") == "https://example.com/page"


def test_accepts_http():
    assert sanitize_url("http://example.com") == "http://example.com"


def test_strips_whitespace():
    assert sanitize_url("  https://example.com  ") == "https://example.com"


# --- sanitize_url: scheme rejection ---


def test_rejects_javascript_scheme():
    assert sanitize_url("javascript:alert(1)") is None


def test_rejects_ftp_scheme():
    assert sanitize_url("ftp://example.com/file") is None


def test_rejects_file_scheme():
    assert sanitize_url("file:///etc/passwd") is None


def test_rejects_data_scheme():
    assert sanitize_url("data:text/html,<h1>x</h1>") is None


# --- sanitize_url: empty / non-string ---


def test_rejects_empty_string():
    assert sanitize_url("") is None


def test_rejects_none_input():
    assert sanitize_url(None) is None  # type: ignore[arg-type]


def test_rejects_int_input():
    assert sanitize_url(42) is None  # type: ignore[arg-type]


# --- sanitize_url: control characters ---


def test_rejects_null_byte():
    assert sanitize_url("https://example.com/\x00path") is None


def test_rejects_newline():
    assert sanitize_url("https://example.com/\npath") is None


def test_rejects_delete_char():
    assert sanitize_url("https://example.com/\x7fpath") is None


# --- sanitize_url: credentials in URL ---


def test_rejects_username_in_url():
    assert sanitize_url("https://user@example.com/") is None


def test_rejects_username_and_password():
    assert sanitize_url("https://user:pass@example.com/") is None


# --- sanitize_url: local hosts ---


def test_rejects_localhost():
    assert sanitize_url("http://localhost:8000") is None


def test_rejects_localhost_localdomain():
    assert sanitize_url("http://localhost.localdomain/x") is None


def test_rejects_subdomain_of_localhost():
    assert sanitize_url("http://foo.localhost/x") is None


def test_rejects_dot_local_suffix():
    assert sanitize_url("http://myhost.local/x") is None


# --- sanitize_url: private IPs ---


def test_rejects_loopback_ipv4():
    assert sanitize_url("http://127.0.0.1/admin") is None


def test_rejects_rfc1918_10():
    assert sanitize_url("http://10.0.0.5/metadata") is None


def test_rejects_rfc1918_172():
    assert sanitize_url("http://172.16.0.1/") is None


def test_rejects_rfc1918_192():
    assert sanitize_url("http://192.168.1.1/") is None


def test_rejects_link_local_ipv4():
    assert sanitize_url("http://169.254.169.254/metadata") is None


def test_rejects_loopback_ipv6():
    assert sanitize_url("http://[::1]/admin") is None


# --- sanitize_url: allowed_hosts ---


def test_allowed_hosts_accepts_listed():
    result = sanitize_url(
        "https://api.example.com/data", allowed_hosts={"api.example.com"}
    )
    assert result == "https://api.example.com/data"


def test_allowed_hosts_rejects_unlisted():
    result = sanitize_url(
        "https://evil.com/data", allowed_hosts={"api.example.com"}
    )
    assert result is None


def test_allowed_hosts_case_insensitive():
    result = sanitize_url(
        "https://API.Example.Com/data", allowed_hosts={"api.example.com"}
    )
    assert result == "https://API.Example.Com/data"


# --- sanitize_url: resolve_host ---


def test_resolve_host_rejects_when_dns_returns_private():
    private_info = [(2, 1, 6, "", ("127.0.0.1", 0))]
    with patch("earnings_analyzer.url_security.socket.getaddrinfo", return_value=private_info):
        assert sanitize_url("https://internal.corp", resolve_host=True) is None


def test_resolve_host_accepts_when_dns_returns_public():
    public_info = [(2, 1, 6, "", ("93.184.216.34", 0))]
    with patch("earnings_analyzer.url_security.socket.getaddrinfo", return_value=public_info):
        assert sanitize_url("https://example.com", resolve_host=True) == "https://example.com"


def test_resolve_host_rejects_when_dns_fails():
    with patch("earnings_analyzer.url_security.socket.getaddrinfo", side_effect=OSError("DNS fail")):
        assert sanitize_url("https://nonexistent.invalid", resolve_host=True) is None


# --- safe_join_url ---


def test_safe_join_resolves_relative():
    result = safe_join_url("https://example.com/a/", "b/c")
    assert result == "https://example.com/a/b/c"


def test_safe_join_resolves_absolute():
    result = safe_join_url("https://example.com/a/", "https://other.com/b")
    assert result == "https://other.com/b"


def test_safe_join_rejects_redirect_to_private_ip():
    result = safe_join_url("https://example.com/", "http://127.0.0.1/admin")
    assert result is None


def test_safe_join_rejects_redirect_to_file_scheme():
    result = safe_join_url("https://example.com/", "file:///etc/passwd")
    assert result is None
