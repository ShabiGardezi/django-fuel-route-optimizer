"""Route geometry and fuel-station corridor visits.

Approximations (documented, not hidden):

* Route coordinates are GeoJSON ``[longitude, latitude]`` pairs as returned by OpenRouteService.
  Each segment is treated as a straight line in latitude/longitude.
* Route miles: segment lengths are great-circle (haversine) lengths, and the cumulative
  lengths are scaled so the last vertex equals the ORS route distance, which is authoritative.
  A position's route mile is therefore proportional to its share of the geometry length.
* Corridor distance: the station and segment are projected onto a local equirectangular plane
  centred on the station (x = R*cos(lat_station)*dlon, y = R*dlat, angles in radians) and the
  planar point-to-segment distance is used. Production code and tests use this one definition.
  It differs slightly from true great-circle distance; tests measure that difference.
* Station positions are themselves approximate Gazetteer points (see FuelStation), so results
  are candidates near the route, not guaranteed station locations.

Prefilter guarantee (bounding boxes in degrees, so no candidate is lost):
if a station P is within W miles of a point Q on segment AB under the planar metric, then
|lat_Q - lat_P| <= W/R and |lon_Q - lon_P| <= W/(R*cos(lat_P)). Q lies in AB's lat/lon box
(it is a convex combination of A and B), and |lat_P| <= max|route lat| + W/R = lat_max, so
cos(lat_P) >= cos(lat_max). P is therefore inside AB's box widened by W/R and
W/(R*cos(lat_max)) (in radians) plus a small epsilon. The route box is the union of segment boxes.
Routes crossing the antimeridian are rejected; the argument assumes longitudes do not wrap.
"""

import math
from dataclasses import dataclass

from django.conf import settings

EARTH_RADIUS_MI = 3958.7613  # mean Earth radius
BOX_EPSILON_DEG = 1e-9  # absorbs floating-point error in the bounding-box comparisons


@dataclass(frozen=True)
class Route:
    lats: tuple  # vertex latitudes, degrees
    lons: tuple  # vertex longitudes, degrees
    cum_miles: tuple  # route mile of each vertex, scaled to distance_mi
    seg_miles: tuple  # scaled length of each segment
    distance_mi: float  # authoritative ORS distance
    geometry_mi: float  # unscaled haversine length of the geometry
    removed_duplicates: int


@dataclass(frozen=True)
class StationVisit:
    """One continuous pass of the route through a station's corridor. Not a database row."""

    station: object  # routing.models.FuelStation
    visit_index: int  # 0-based order of this station's visits along the route
    entry_mile: float
    exit_mile: float
    mile: float  # route mile of the closest approach
    offset_mi: float  # planar distance from the station point to the route at that mile


def haversine_mi(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MI * math.asin(min(1.0, math.sqrt(h)))


def prepare_route(coordinates, distance_mi):
    """Build a Route from GeoJSON [lon, lat] coordinates and the ORS distance in miles."""
    if not (isinstance(distance_mi, (int, float)) and math.isfinite(distance_mi) and distance_mi > 0):
        raise ValueError('route distance must be a positive finite number of miles')
    lats, lons = [], []
    removed = 0
    for point in coordinates:
        if len(point) < 2:
            raise ValueError(f'route coordinate {point!r} must be [longitude, latitude]')
        lon, lat = float(point[0]), float(point[1])
        if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError(f'route coordinate {point!r} is out of range')
        if lats and lat == lats[-1] and lon == lons[-1]:
            removed += 1
            continue
        if lons and abs(lon - lons[-1]) > 180:
            raise ValueError('routes crossing the antimeridian are not supported')
        lats.append(lat)
        lons.append(lon)
    if len(lats) < 2:
        raise ValueError('route needs at least two distinct coordinates')
    raw = [haversine_mi(lats[i], lons[i], lats[i + 1], lons[i + 1]) for i in range(len(lats) - 1)]
    geometry_mi = math.fsum(raw)
    if geometry_mi <= 0:
        raise ValueError('route geometry has zero length')
    scale = distance_mi / geometry_mi
    seg_miles = tuple(length * scale for length in raw)
    cum = [0.0]
    running = 0.0
    for length in raw:
        running += length
        cum.append(running * scale)
    cum[-1] = float(distance_mi)  # exact by definition, avoids accumulated rounding at the end
    return Route(tuple(lats), tuple(lons), tuple(cum), seg_miles, float(distance_mi), geometry_mi, removed)


def _plane(station_lat, station_lon):
    k_x = EARTH_RADIUS_MI * math.cos(math.radians(station_lat))

    def to_plane(lat, lon):
        return k_x * math.radians(lon - station_lon), EARTH_RADIUS_MI * math.radians(lat - station_lat)

    return to_plane


def _project(ax, ay, bx, by):
    """Closest point to the origin on segment AB: (distance, t). Zero-length segments use t=0."""
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    t = 0.0 if length_sq == 0 else min(1.0, max(0.0, -(ax * dx + ay * dy) / length_sq))
    return math.hypot(ax + t * dx, ay + t * dy), t


def corridor_distance_mi(station_lat, station_lon, a, b):
    """Planar distance from a station to segment a->b ((lat, lon) pairs) and the closest point's t in [0, 1]."""
    to_plane = _plane(station_lat, station_lon)
    return _project(*to_plane(*a), *to_plane(*b))


def _segment_interval(ax, ay, bx, by, a_inside, b_inside, radius):
    """Return (distance, t_closest, t_lo, t_hi); t_lo/t_hi are None when the segment never enters the corridor.

    The segment is inside the corridor exactly when its closest distance is <= radius. Endpoints use the
    shared per-vertex inside flags so adjacent segments agree on their common vertex.
    """
    distance, t_closest = _project(ax, ay, bx, by)
    if distance > radius:
        return distance, t_closest, None, None
    dx, dy = bx - ax, by - ay
    a_coef = dx * dx + dy * dy
    lo = hi = t_closest
    if a_coef > 0:
        half_b = ax * dx + ay * dy
        c = ax * ax + ay * ay - radius * radius
        disc = half_b * half_b - a_coef * c
        if disc >= 0:
            root = math.sqrt(disc)
            # Numerically stable roots of a*t^2 + 2*half_b*t + c = 0.
            q = -(half_b + math.copysign(root, half_b))
            if q != 0:
                r1, r2 = sorted((q / a_coef, c / q))
            else:  # half_b == 0 and disc == 0 -> tangent at t = 0
                r1 = r2 = 0.0
            lo, hi = max(0.0, r1), min(1.0, r2)
    if a_inside:
        lo = 0.0
    if b_inside:
        hi = 1.0
    # Rounding can push roots slightly past the closest point; the closest point is inside by definition.
    lo, hi = min(lo, t_closest), max(hi, t_closest)
    return distance, t_closest, lo, hi


def _margins(route, radius):
    lat_margin = math.degrees(radius / EARTH_RADIUS_MI)
    lat_max = max(abs(lat) for lat in route.lats) + lat_margin
    if lat_max >= 89.0:
        raise ValueError('routes this close to a pole are not supported')
    lon_margin = math.degrees(radius / (EARTH_RADIUS_MI * math.cos(math.radians(lat_max))))
    return lat_margin + BOX_EPSILON_DEG, lon_margin + BOX_EPSILON_DEG


def segment_boxes(route, radius):
    """Widened (lat_lo, lat_hi, lon_lo, lon_hi) box for every segment; see the module docstring."""
    lat_m, lon_m = _margins(route, radius)
    lats, lons = route.lats, route.lons
    return [
        (
            min(lats[i], lats[i + 1]) - lat_m, max(lats[i], lats[i + 1]) + lat_m,
            min(lons[i], lons[i + 1]) - lon_m, max(lons[i], lons[i + 1]) + lon_m,
        )
        for i in range(len(lats) - 1)
    ]


def _candidate_segments(boxes, lat, lon):
    return [i for i, (a, b, c, d) in enumerate(boxes) if a <= lat <= b and c <= lon <= d]


def point_visits(route, lat, lon, radius, segments=None):
    """Visits of one point as (entry_mile, exit_mile, closest_mile, offset_mi) tuples.

    ``segments`` restricts the work to the given segment indices (from a prefilter); None checks every
    segment and is the reference behaviour.
    """
    if segments is None:
        segments = range(len(route.seg_miles))
    to_plane = _plane(lat, lon)
    plane_cache = {}

    def vertex(i):
        if i not in plane_cache:
            x, y = to_plane(route.lats[i], route.lons[i])
            plane_cache[i] = (x, y, math.hypot(x, y) <= radius)
        return plane_cache[i]

    visits = []
    current = None  # [entry, exit, best_distance, best_mile, last_segment]
    for i in segments:
        ax, ay, a_inside = vertex(i)
        bx, by, b_inside = vertex(i + 1)
        distance, t_closest, lo, hi = _segment_interval(ax, ay, bx, by, a_inside, b_inside, radius)
        if lo is None:
            continue
        start = route.cum_miles[i]
        length = route.seg_miles[i]
        entry, exit_, closest = start + lo * length, start + hi * length, start + t_closest * length
        joins = current is not None and current[4] == i - 1 and a_inside
        if joins:
            current[1] = exit_
            if distance < current[2]:
                current[2], current[3] = distance, closest
            current[4] = i
        else:
            if current is not None:
                visits.append(current)
            current = [entry, exit_, distance, closest, i]
    if current is not None:
        visits.append(current)
    return [(entry, exit_, mile, distance) for entry, exit_, distance, mile, _ in visits]


def find_station_visits(route, stations, corridor_mi=None):
    """All corridor visits of located stations, ordered by (mile, lowest price, OPIS ID, visit index).

    Stations sharing coordinates are evaluated once. Stations without coordinates are skipped.
    """
    radius = float(settings.FUEL_CORRIDOR_MILES if corridor_mi is None else corridor_mi)
    if not (math.isfinite(radius) and radius > 0):
        raise ValueError('corridor radius must be a positive number of miles')
    boxes = segment_boxes(route, radius)
    route_box = (min(b[0] for b in boxes), max(b[1] for b in boxes), min(b[2] for b in boxes), max(b[3] for b in boxes))

    by_point = {}
    for station in stations:
        if station.latitude is None or station.longitude is None:
            continue
        by_point.setdefault((station.latitude, station.longitude), []).append(station)

    visits = []
    for (lat, lon), members in by_point.items():
        if not (route_box[0] <= lat <= route_box[1] and route_box[2] <= lon <= route_box[3]):
            continue
        segments = _candidate_segments(boxes, lat, lon)
        if not segments:
            continue
        point = point_visits(route, lat, lon, radius, segments)
        for station in members:
            for index, (entry, exit_, mile, offset) in enumerate(point):
                visits.append(StationVisit(station, index, entry, exit_, mile, offset))
    visits.sort(key=lambda v: (v.mile, v.station.lowest_price, v.station.opis_id, v.visit_index))
    return visits
