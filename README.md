# stride-sim

Replay a Tongji "体育刷锻" free-run record from a PC, built to mimic what the
WeChat mini program itself uploads.

> Personal-use tool, shared with one other person. Records are bound to a
> WeChat/Tongji account, so capture and use **your own** token. Not affiliated
> with Tongji University - use at your own discretion and risk, and respect
> your school's rules.

## Requirements

- A Linux PC. Token capture relies on WeChat for Linux + mitmproxy; see
  `docs/token-capture.md` for the Windows notes.
- [`uv`](https://docs.astral.sh/uv/). The scripts are stdlib-only Python 3.11+.
- `mitmproxy`, only for capturing the token: `uv tool install mitmproxy`.

## Quick start

Run from the repository root.

1. Capture a token (valid 3 days) - follow **`docs/token-capture.md`**.
2. Check the account and the current rules:

   ```bash
   uv run --no-project scripts/replay_run.py \
     --token "$(cat ~/.cache/stride-sim/wechat_token)" --check
   ```

3. Dry run (builds the payload, uploads nothing):

   ```bash
   uv run --no-project scripts/replay_run.py \
     --token "$(cat ~/.cache/stride-sim/wechat_token)"
   ```

4. Realtime run - one 60 s chunk per minute, like the app:

   ```bash
   uv run --no-project scripts/replay_run.py \
     --token "$(cat ~/.cache/stride-sim/wechat_token)" --submit
   ```

Useful options: `--distance-km`, `--pace` (sec/km), `--start "YYYY-MM-DD HH:MM:SS"`,
`--seed`, `--route scripts/data/tongji_129_loop.json` (Siping; the default is
Jiading), `--offline` (skip server context fetches), `--extract-token`.
`--fast` uploads every chunk back-to-back and is easy to distinguish from the
app - avoid it unless a window is about to close.

## Code layout

The tooling is split so "make the data look natural" and "match the app byte
for byte" never mix:

- `scripts/geo.py` - shared geometry (the app's `calculateDistance` formula).
- `scripts/phantom_track.py` - **PHANTOM**: fabricates raw GPS samples (route,
  physics, noise). Knows nothing about Tongji or the wire format.
- `scripts/miniapp_client.py` - **CLIENT EMULATION**: reproduces the mini
  program's upload 1:1 (smoothing filter, derived fields, 60 s chunking, request
  body, md5 sign, HTTP and token plumbing). The save-payload field reference and
  the server-quirk notes live in its module docstring.
- `scripts/replay_run.py` - orchestrator and CLI (calibration, dry run, upload
  loop, `--extract-token`).

Rule of thumb: a change about realism goes in `phantom_track.py`; a change about
the app or the protocol goes in `miniapp_client.py`.

## Data

`scripts/data/*.json` are campus loop tracks used by the phantom layer:
`tongji_jiading_loop.json` (default) and `tongji_129_loop.json` (Siping).

## Notes

- The token file (`~/.cache/stride-sim/wechat_token`) and capture log live
  outside the repository and are never committed.
- The default run is ~2.1 km at ~5:00/km with jitter, a small random track
  offset, and Jiading track noise; it is rebuilt per run from the raw sample
  model and passed through the app's own filter.
