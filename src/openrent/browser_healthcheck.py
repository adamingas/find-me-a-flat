"""Check the authenticated worker API without launching Chromium."""

import os
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener


def main():
    token = Path(os.environ["BROWSER_TOKEN_FILE"]).read_text().strip()
    request = Request("http://127.0.0.1:8091/status", headers={"Authorization": f"Bearer {token}"})
    with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
        response.read()


if __name__ == "__main__":
    main()
