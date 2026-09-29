"""Shared geometry helpers.

Used by both layers, so it belongs to neither: phantom_track.py generates raw
positions with it, miniapp_client.py re-measures distances with it the way the
mini program's calculateDistance() does.
"""
import math


def haversine(lat1, lon1, lat2, lon2):
    """Distance in metres, using the mini program's calculateDistance formula
    (R = 6371 km, atan2 form - app-service.js ~773517)."""
    n = math.radians(lat2 - lat1)
    a = math.radians(lon2 - lon1)
    l = math.sin(n / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(a / 2) ** 2
    return 6371000.0 * (2 * math.atan2(math.sqrt(l), math.sqrt(1 - l)))


class LocalProjection:
    def __init__(self, lat0, lon0):
        self.lat0 = lat0
        self.lon0 = lon0
        self.kx = 111320.0 * math.cos(math.radians(lat0))
        self.ky = 110540.0

    def to_xy(self, lat, lon):
        return ((lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky)

    def to_latlon(self, x, y):
        return (self.lat0 + y / self.ky, self.lon0 + x / self.kx)
