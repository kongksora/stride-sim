---
name: wechat-token-capture
description: Capture the Tongji sports mini program bearer token for stride-sim on a Linux PC with mitmproxy, with notes for Windows. Use when the stride-sim token expires (replay_run.py reports 401 or "token has expired"), or when setting up replay on a new machine.
---

# Token capture (stride-sim) - thin wrapper

The full procedure is documentation in this repository:

**`docs/token-capture.md`**

Read that file and follow it. It covers why a proxy is required, the Linux
mitmproxy steps (env-proxy WeChat launch, NSS CA, `--extract-token`), the
machine-hygiene checks, Windows notes, and the replay operating notes.

Trigger: use this when `scripts/replay_run.py` reports a 401 or
"token has expired", when the token is missing, or when setting up replay on a
new machine.
