from __future__ import annotations

import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

from .util import TriadError, read_json, uid


class Client:
    def __init__(self, state=None, token=None):
        self.root = Path(state or os.environ.get("TRIAD_STATE", ".triad")).resolve()
        self.token = token or os.environ.get("TRIAD_TOKEN")
        if not self.token:
            self.token = read_json(self.root / "config.json")["admin_token"]

    def call(self, action, data=None, request_id=None, retries=0):
        payload = json.dumps({"id": request_id or uid("req"), "action": action,
                              "data": data or {}}).encode()
        for attempt in range(retries + 1):
            try:
                endpoint = read_json(self.root / "endpoint.json")["url"]
                request = urllib.request.Request(endpoint + "/rpc", data=payload,
                                                 headers={"Authorization": "Bearer " + self.token,
                                                          "Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=30) as response:
                    result = json.load(response)
                if not result.get("ok"):
                    raise TriadError(result.get("error", "Controller rejected request"))
                return result["result"]
            except urllib.error.HTTPError as exc:
                try:
                    detail = json.loads(exc.read()).get("error", str(exc))
                except ValueError:
                    detail = str(exc)
                raise TriadError(detail) from exc
            except (OSError, urllib.error.URLError) as exc:
                if attempt >= retries:
                    raise TriadError(f"Controller unavailable: {exc}") from exc
                time.sleep(min(0.25 * 2**attempt, 3))
