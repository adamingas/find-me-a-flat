"""Small shared contract for the optional browser worker."""

from urllib.parse import urlsplit

MAX_BODY_BYTES = 25 * 1024 * 1024


def allowed_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        return (
            parts.scheme == "https"
            and (host == "openrent.co.uk" or host.endswith(".openrent.co.uk"))
            and parts.port in (None, 443)
            and parts.username is None
            and parts.password is None
        )
    except ValueError:
        return False


def is_challenge(headers, body: bytes = b"") -> bool:
    """Recognize WAF responses, never infer a CAPTCHA from a parser failure."""
    if headers.get("x-amzn-waf-action", "").lower() in {"challenge", "captcha"}:
        return True
    if "html" not in headers.get("content-type", "").lower():
        return False
    text = body[:128_000].lower()
    return b"let's confirm you are human" in text or (
        b"awswaf" in text and (b"challenge.js" in text or b"captcha.js" in text)
    )
