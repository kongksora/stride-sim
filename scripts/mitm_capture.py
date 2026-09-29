import json
import os
import time

from mitmproxy import http

LOG = os.path.expanduser("~/.cache/stride-sim/mitm.log")


def response(flow: http.HTTPFlow):
    host = flow.request.pretty_host
    if "tongji" not in host and "msports" not in host:
        return
    entry = {
        "t": time.strftime("%F %T"),
        "method": flow.request.method,
        "url": flow.request.pretty_url,
        "auth": flow.request.headers.get("authorization", ""),
        "req": flow.request.get_text(strict=False)[:4000] if flow.request.content else "",
        "status": flow.response.status_code,
    }
    ctype = flow.response.headers.get("content-type", "")
    if "json" in ctype or "text" in ctype:
        entry["resp"] = flow.response.get_text(strict=False)[:6000]
    else:
        entry["resp"] = ""
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
