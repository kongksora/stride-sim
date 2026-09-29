#!/usr/bin/env python3
"""Replay a Tongji running record.

The code is deliberately split into three layers so the two concerns never get
mixed up:

  phantom_track.py    fabricates *natural-looking raw GPS samples* (route,
                      physics, noise). It knows nothing about Tongji.
  miniapp_client.py   reproduces *one to one* what the mini program uploads
                      (filter, derived fields, chunks, body, sign, headers).
  replay_run.py       this file: orchestration (calibration, CLI, upload loop).

If you change how the data is generated it is a phantom change; if you change
how it is sent it is a client change. Do not blend the two.
"""
import argparse
import json
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import miniapp_client as client
import phantom_track as phantom


def main():
    parser = argparse.ArgumentParser(description="Rebuild and replay a Tongji running record.")
    parser.add_argument("--token", help="Bearer token from the mini program storage")
    parser.add_argument("--extract-token", action="store_true", help="scan PC WeChat storage for a token")
    parser.add_argument("--check", action="store_true", help="fetch user/setting/blacklist and exit")
    parser.add_argument("--submit", action="store_true", help="actually POST to the server")
    parser.add_argument("--fast", action="store_true",
                        help="upload every chunk back-to-back instead of one per 60s "
                             "(bursty and distinguishable; prefer realtime)")
    parser.add_argument("--start", help="run start time, YYYY-MM-DD HH:MM:SS in the chosen timezone")
    parser.add_argument("--tz", help="timezone name, default system local")
    parser.add_argument("--distance-km", type=float, default=None, help="planned distance")
    parser.add_argument("--distance-jitter", type=float, default=0.05, help="random +/- km per run")
    parser.add_argument("--offset-m", type=float, default=3.0, help="random whole-track translation radius in meters")
    parser.add_argument("--pace", type=float, default=None, help="seconds per km")
    parser.add_argument("--pace-jitter", type=float, default=8.0, help="random +/- seconds per km per run")
    parser.add_argument("--route", help="json route file with a points array")
    parser.add_argument("--gpx", help="gpx track to follow")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--appid", default=client.APPID)
    parser.add_argument("--base", default=client.BASE)
    parser.add_argument("--uid", default=None)
    parser.add_argument("--login-name", default=None)
    parser.add_argument("--sex", type=int, default=None, choices=[0, 1])
    parser.add_argument("--result-km", type=float, default=None)
    parser.add_argument("--standard-pace", default=None)
    parser.add_argument("--chunk-seconds", type=int, default=60)
    parser.add_argument("--skip-chunks", type=int, default=0,
                        help="skip the first N chunks (resume an in-progress record)")
    parser.add_argument("--offline", action="store_true", help="skip server context fetches")
    parser.add_argument("--emit-template", help="write a patch template json and exit")
    args = parser.parse_args()

    client.APPID = args.appid
    client.BASE = args.base

    if args.extract_token:
        captured = client.token_from_capture_log()
        if captured:
            client.save_token(captured)
            print(f"token saved to {client.TOKEN_FILE}")
            client.report_expiry(captured, prefix="")
            return 0
        found = client.extract_token(args.appid)
        if not found:
            print("no token found; see the wechat-token-capture skill, log in, then retry")
            return 1
        for path, value in found:
            print(f"{path}\n  {value}\n")
        return 0

    if not args.token and not (args.emit_template and args.offline):
        print("missing --token (or use --extract-token after logging in)")
        return 1

    if args.token:
        exp = client.jwt_expiry(args.token)
        if exp:
            remaining = exp - time.time()
            if remaining <= 0:
                print("token has expired; re-capture via the wechat-token-capture skill")
                return 1
            if remaining < 6 * 3600:
                print(f"warning: token expires in {remaining/3600:.1f} h; consider re-capturing")

    tz = client.resolve_timezone(args.tz)
    rng = random.Random(args.seed)

    if args.check:
        for path in ("/getUser", "/api/public/running/query/setting", "/api/public/tongji/getUserRunBlack"):
            try:
                print(path, json.dumps(client.api_get(path, args.token), ensure_ascii=False)[:800])
            except Exception as exc:
                print(path, f"failed: {exc}")
        return 0

    context = {
        "login_name": args.login_name or "",
        "sex": args.sex if args.sex is not None else 0,
        "uid": args.uid or "",
        "standard_pace": client.sanitize_standard_pace(args.standard_pace or ""),
        "result_km": args.result_km if args.result_km is not None else 2.0,
        "semester": {},
        "run_status": 1,
        "venue_id": 0,
        "venue_name": "",
    }
    if not args.offline:
        context = client.fetch_context(args.token, args, tz)
    result_km = args.result_km if args.result_km is not None else context["result_km"]
    distance_km = args.distance_km if args.distance_km is not None else phantom.DEFAULT_DISTANCE_KM
    if args.distance_jitter:
        distance_km += rng.uniform(-args.distance_jitter, args.distance_jitter)
        distance_km = max(distance_km, 2.01)
    pace = args.pace if args.pace is not None else phantom.DEFAULT_PACE_SEC_PER_KM
    if args.pace_jitter:
        pace += rng.uniform(-args.pace_jitter, args.pace_jitter)

    if args.route:
        route = [(p[0], p[1]) for p in json.loads(Path(args.route).read_text())["points"]]
    else:
        route = phantom.load_route(args)
    route = phantom.offset_route(route, args.offset_m, rng)

    duration_guess = distance_km * pace
    if args.start:
        start_dt = datetime.strptime(args.start, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
    elif args.fast:
        start_dt = datetime.now(tz).replace(microsecond=0) - timedelta(seconds=duration_guess) - timedelta(minutes=2)
    else:
        start_dt = datetime.now(tz).replace(microsecond=0)

    meta = phantom.route_meta(args)
    # Calibrate the phantom pace so the *uploaded* (filtered) run matches target.
    # Pin the RNG state for every trial: otherwise each trial draws a different
    # event schedule and the effective pace oscillates instead of converging.
    # The final trial is the run we keep, so its randomness is the one uploaded.
    target_pace = pace
    rng_state = rng.getstate()
    for _ in range(6):
        rng.setstate(rng_state)
        raw = phantom.simulate_raw(route, distance_km * 1000.0, pace, rng,
                                   shrink_m=meta["shrink_m"], min_active_s=phantom.MIN_ACTIVE_SECONDS,
                                   altitude_base=meta["altitude_base"])
        run = client.emulate_samples(raw["samples"], start_dt, tz)
        effective = run["elapsed_s"] / max(run["total_m"] / 1000.0, 0.001)
        if abs(effective - target_pace) < 2:
            break
        pace *= target_pace / effective
    chunks = client.plan_chunks(run["details"], args.chunk_seconds)

    if args.emit_template:
        template = {
            "generated_at": datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S"),
            "total_m": round(run["total_m"], 3),
            "total_km": client.format_distance(run["total_m"]),
            "duration_s": round(run["elapsed_s"], 3),
            "stop_s": round(run["stop_s"], 3),
            "step_number": run["step_number"],
            "cadence": run["cadence"],
            "alt_min": run["alt_min"],
            "alt_max": run["alt_max"],
            "points": [
                {
                    "t": round(p["_t"], 3),
                    "latitude": p["latitude"],
                    "longitude": p["longitude"],
                    "speed": p["speed"],
                    "altitude": p["altitude"],
                    "dist": p["dist"],
                }
                for p in run["details"]
            ],
        }
        Path(args.emit_template).write_text(json.dumps(template, ensure_ascii=False))
        print(f"template -> {args.emit_template} ({len(template['points'])} points)")
        return 0

    print(
        f"distance {run['total_m']/1000:.3f} km, duration {run['elapsed_s']/60:.1f} min, "
        f"active pace {run['elapsed_s']/max(run['total_m']/1000,0.001):.0f} s/km, "
        f"steps {run['step_number']}, cadence {run['cadence']:.1f}/min, stop {run['stop_s']:.0f} s, "
        f"chunks {len(chunks)}"
    )
    print(f"window {run['start']} -> {run['end']}, resultKm {result_km}, runStatus {context['run_status']}")

    run_state = {"result_km": result_km, "run_status": context["run_status"],
                 "venue_id": context["venue_id"], "venue_name": context["venue_name"]}

    if not args.submit:
        sample = client.build_body(run, chunks[0][:2], context["uid"], context["login_name"], context["sex"],
                                   run_state, context["semester"], context["standard_pace"],
                                   finish=1 if len(chunks) == 1 else 0)
        print(json.dumps(sample, ensure_ascii=False, indent=2)[:1200])
        print("dry run only, add --submit to upload")
        return 0

    uid = context["uid"]
    if args.fast:
        print(
            f"WARNING: --fast uploads all {len(chunks)} chunks back-to-back, so the server "
            f"sees a burst of requests for a {run['elapsed_s']/60:.1f} min run that claims to "
            "span the whole window. The app uploads one 60 s chunk per minute. It works today "
            "but is easy to distinguish; use realtime unless the window is about to close."
        )
    start_wall = time.time()
    skip = max(0, args.skip_chunks)
    if skip >= len(chunks):
        print(f"nothing to upload: --skip-chunks {skip} >= {len(chunks)} chunks")
        return 1
    pending = chunks[skip:]
    total = len(chunks)
    elapsed_before = float(sum(len(c) for c in chunks[:skip]))
    base_offset = elapsed_before
    for index, chunk in enumerate(pending):
        seq = index + skip
        last = index == len(pending) - 1
        chunk_end = elapsed_before + len(chunk)
        if not args.fast:
            target = start_wall + chunk_end - base_offset
            delay = target - time.time()
            if delay > 0:
                time.sleep(delay)
        else:
            time.sleep(0.4)
        body = client.build_body(run, chunk, uid, context["login_name"], context["sex"],
                                 run_state, context["semester"], context["standard_pace"],
                                 finish=1 if last else 0)
        try:
            resp = client.api_post("/api/public/running/save", args.token, body)
        except Exception as exc:
            print(f"chunk {seq+1}/{total} failed: {exc}")
            return 1
        data = resp.get("data") or {}
        if data.get("uid"):
            uid = str(data["uid"])
        extra = ""
        if last:
            extra = f" state={data.get('state')} avgSpeed={data.get('avgSpeed')} takeTime={data.get('takeTime')}"
        print(f"chunk {seq+1}/{total} code={resp.get('code')} msg={resp.get('msg')}{extra}")
        if resp.get("code") != 200:
            print(
                f"aborting after chunk {seq+1}/{total} (uid={uid or 'none'}). If a partial "
                f"record was created, resume later with --uid {uid} --skip-chunks {seq+1}."
            )
            return 1
        elapsed_before = chunk_end
    return 0


if __name__ == "__main__":
    sys.exit(main())
