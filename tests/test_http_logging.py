from openrent.http_logging import BODY_LIMIT, safe_error


def test_browser_error_keeps_cause_and_redacts_credentials():
    error = RuntimeError(
        "Navigation failed: net::ERR_PROXY_CONNECTION_FAILED\n"
        "https://user:private-password@www.openrent.co.uk/search?token=private-token&term=London\n"
        "Authorization: Bearer private-bearer\n"
        "cookie='session=private-cookie'\n"
        "socks5://user:private-proxy@localhost:1080"
    )
    result = safe_error(error)
    assert "ERR_PROXY_CONNECTION_FAILED" in result
    assert "term=London" in result
    assert "localhost:1080" in result
    assert "private-" not in result
    assert len(safe_error(RuntimeError("x" * (BODY_LIMIT + 100)))) == BODY_LIMIT
