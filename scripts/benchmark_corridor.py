"""Benchmark corridor matching against the imported stations.

Usage:  python scripts/benchmark_corridor.py [route.json]

Without an argument it uses a SYNTHETIC stress route (Los Angeles -> New York through fixed
waypoints, densified to about one vertex per 0.1 mile with a small deterministic wiggle). It is
not a real road route. A real ORS response saved as JSON can be passed instead
({"coordinates": [[lon, lat], ...], "distance_mi": ...}).
"""

import json
import math
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

from routing import geo  # noqa: E402
from routing.models import FuelStation  # noqa: E402

WAYPOINTS = [  # (lat, lon)
    (34.05, -118.24), (35.20, -111.65), (35.08, -106.65), (35.22, -101.83), (35.47, -97.52),
    (38.63, -90.20), (39.77, -86.16), (39.96, -83.00), (40.44, -79.99), (40.27, -76.88), (40.71, -74.01),
]


def synthetic_route(step_mi=0.1, wiggle_mi=0.3):
    coords = []
    for (lat1, lon1), (lat2, lon2) in zip(WAYPOINTS, WAYPOINTS[1:]):
        n = max(1, int(geo.haversine_mi(lat1, lon1, lat2, lon2) / step_mi))
        for k in range(n):
            f = k / n
            lat, lon = lat1 + f * (lat2 - lat1), lon1 + f * (lon2 - lon1)
            offset = wiggle_mi * math.sin(len(coords) / 37.0) / 69.0  # deterministic lateral wiggle, degrees
            coords.append([lon + offset, lat - offset])
    coords.append([WAYPOINTS[-1][1], WAYPOINTS[-1][0]])
    geometry = math.fsum(geo.haversine_mi(a[1], a[0], b[1], b[0]) for a, b in zip(coords, coords[1:]))
    return coords, geometry * 1.02, 'SYNTHETIC stress route LA->NYC (not a real road route)'


def main():
    if len(sys.argv) > 1:
        data = json.loads(Path(sys.argv[1]).read_text())
        coords, distance, label = data['coordinates'], data['distance_mi'], f'route fixture {sys.argv[1]}'
    else:
        coords, distance, label = synthetic_route()
    stations = list(FuelStation.objects.exclude(latitude=None))
    radius = settings.FUEL_CORRIDOR_MILES

    t0 = time.perf_counter()
    route = geo.prepare_route(coords, distance)
    t1 = time.perf_counter()
    visits = geo.find_station_visits(route, stations, radius)
    t2 = time.perf_counter()

    print(label)
    print(f'vertices: {len(route.lats)}  segments: {len(route.seg_miles)}  distance: {route.distance_mi:.1f} mi  '
          f'geometry: {route.geometry_mi:.1f} mi  corridor: {radius} mi')
    print(f'located stations: {len(stations)}  unique points: {len({(s.latitude, s.longitude) for s in stations})}')
    print(f'prepare_route: {t1 - t0:.3f} s   find_station_visits: {t2 - t1:.3f} s')
    print(f'visits: {len(visits)}  distinct stations visited: {len({v.station.pk for v in visits})}')

    # Spot-check the prefiltered result against the unfiltered reference on a deterministic sample.
    boxes = geo.segment_boxes(route, radius)
    points = sorted({(s.latitude, s.longitude) for s in stations})
    sample = random.Random(4).sample(points, 60) + sorted({(v.station.latitude, v.station.longitude) for v in visits})[:40]
    mismatches = 0
    t3 = time.perf_counter()
    for lat, lon in sample:
        reference = geo.point_visits(route, lat, lon, radius)
        filtered = geo.point_visits(route, lat, lon, radius, geo._candidate_segments(boxes, lat, lon))
        mismatches += reference != filtered
    print(f'reference check on {len(sample)} points: {mismatches} mismatches ({time.perf_counter() - t3:.1f} s)')


if __name__ == '__main__':
    main()
