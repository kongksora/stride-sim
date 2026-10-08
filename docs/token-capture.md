# Token capture (Tongji sports mini program)

The replay script talks to `https://ty.tongji.edu.cn/msports` with
`Authorization: Bearer <token>`. The token is a JWT with a **3 day** lifetime;
it comes from the mini program's login exchange and is account-bound. It is
stored at `~/.cache/stride-sim/wechat_token` and validated by
`scripts/replay_run.py` before every run.

## Why a proxy is required

- Plain packet capture (tcpdump/Wireshark) is useless: TLS terminates inside
  WeChat's statically linked BoringSSL, no keylog is written.
- Linux WeChat runs mini programs in `WeChatAppEx` (Chromium/XWeb). It
  **honors `http_proxy` / `https_proxy` environment variables**. It ignores
  gsettings system proxy and `--proxy-server` when the env vars are set, so
  launch WeChat with the env vars pointing at mitmproxy.
- The mitmproxy CA must be trusted in the user's NSS database
  (`~/.pki/nssdb`), which Chromium/XWeb uses on Linux.

## Linux procedure

Run the commands from the repository root (`scripts/...` are repo-relative).

1. Install mitmproxy once (per machine):

   ```bash
   uv tool install mitmproxy
   ```

2. Start the capture in tmux. Chain to an upstream proxy if the machine needs
   one (e.g. Clash on 7897), otherwise drop `--mode`:

   ```bash
   tmux new -d -s stride-mitm \
     'mitmdump --mode upstream:http://127.0.0.1:7897 -p 8899 -q \
       -s scripts/mitm_capture.py'
   ```

   The addon writes `ty.tongji.edu.cn` requests/responses to
   `~/.cache/stride-sim/mitm.log`.

3. Trust the CA (created under `~/.mitmproxy/` on first start):

   ```bash
   certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n stride-mitm \
     -i ~/.mitmproxy/mitmproxy-ca-cert.pem
   ```

4. Quit WeChat completely, then start it through a **throwaway script**. Never
   persist this: no `.desktop` override, no shell rc changes, no exported env
   in the session.

   ```bash
   cat > /tmp/wechat-capture.sh <<'EOF'
   #!/bin/sh
   exec env http_proxy=http://127.0.0.1:8899 https_proxy=http://127.0.0.1:8899 \
       HTTP_PROXY=http://127.0.0.1:8899 HTTPS_PROXY=http://127.0.0.1:8899 \
       no_proxy=localhost,127.0.0.1,::1 NO_PROXY=localhost,127.0.0.1,::1 \
       /opt/wechat/wechat "$@"
   EOF
   chmod +x /tmp/wechat-capture.sh
   /tmp/wechat-capture.sh &
   ```

   Your normal launch must keep working as before; capture launches only ever
   go through this temp script.

5. Open the "同济体育刷锻" mini program and log in (or just browse an
   authenticated page). Any request to `ty.tongji.edu.cn` carries the Bearer
   header, and the login response carries `"token"`.

6. Extract and save the token:

   ```bash
   uv run --no-project scripts/replay_run.py --extract-token
   ```

   It reads the newest token from the capture log (picks the one with the
   latest `exp`) and prints the expiry time.

7. Clean up: stop the tmux session, remove the temp script, optionally remove
   the CA:

   ```bash
   tmux kill-session -t stride-mitm
   rm -f /tmp/wechat-capture.sh
   certutil -d sql:$HOME/.pki/nssdb -D -n stride-mitm
   ```

## Machine hygiene (must hold after every capture)

- WeChat's default launch stays untouched: no `~/.local/share/applications/wechat.desktop`,
  no `~/.config/autostart/wechat.desktop`, no shell rc changes.
- System proxy settings restored to their original values (e.g. gsettings ports
  back to your usual Clash port).
- The mitm CA removed from `~/.pki/nssdb` unless you explicitly want it kept.
- Only `~/.cache/stride-sim/*` (token, logs, backups) may remain.

Quick verification:

```bash
ls ~/.local/share/applications/wechat.desktop 2>/dev/null   # must not exist
gsettings get org.gnome.system.proxy.http port               # original port
certutil -d sql:$HOME/.pki/nssdb -L | grep stride-mitm       # no output
tmux ls                                                       # no stride-mitm session
```

## Verify capture is working

While WeChat runs, its network process should connect to the mitm port:

```bash
ss -tnp | grep WeChatAppEx | grep 8899
```

If nothing appears, the env proxy was not honored: confirm WeChat was fully
restarted (not just the mini program) and that the env vars were exported.

Watch the destination port, not just the presence of connections. XWeb/Chromium
prefers `ALL_PROXY`/`all_proxy` over `http_proxy`/`https_proxy`, and Clash-style
tooling exports a session-wide `all_proxy=socks5://127.0.0.1:7897`. If that leaks
into the launch environment, WeChatAppEx talks SOCKS to Clash (7897) and never
reaches mitmproxy:

```bash
ss -tnp | grep WeChatAppEx            # expect 127.0.0.1:8899, not :7897
```

Guard against it in the throwaway launcher by clearing the SOCKS vars:

```bash
exec env -u all_proxy -u ALL_PROXY \
    http_proxy=http://127.0.0.1:8899 https_proxy=http://127.0.0.1:8899 \
    HTTP_PROXY=http://127.0.0.1:8899 HTTPS_PROXY=http://127.0.0.1:8899 \
    no_proxy=localhost,127.0.0.1,::1 NO_PROXY=localhost,127.0.0.1,::1 \
    /opt/wechat/wechat "$@"
```

Belt-and-suspenders: XWeb also honors the GNOME system proxy, so temporarily
point it at the capture port and restore it afterwards (Hygiene step):

```bash
gsettings set org.gnome.system.proxy.http  port 8899
gsettings set org.gnome.system.proxy.https port 8899
gsettings set org.gnome.system.proxy.socks port 8899
```

Do not use `pkill -f /opt/wechat`: the pattern matches the invoking shell's own
command line and kills the session. Use `pkill -x wechat` (and `-x WeChatAppEx`).

## Windows notes

Windows WeChat uses WMPF; there is no known env-proxy path. Options are
WMPFDebugger / Frida to read `wx.getStorageSync("token")` directly, or a
system-proxy MITM tool with the CA installed in the Windows certificate store.
Both are version-specific; handle per machine. Do not copy a token between
machines or accounts: `loginName` and the request `sign` must match the token.

## Caveats

- Each user must capture their own token (it is bound to their WeChat/Tongji
  account).
- Tokens expire after 3 days; `scripts/replay_run.py` refuses to run with an
  expired token and warns when less than 6 hours remain.
- After a token re-capture, no other cleanup is needed; the token file is
  overwritten in place.

## Operating notes (learned the hard way)

- Prefer a realtime run (one chunk per 60 s, like the app). `--fast` uploads
  every chunk back-to-back; the server accepts it today but the burst is easy to
  distinguish from the app's cadence.
- Daily 达标 credit is 1 and goes to the **first** qualifying run of the day
  (`gradeStatus=2`); later runs that day only get 合格 (`gradeStatus=0`). Never
  submit a partial or throwaway run before the real one.
- The first chunk stored for a run must carry a valid numeric `standardPace`
  (e.g. `"8.00"`). Without it every later `finish=1` save fails with
  `{"code":500,"msg":null}` and the run can never be completed - there is no
  update or delete API. `miniapp_client.sanitize_standard_pace()` guards this.
- POST bodies must use `Content-Type: application/json;charset=UTF-8`; without
  the charset the server answers every save with the generic 请求异常 500.
