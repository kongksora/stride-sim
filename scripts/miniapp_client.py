"""CLIENT EMULATION - one-to-one replica of the Tongji mini program's upload.

Everything in this module mirrors app-service.js. It does not invent data: it
consumes the raw GPS samples from phantom_track.py and reproduces exactly what
the mini program would put on the wire - the smoothing filter, the derived
speed/dist/attribute1 fields, the 60 s chunking, the request body, the md5
sign and the HTTP headers.

Rule of thumb: if a change is about matching the app or the server protocol it
belongs here; if it is about making the run look natural it belongs in
phantom_track.py.

References below are byte offsets in the unpacked app-service.js.

Save-payload field reference
============================

Motion fields, per point (details[]):
  latitude / longitude  GCJ-02 position, after ia() smoothing
  time                  "YYYY-MM-DD HH:MM:SS"
  speed                 distance from the previous *uploaded* point / dt (m/s;
                        the first point is 0, NaN -> 0)
  dist                  distance from the previous *uploaded* point (m)
  altitude              raw GPS altitude (m)
  attribute1            pause-segment index (H.length - 1); 0 for a continuous run

Motion fields, per record (top level):
  totalKm               formatDistance(p.value): cumulative over uploaded points,
                        truncated (not rounded) to 2 decimals
  stepNumber            cumulative steps
  avgFrequency          formatStrideRate(steps, elapsed_s)
                        = steps / Number((elapsed_s / 60).toFixed(4))
  altitudeMin / Max     min / max raw altitude
  stopTime              paused seconds (the client sends paused minutes * 60)
  timeFrom / timeTo     run start / finish ("timeTo" only when finish=1)
  resultKm              target distance from the rules (not measured)
  runStatus             1 = 自由跑

Identity / auth / rule (not motion): uid, loginName, sex, semesterId,
semesterName, venueId, venueName, standardPace, timestamp, sign.

---------------------------------------------------------------------------
Server quirks, learned the hard way (ty.tongji.edu.cn/msports). Read before
touching request construction.

2026-09-29 - the save endpoint got stricter; what worked on 09-28 now fails:
  * POST /api/public/running/save with Content-Type "application/json" (no
    charset) returns {"code":500,"msg":"请求异常，请稍后再试"} for every body,
    even "{}" - the request is never deserialized. Adding ";charset=UTF-8"
    makes the same "{}" reach validation, so the charset parameter is now
    mandatory. Requests must use application/json;charset=UTF-8.
  * standardPace is parsed as a number. The mini program sends "8.00"
    (formatAveragePace -> "8'00''" -> JS first-occurrence replace -> "8.00").
    Python's str.replace replaces every occurrence, so the naive port produced
    "8.00..", which now makes the finishing chunk throw {"code":500,"msg":null}.
    Use js_standard_pace()/sanitize_standard_pace() and omit the key when the
    value is not a plain decimal.
  * The FIRST chunk stored for a run must carry a valid standardPace. If it is
    absent, every later finish=1 save fails with {"code":500,"msg":null} and
    the run can never be completed - there is no update or delete API. Any
    throwaway probe that creates a record can thus strand it forever.
  * Re-posting after a finished run -> "已存在跑步结束数据,请勿重复提交".
    Starting with uid="" while an unfinished record exists -> 非法参数.
    Rapid repeated starts -> the generic 请求异常 500.
  * Daily 达标 credit is 1: the first qualifying run of the day gets
    gradeStatus=2 (达标); later runs the same day get gradeStatus=0 (合格).
    Both are state=0 and both show in run/list, so never submit a partial or
    throwaway run before the real one - it may take the day's 达标 slot with a
    broken track.
  * attribute1 is the pause-segment index (H.length-1), constant 0 for a
    continuous run. The record page groups points by it and skips groups of a
    single point, so an incrementing attribute1 yields no green line.
---------------------------------------------------------------------------
"""
import base64
import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from geo import haversine

APPID = "wx89b3d9ebb11efee3"
BASE = "https://ty.tongji.edu.cn/msports"
XWECHAT_USERS = Path.home() / ".xwechat/radium/users"
CAPTURE_LOG = Path.home() / ".cache/stride-sim/mitm.log"
TOKEN_FILE = Path.home() / ".cache/stride-sim/wechat_token"
SIGN_SALT = "TongjiSports"
UA = (
    "Mozilla/5.0 (Linux; Android 15; V2405A Build/AP3A.240905.015) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Version/4.0 Chrome/126.0.6478.71 Mobile Safari/537.36 "
    "MicroMessenger/8.0.42.2460(0x28002A38) WeChat/arm64 Weixin NetType/WIFI "
    "Language/zh_CN ABI/arm64 MiniProgramEnv/android"
)

# --- smoothing filter constants, straight from the run page -----------------
CLIENT_FILTER_WEIGHTS = (0.5, 0.3, 0.2)   # ia(): weights for y[-1], y[-2], y[-3]
CLIENT_FILTER_WINDOW = len(CLIENT_FILTER_WEIGHTS)   # we = 3 raw points
CLIENT_MAX_SPEED = 20.0                   # ka: points faster than this are dropped
CLIENT_SHARP_TURN_DEG = 45.0              # ua(): "sharp turn" threshold


def md5_hex(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def format_distance(meters):
    km = str(meters / 1000.0)
    parts = km.split(".")
    whole = parts[0] if parts[0] else "0"
    frac = parts[1] if len(parts) > 1 else ""
    if len(frac) >= 2:
        frac = frac[:2]
    else:
        frac = frac + "0"
    return f"{whole}.{frac}"


def format_avg_pace(seconds_per_km):
    if not seconds_per_km:
        return "0'00''"
    minutes = int(seconds_per_km // 60)
    secs = int(round(seconds_per_km % 60))
    if secs == 60:
        secs = 0
        minutes += 1
    return f"{minutes}'{secs:02d}''"


def js_standard_pace(pace_str):
    """Mirror the mini program: .replace("'", ".").replace("''", "").

    JS String.replace with a string pattern replaces only the first match,
    so "8'00''" becomes "8.00" (the server now parses this as a number).
    """
    i = pace_str.find("'")
    if i < 0:
        return pace_str
    return (pace_str[:i] + "." + pace_str[i + 1:]).replace("''", "", 1)


def sanitize_standard_pace(value):
    """Keep standardPace only when it is a plain decimal like "8.00".

    The server parses this field as a number; a string such as "8'00''" or the
    old "8.00.." bug makes the finishing save return {"code":500,"msg":null}
    (seen 2026-09-29) and the run can then never be finalized.
    """
    value = (value or "").strip()
    return value if re.fullmatch(r"\d+\.\d{2}", value) else ""


def resolve_timezone(name):
    if not name:
        return datetime.now().astimezone().tzinfo
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# The run page's point filter (app-service.js): ia() smooths a point with the
# last 3 *raw* points, unless la() flags it as an outlier (jump / sharp turn).
# ---------------------------------------------------------------------------
def _angle(lat1, lon1, lat2, lon2):
    return math.atan2(lat2 - lat1, lon2 - lon1)


def _turn_angle(prev, last, new):
    u = _angle(prev["latitude"], prev["longitude"], last["latitude"], last["longitude"])
    return (_angle(last["latitude"], last["longitude"], new["latitude"], new["longitude"]) - u) * 180.0 / math.pi


def _is_outlier(point, window):
    """la(t, y): True when the point jumped away from the window centroid.

    threshold is 5 m/s (20 m/s when the point itself is fast), doubled on a
    sharp turn.
    """
    dt = point["_t"] - window[0]["_t"]
    if dt <= 0:
        return False
    clat = sum(p["latitude"] for p in window) / len(window)
    clon = sum(p["longitude"] for p in window) / len(window)
    distance = haversine(clat, clon, point["latitude"], point["longitude"])
    limit = 20.0 if point["speed"] > 5 else 5.0
    if len(window) >= 3 and abs(_turn_angle(window[-2], window[-1], point)) > CLIENT_SHARP_TURN_DEG:
        limit *= 2
    return distance / dt > limit


def _smooth(point, window):
    """ia(t): weighted average of the raw window, or the point itself if too few
    samples / flagged as an outlier."""
    if len(window) < CLIENT_FILTER_WINDOW:
        return point
    if _is_outlier(point, window):
        return point
    lat = sum(window[-1 - i]["latitude"] * CLIENT_FILTER_WEIGHTS[i] for i in range(CLIENT_FILTER_WINDOW))
    lon = sum(window[-1 - i]["longitude"] * CLIENT_FILTER_WEIGHTS[i] for i in range(CLIENT_FILTER_WINDOW))
    return {**point, "latitude": lat, "longitude": lon}


def _format_time(start_dt, offset_seconds, tz):
    return (start_dt + timedelta(seconds=offset_seconds)).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")


def emulate_samples(samples, start_dt, tz):
    """Turn raw samples into the exact point stream the mini program uploads.

    Reproduces the run page's loop: speed/dist measured against the previously
    *pushed* point, ia() smoothing of the coordinates, ka speed drop, cumulative
    distance from the pushed points, cumulative altitude min/max and attribute1.
    """
    pushed = []      # n: points that will be uploaded
    window = []      # y: last CLIENT_FILTER_WINDOW raw points
    total_m = 0.0
    alt_lo = alt_hi = None
    for sample in samples:
        point = dict(sample)
        point["speed"] = 0.0
        point["dist"] = 0.0
        if pushed:
            prev = pushed[-1]
            distance = haversine(prev["latitude"], prev["longitude"], point["latitude"], point["longitude"])
            dt = point["_t"] - prev["_t"]
            speed = distance / dt if dt > 0 else 0.0
            if math.isnan(speed):
                speed = 0.0
            point["speed"] = speed
            point["dist"] = distance
            window.append(point)
            if len(window) > CLIENT_FILTER_WINDOW:
                window.pop(0)
            if speed > CLIENT_MAX_SPEED:
                continue      # U.warn("异常点位") then return - point not uploaded
        out = _smooth(point, window)
        pushed.append(out)
        if len(pushed) > 1:
            total_m += haversine(pushed[-2]["latitude"], pushed[-2]["longitude"],
                                 pushed[-1]["latitude"], pushed[-1]["longitude"])
        out["_dist_cum"] = total_m
        alt = out["altitude"]
        alt_lo = alt if alt_lo is None else min(alt_lo, alt)
        alt_hi = alt if alt_hi is None else max(alt_hi, alt)
        out["_alt_lo"] = alt_lo
        out["_alt_hi"] = alt_hi
        out["attribute1"] = 0
        out["time"] = _format_time(start_dt, out["_t"], tz)
    elapsed = samples[-1]["_t"] if samples else 0.0
    steps = samples[-1]["_steps"] if samples else 0.0
    stop = samples[-1]["_stop"] if samples else 0.0
    return {
        "details": pushed,
        "total_m": total_m,
        "elapsed_s": elapsed,
        "stop_s": stop,
        "step_number": int(round(steps)),
        "cadence": steps / max(elapsed / 60.0, 1e-4) if steps else 0.0,
        "alt_min": round(alt_lo, 3) if alt_lo is not None else 0.0,
        "alt_max": round(alt_hi, 3) if alt_hi is not None else 0.0,
        "start": start_dt,
        "end": start_dt + timedelta(seconds=elapsed),
    }


def plan_chunks(details, chunk_seconds):
    """The app saves one batch per 60 s of wall time (Xe > 60 * 1e3)."""
    chunks = []
    current = []
    chunk_start = details[0]["time"]
    base = datetime.strptime(chunk_start, "%Y-%m-%d %H:%M:%S")
    for p in details:
        pt = datetime.strptime(p["time"], "%Y-%m-%d %H:%M:%S")
        if (pt - base).total_seconds() >= chunk_seconds and current:
            chunks.append(current)
            current = []
            base = pt
        current.append(p)
    if current:
        chunks.append(current)
    return chunks


def build_body(run, chunk, uid, login_name, sex, run_state, semester, standard_pace, finish):
    # Mirrors the mini program's pushhRunStepPoints payload (app-service.js ~1121450).
    # Point fields are latitude/longitude/time/speed/altitude/attribute1/dist, where
    #   speed      = segment distance / dt  (m/s, not the device speed)
    #   dist       = distance from the previous point (m)
    #   attribute1 = segment index (H.length - 1); constant 0 for a continuous run.
    # The record page groups points by attribute1 and draws a green polyline per
    # group, so sending an incrementing value breaks the track (groups of 1 point
    # are dropped and no line is drawn).
    point_fields = ("latitude", "longitude", "time", "speed", "altitude", "attribute1", "dist")
    detail_points = [{k: p[k] for k in point_fields} for p in chunk]
    tail = chunk[-1]
    cum_m = tail.get("_dist_cum", run["total_m"])
    cum_steps = tail.get("_steps", run["step_number"])
    cum_stop = tail.get("_stop", run["stop_s"])
    cum_t = tail.get("_t", run["elapsed_s"])
    # formatStrideRate(steps, elapsed_s) = steps / Number((elapsed_s / 60).toFixed(4))
    cadence = cum_steps / max(round(cum_t / 60.0, 4), 1e-4) if cum_steps else run["cadence"]
    body = {
        "uid": uid,
        "details": detail_points,
        "resultKm": run_state["result_km"],
        "timestamp": int(time.time() * 1000),
        "runStatus": run_state["run_status"],
        "loginName": login_name,
        "semesterId": semester.get("semesterId") or "",
        "semesterName": semester.get("semesterName") or "",
        "totalKm": format_distance(cum_m),
        "avgFrequency": cadence,
        "altitudeMin": round(tail.get("_alt_lo", run["alt_min"]), 3),
        "altitudeMax": round(tail.get("_alt_hi", run["alt_max"]), 3),
        "stopTime": int(round(cum_stop)),
        "timeFrom": run["start"].strftime("%Y-%m-%d %H:%M:%S"),
        "sex": sex,
        "venueId": run_state["venue_id"],
        "venueName": run_state["venue_name"],
        "stepNumber": int(round(cum_steps)),
    }
    # The mini program only sets standardPace when it knows it (JS drops undefined
    # keys from the JSON). Keep that behavior: an empty/non-numeric value makes the
    # server throw {"code":500,"msg":null} on the finish chunk (seen 2026-09-29).
    if standard_pace:
        body["standardPace"] = standard_pace
    if finish:
        body["finish"] = finish
        body["timeTo"] = run["end"].strftime("%Y-%m-%d %H:%M:%S")
    body["sign"] = md5_hex(f"{uid}{login_name}{body['timestamp']}{SIGN_SALT}")
    return body


# ---------------------------------------------------------------------------
# HTTP + token plumbing
# ---------------------------------------------------------------------------
def api_request(method, path, token, body=None, params=None, timeout=25):
    url = BASE + path
    if params:
        query = urllib.parse.urlencode(params)
        url = f"{url}?{query}"
    data = None
    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        # charset is mandatory: without it the server answers every POST with the
        # generic 请求异常 500 without ever reading the body (see module comment).
        headers["Content-Type"] = "application/json;charset=UTF-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"code": -1, "msg": raw[:500]}


def api_get(path, token, params=None):
    merged = dict(params or {})
    if token:
        merged.setdefault("token", token)
    return api_request("GET", path, token, params=merged)


def api_post(path, token, body):
    return api_request("POST", path, token, body=body)


def extract_token(appid):
    candidates = []
    for user_dir in sorted(XWECHAT_USERS.glob("*")):
        base = user_dir / "applet" / "local" / appid
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            try:
                if path.stat().st_size > 32 * 1024 * 1024:
                    continue
                blob = path.read_bytes()
            except OSError:
                continue
            for match in re.finditer(rb"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}", blob):
                candidates.append((str(path), match.group(0).decode("ascii", "replace")))
            for match in re.finditer(rb"token", blob):
                start = max(0, match.start() - 40)
                end = min(len(blob), match.end() + 120)
                ctx = blob[start:end].decode("utf-8", "replace")
                if "eyJ" not in ctx:
                    candidates.append((str(path), ctx))
    return candidates


def jwt_expiry(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        return int(exp) if exp else None
    except (IndexError, ValueError, json.JSONDecodeError):
        return None


def token_from_capture_log():
    try:
        lines = CAPTURE_LOG.read_text(errors="replace").splitlines()
    except OSError:
        return None
    tokens = []
    for line in lines:
        for match in re.finditer(r'"auth":\s*"Bearer\s+([^"]+)"', line):
            tokens.append(match.group(1))
        for match in re.finditer(r'\\"token\\":\\"([A-Za-z0-9._-]+)\\"', line):
            tokens.append(match.group(1))
    if not tokens:
        return None
    best = None
    for token in tokens:
        exp = jwt_expiry(token)
        if exp and (best is None or exp > best[0]):
            best = (exp, token)
    if best:
        return best[1]
    return tokens[-1]


def save_token(token):
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token + "\n")
    TOKEN_FILE.chmod(0o600)


def report_expiry(token, prefix=""):
    exp = jwt_expiry(token)
    if not exp:
        return
    now = time.time()
    when = datetime.fromtimestamp(exp).astimezone().strftime("%Y-%m-%d %H:%M")
    hours = (exp - now) / 3600.0
    print(f"{prefix}token expires {when} ({hours:.1f} h from now)")


def fetch_context(token, args, tz):
    context = {
        "login_name": args.login_name or "",
        "sex": args.sex,
        "uid": args.uid or "",
        "standard_pace": sanitize_standard_pace(args.standard_pace or ""),
        "result_km": args.result_km,
        "semester": {},
        "run_status": 1,
        "venue_id": 0,
        "venue_name": "",
    }
    try:
        user = api_get("/getUser", token)
        data = user.get("data") or {}
        context["login_name"] = args.login_name or data.get("loginName") or data.get("loginId") or ""
        if data.get("sex") is not None and args.sex is None:
            context["sex"] = int(data.get("sex") or 0)
        context["uid"] = args.uid or str(data.get("uid") or "")
    except Exception as exc:
        print(f"warn: getUser failed: {exc}")
    try:
        setting = api_get("/api/public/running/query/setting", token)
        data = setting.get("data") or {}
        context["standard_pace"] = sanitize_standard_pace(args.standard_pace or (data.get("standardPace") or ""))
        context["semester"] = {
            "semesterId": data.get("semesterId") or "",
            "semesterName": data.get("semesterName") or "",
        }
    except Exception as exc:
        print(f"warn: query/setting failed: {exc}")
    try:
        base = api_get("/api/public/tongji/getRunningSemesterGoalRequest", token)
        data = base.get("data") or {}
        if data.get("semesterId") and not context["semester"].get("semesterId"):
            context["semester"]["semesterId"] = data.get("semesterId")
        if data.get("semesterName") and not context["semester"].get("semesterName"):
            context["semester"]["semesterName"] = data.get("semesterName")
        key = "onceMinFemalekm" if context.get("sex") == 1 else "onceMinMalekm"
        if args.result_km is None and data.get(key) is not None:
            context["result_km"] = float(data.get(key))
        if not context["standard_pace"]:
            time_key = "onceCompletionFemaleTime" if context.get("sex") == 1 else "onceCompletionMaleTime"
            try:
                pace_seconds = float(data.get(time_key) or 0) / max(float(data.get(key) or 1), 0.1) * 60.0
                if pace_seconds > 0:
                    candidate = js_standard_pace(format_avg_pace(pace_seconds))
                    # The server now parses standardPace as a number; drop anything
                    # that is not "8.00"-shaped instead of shipping a bad value.
                    if re.fullmatch(r"\d+\.\d{2}", candidate):
                        context["standard_pace"] = candidate
            except (TypeError, ValueError):
                pass
    except Exception as exc:
        print(f"warn: getRunningSemesterGoalRequest failed: {exc}")
    if context["result_km"] is None:
        context["result_km"] = 2.0
    return context
