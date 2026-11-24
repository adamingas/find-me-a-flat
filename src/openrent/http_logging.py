"""Bounded, redacted diagnostics for failed OpenRent responses."""

import json
import logging
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)
BODY_LIMIT = 8192
_SENSITIVE = re.compile(
    r"auth|cookie|token|secret|password|passwd|credential|signature|session|csrf|xsrf|jwt|key|^iv$|^context$",
    re.IGNORECASE,
)
_ASSIGNMENT = re.compile(
    r"""(["']?[\w-]*(?:token|secret|password|passwd|credential|signature|session|auth|cookie|csrf|xsrf|jwt|key|context|iv)[\w-]*["']?\s*[:=]\s*)"""
    r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;<>&}"']+)""",
    re.IGNORECASE,
)


def safe_url(url):
    parts = urlsplit(str(url))
    query = urlencode(
        [
            (key, "[REDACTED]" if _SENSITIVE.search(key) else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, query, ""))


def _headers(headers):
    return {
        name: "[REDACTED]"
        if _SENSITIVE.search(name)
        else (safe_url(value) if name.lower() in {"location", "referer"} else value[:1024])
        for name, value in headers.items()
    }


def log_http_failure(response, reason):
    """Log existing response data only; never read an unconsumed stream."""
    try:
        request = response.request
        secrets = []
        for headers in (request.headers, response.headers):
            for name, value in headers.items():
                if _SENSITIVE.search(name):
                    secrets.append(value)
                    if "cookie" in name.lower():
                        parts = (
                            [item.split(";", 1)[0] for item in headers.get_list(name)]
                            if name.lower() == "set-cookie"
                            else value.split(";")
                        )
                        secrets.extend(
                            part.split("=", 1)[1].strip() for part in parts if "=" in part
                        )
                    if name.lower() in {"authorization", "proxy-authorization"}:
                        secrets.append(value.split(" ", 1)[-1])
        secrets.extend(
            value for key, value in request.url.params.multi_items() if _SENSITIVE.search(key)
        )
        try:
            body = response.text
            for secret in sorted(set(secrets), key=len, reverse=True):
                if secret:
                    body = body.replace(secret, "[REDACTED]")
            body = _ASSIGNMENT.sub(lambda match: match[1] + '"[REDACTED]"', body)
            body = re.sub(
                r"""(\bvalue\s*=\s*)(?:"[^"]*"|'[^']*'|[^\s>]+)""",
                lambda match: match[1] + '"[REDACTED]"',
                body,
                flags=re.IGNORECASE,
            )
            truncated = len(body) > BODY_LIMIT
            body = body[:BODY_LIMIT]
        except httpx.ResponseNotRead:
            body, truncated = "[stream not read]", False
        logger.error(
            "HTTP failure: %s",
            json.dumps(
                {
                    "reason": reason,
                    "request": {
                        "method": request.method,
                        "url": safe_url(request.url),
                        "headers": _headers(request.headers),
                    },
                    "response": {
                        "status": response.status_code,
                        "headers": _headers(response.headers),
                        "body": body,
                        "body_truncated": truncated,
                    },
                    "waf_action": response.headers.get("x-amzn-waf-action"),
                    "redirects": [
                        {"status": item.status_code, "url": safe_url(item.url)}
                        for item in response.history
                    ],
                },
                ensure_ascii=True,
            ),
        )
    except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
        # Diagnostic formatting must never replace the original application error.
        logger.error("HTTP diagnostics unavailable: %s (reason=%s)", type(exc).__name__, reason)
