# Packaged xKiian/awswaf

Source: https://github.com/xKiian/awswaf
Pinned upstream commit: `0b7d9eb673daa50577453926576e0c9274f9dc75`
License: MIT (retained in LICENSE).

Upstream has no Python packaging metadata. This local dependency packages its
`python/awswaf/*.py` files and `webgl.json` without fetching moving code at build time.
The only change to the upstream Python files loads webgl.json as a package resource
instead of relative to the process working directory. An empty __init__.py and
pyproject.toml provide packaging.

Upstream's Python solver only implements silent challenge tokens; its visual
CAPTCHA/Gemini implementation is Go-only. `awswaf/captcha.py` is an async Python
adaptation of `internal/aws/captcha/{captcha,structs}.go`. It accepts HTTP and image
selection callbacks. The application supplies Chromium's request context and its
existing OpenRouter model configuration. It reuses Chromium's silent-challenge
token, without executing the upstream fingerprint/CPU proof-of-work path on the
worker event loop.

No monkeypatches, global credential configuration, or paid solver service are used.
The upstream silent solver remains available for compatibility testing.
