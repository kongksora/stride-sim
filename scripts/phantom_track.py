"""PHANTOM TRACK - fabricate natural-looking raw GPS samples.

This layer knows nothing about Tongji, the server, or the mini program wire
format. It only simulates what a phone's GPS would have observed on a run:
a route, a human-ish speed model, a step cadence, and correlated position
noise (plus the occasional multi-metre jump).

It returns *raw samples* only - no speed, no distance, no attribute1, no
timestamps. Everything the server sees is derived from these samples by the
client-emulation layer (miniapp_client.emulate_samples), which reproduces the
mini program's own filtering one to one.

Keep it that way: if a change is about "looking more like a real run", it goes
here; if it is about "matching what the app sends", it goes in miniapp_client.
"""
import json
import math
from pathlib import Path

from geo import LocalProjection, haversine

ROUTE_FILE = Path(__file__).resolve().parent / "data" / "tongji_jiading_loop.json"
DEFAULT_DISTANCE_KM = 2.1
DEFAULT_PACE_SEC_PER_KM = 300
MIN_ACTIVE_SECONDS = 0.0


def load_route(args):
    if args.gpx:
        return load_gpx(args.gpx)
    data = json.loads(ROUTE_FILE.read_text())
    return [(p[0], p[1]) for p in data["points"]]


def route_meta(args):
    if args.gpx:
        return {"altitude_base": 4.2, "shrink_m": 6.0}
    path = Path(args.route) if args.route else ROUTE_FILE
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    return {
        "altitude_base": float(data.get("altitude_base", 4.2)),
        "shrink_m": float(data.get("shrink_m", 6.0)),
    }


def load_gpx(path):
    import xml.etree.ElementTree as ET

    tree = ET.parse(path)
    root = tree.getroot()
    points = []
    for trkpt in root.iter():
        if trkpt.tag.split("}")[-1] != "trkpt":
            continue
        lat = trkpt.get("lat")
        lon = trkpt.get("lon")
        if lat is None or lon is None:
            continue
        points.append((float(lat), float(lon)))
    if len(points) < 2:
        raise SystemExit("gpx contains too few track points")
    return points


def shrink_loop(points, meters):
    if meters <= 0:
        return list(points)
    lat0 = sum(p[0] for p in points) / len(points)
    lon0 = sum(p[1] for p in points) / len(points)
    proj = LocalProjection(lat0, lon0)
    xs = [proj.to_xy(lat, lon) for lat, lon in points]
    cx = sum(x for x, _ in xs) / len(xs)
    cy = sum(y for _, y in xs) / len(xs)
    out = []
    for x, y in xs:
        dx, dy = x - cx, y - cy
        norm = math.hypot(dx, dy) or 1.0
        nx = x - dx / norm * meters
        ny = y - dy / norm * meters
        out.append(proj.to_latlon(nx, ny))
    return out


def offset_route(points, max_meters, rng):
    if max_meters <= 0 or not points:
        return list(points)
    lat0 = sum(p[0] for p in points) / len(points)
    lon0 = sum(p[1] for p in points) / len(points)
    proj = LocalProjection(lat0, lon0)
    angle = rng.uniform(0, 2 * math.pi)
    radius = max_meters * math.sqrt(rng.random())
    dx = math.cos(angle) * radius
    dy = math.sin(angle) * radius
    out = []
    for lat, lon in points:
        x, y = proj.to_xy(lat, lon)
        out.append(proj.to_latlon(x + dx, y + dy))
    return out


class PathWalker:
    def __init__(self, latlon_points):
        lat0 = sum(p[0] for p in latlon_points) / len(latlon_points)
        lon0 = sum(p[1] for p in latlon_points) / len(latlon_points)
        self.proj = LocalProjection(lat0, lon0)
        self.pts = [self.proj.to_xy(lat, lon) for lat, lon in latlon_points]
        if self.pts[0] != self.pts[-1]:
            self.pts.append(self.pts[0])
        self.seg_len = []
        self.seg_dir = []
        self.cum = [0.0]
        for i in range(len(self.pts) - 1):
            x1, y1 = self.pts[i]
            x2, y2 = self.pts[i + 1]
            dx, dy = x2 - x1, y2 - y1
            length = math.hypot(dx, dy)
            self.seg_len.append(length)
            self.seg_dir.append((dx / length, dy / length))
            self.cum.append(self.cum[-1] + length)
        self.total = self.cum[-1]

    def direction_at(self, s):
        s = s % self.total
        lo, hi = 0, len(self.seg_len) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if self.cum[mid + 1] < s:
                lo = mid + 1
            else:
                hi = mid
        return self.seg_dir[lo]

    def point_at(self, s):
        s = s % self.total
        lo, hi = 0, len(self.seg_len) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if self.cum[mid + 1] < s:
                lo = mid + 1
            else:
                hi = mid
        remain = s - self.cum[lo]
        x = self.pts[lo][0] + self.seg_dir[lo][0] * remain
        y = self.pts[lo][1] + self.seg_dir[lo][1] * remain
        return self.proj.to_latlon(x, y)


def schedule_events(rng, duration):
    events = []
    t = rng.uniform(120, 240)
    stop_budget = 25.0
    while t < duration - 60:
        if rng.random() < 0.3 and stop_budget > 1.0:
            length = min(rng.uniform(5, 18), stop_budget)
            stop_budget -= length
            events.append((t, length, "stop"))
        else:
            events.append((t, rng.uniform(4, 12), "slow"))
        t += rng.uniform(150, 420)
    return events


def simulate_raw(route_points, distance_m, pace_sec_per_km, rng,
                 shrink_m=6.0, min_active_s=0.0, altitude_base=4.2):
    """Return raw 1 Hz GPS samples for a run of roughly *distance_m*.

    Each sample: latitude, longitude, altitude, _t (seconds since start),
    _steps (cumulative), _stop (cumulative paused seconds).
    """
    loop = shrink_loop(route_points, shrink_m)
    walker = PathWalker(loop)
    base_v = 1000.0 / pace_sec_per_km
    max_time = distance_m / 0.5 * 3 + 3600 + min_active_s * 3
    events = schedule_events(rng, max_time)
    event_idx = 0
    v = base_v * 0.6
    theta = 0.05
    sigma_v = 0.25
    noise_x = noise_y = 0.0
    rho = 0.88
    sigma_pos = rng.uniform(0.9, 1.4)
    cadence = rng.uniform(164, 172)
    cadence_theta = 0.03
    alt = altitude_base
    s_true = 0.0
    t = 0.0
    dt = 1.0
    steps = 0.0
    stop_seconds = 0.0
    total_reported = 0.0
    prev_clean = None
    samples = []
    warmup_end = 12.0
    while (total_reported < distance_m or (t - stop_seconds) < min_active_s) and t < max_time:
        while event_idx < len(events) and events[event_idx][0] <= t:
            event_idx += 1
        current = events[event_idx - 1] if event_idx > 0 else None
        v_target = base_v
        if current and current[0] <= t < current[0] + current[1]:
            if current[2] == "stop":
                v_target = 0.0
            else:
                v_target = base_v * rng.uniform(0.35, 0.6)
        if t < warmup_end:
            v_target *= 0.75 + 0.25 * (t / warmup_end)
        v += theta * (v_target - v) * dt + sigma_v * math.sqrt(dt) * rng.gauss(0, 1)
        v = max(0.0, min(v, 4.6))
        if current and current[2] == "stop" and current[0] <= t < current[0] + current[1]:
            stop_seconds += dt
            s_true += max(v, 0.0) * dt
        else:
            s_true += v * dt
            cadence += cadence_theta * (170 - cadence) * dt + 0.8 * math.sqrt(dt) * rng.gauss(0, 1)
            cadence = max(150.0, min(cadence, 182.0))
            steps += cadence / 60.0 * dt
        t += dt
        lat, lon = walker.point_at(s_true)
        noise_x = rho * noise_x + math.sqrt(1 - rho * rho) * sigma_pos * rng.gauss(0, 1)
        noise_y = rho * noise_y + math.sqrt(1 - rho * rho) * sigma_pos * rng.gauss(0, 1)
        lat_rep = lat + noise_y / 110540.0
        lon_rep = lon + noise_x / (111320.0 * math.cos(math.radians(lat)))
        if rng.random() < 0.015:
            lat_rep += rng.gauss(0, 3.0) / 110540.0
            lon_rep += rng.gauss(0, 3.0) / (111320.0 * math.cos(math.radians(lat)))
        alt += 0.08 * (altitude_base + 0.2 - alt) * dt + 0.35 * math.sqrt(dt) * rng.gauss(0, 1)
        alt += 0.12 * math.sin(t / 90.0)
        if prev_clean is not None:
            total_reported += haversine(prev_clean[0], prev_clean[1], lat, lon)
        prev_clean = (lat, lon)
        samples.append(
            {
                "latitude": round(lat_rep, 7),
                "longitude": round(lon_rep, 7),
                "altitude": round(alt, 3),
                "_t": t,
                "_steps": steps,
                "_stop": stop_seconds,
            }
        )
    return {
        "samples": samples,
        "elapsed_s": t,
        "stop_s": stop_seconds,
        "step_number": int(round(steps)),
        "raw_total_m": total_reported,
    }
