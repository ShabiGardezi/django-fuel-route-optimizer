import math
import random
from decimal import Decimal

from django.test import SimpleTestCase, override_settings

from routing import geo
from routing.models import FuelStation

R = geo.EARTH_RADIUS_MI


def to_lonlat(x, y, lat0, lon0):
    """Inverse of the station-centred plane: mile offsets (x east, y north) -> [lon, lat]."""
    lat = lat0 + math.degrees(y / R)
    lon = lon0 + math.degrees(x / (R * math.cos(math.radians(lat0))))
    return [lon, lat]


def plane_route(points, lat0=40.0, lon0=-100.0, distance_mi=None):
    coords = [to_lonlat(x, y, lat0, lon0) for x, y in points]
    if distance_mi is None:
        distance_mi = math.fsum(geo.haversine_mi(a[1], a[0], b[1], b[0]) for a, b in zip(coords, coords[1:]))
    return geo.prepare_route(coords, distance_mi)


def station(opis_id, lat, lon, price='3.000'):
    return FuelStation(opis_id=opis_id, latitude=lat, longitude=lon, lowest_price=Decimal(price))


def great_circle_distance_to_segment(lat, lon, a, b, samples=4000):
    """Dense-sampling oracle: great-circle distance from (lat, lon) to the lat/lon-straight segment a->b."""
    return min(
        geo.haversine_mi(lat, lon, a[0] + k / samples * (b[0] - a[0]), a[1] + k / samples * (b[1] - a[1]))
        for k in range(samples + 1)
    )


class ProjectionTests(SimpleTestCase):
    def test_interior_projection(self):
        distance, t = geo._project(-5.0, 3.0, 5.0, 3.0)
        self.assertAlmostEqual(distance, 3.0, places=12)
        self.assertAlmostEqual(t, 0.5, places=12)

    def test_projection_clamps_to_endpoints(self):
        self.assertEqual(geo._project(2.0, 0.0, 6.0, 0.0), (2.0, 0.0))
        self.assertEqual(geo._project(-6.0, 0.0, -2.0, 0.0), (2.0, 1.0))

    def test_zero_length_segment_uses_its_start(self):
        self.assertEqual(geo._project(3.0, 4.0, 3.0, 4.0), (5.0, 0.0))
        self.assertEqual(geo._segment_interval(3.0, 4.0, 3.0, 4.0, True, True, 10.0)[2:], (0.0, 1.0))
        self.assertEqual(geo._segment_interval(30.0, 40.0, 30.0, 40.0, False, False, 10.0)[2:], (None, None))

    def test_corridor_distance_uses_station_plane(self):
        route = plane_route([(-5, 3), (5, 3)])
        distance, t = geo.corridor_distance_mi(40.0, -100.0, (route.lats[0], route.lons[0]), (route.lats[1], route.lons[1]))
        self.assertAlmostEqual(distance, 3.0, places=9)
        self.assertAlmostEqual(t, 0.5, places=9)

    def test_planar_distance_matches_great_circle_oracle(self):
        rng = random.Random(7)
        worst_abs = worst_rel = 0.0
        for _ in range(150):
            lat0, lon0 = rng.uniform(25, 70), rng.uniform(-120, -70)
            a = to_lonlat(rng.uniform(-15, 15), rng.uniform(-15, 15), lat0, lon0)
            angle, length = rng.uniform(0, 2 * math.pi), rng.uniform(0.5, 20)
            end = (math.cos(angle) * length, math.sin(angle) * length)
            start_xy = ((a[0] - lon0) * math.radians(1) * R * math.cos(math.radians(lat0)), (a[1] - lat0) * math.radians(1) * R)
            b = to_lonlat(start_xy[0] + end[0], start_xy[1] + end[1], lat0, lon0)
            planar, _ = geo.corridor_distance_mi(lat0, lon0, (a[1], a[0]), (b[1], b[0]))
            oracle = great_circle_distance_to_segment(lat0, lon0, (a[1], a[0]), (b[1], b[0]))
            if oracle <= 20:
                worst_abs = max(worst_abs, abs(planar - oracle))
                worst_rel = max(worst_rel, abs(planar - oracle) / max(oracle, 1.0))
        # Measured worst case is recorded in the step report; these bounds leave headroom.
        self.assertLess(worst_abs, 0.05)
        self.assertLess(worst_rel, 0.005)


class RouteTests(SimpleTestCase):
    def test_route_miles_scale_to_ors_distance(self):
        route = plane_route([(0, 0), (30, 0), (30, 40)], distance_mi=None)
        scaled = plane_route([(0, 0), (30, 0), (30, 40)], distance_mi=route.geometry_mi * 1.1)
        self.assertEqual(scaled.cum_miles[-1], scaled.distance_mi)
        self.assertAlmostEqual(scaled.cum_miles[1] / scaled.distance_mi, route.cum_miles[1] / route.distance_mi, places=12)
        self.assertAlmostEqual(math.fsum(scaled.seg_miles), scaled.distance_mi, places=9)
        self.assertAlmostEqual(scaled.cum_miles[1], 30 * 1.1, delta=0.05)  # planar 30 mi leg, scaled by 1.1

    def test_consecutive_duplicates_are_removed(self):
        coords = [[-100.0, 40.0], [-100.0, 40.0], [-99.9, 40.0], [-99.9, 40.0], [-99.9, 40.0], [-99.8, 40.0]]
        route = geo.prepare_route(coords, 10.0)
        self.assertEqual(route.removed_duplicates, 3)
        self.assertEqual(len(route.lats), 3)
        self.assertTrue(all(length > 0 for length in route.seg_miles))

    def test_invalid_routes_are_rejected(self):
        for coords, distance in [
            ([[-100.0, 40.0], [-100.0, 40.0]], 5.0),  # one distinct point
            ([[-100.0, 40.0], [-99.0, 40.0]], 0.0),  # non-positive distance
            ([[-100.0, 40.0], [-99.0, 95.0]], 5.0),  # latitude out of range
            ([[179.5, 50.0], [-179.5, 50.0]], 5.0),  # antimeridian
        ]:
            with self.assertRaises(ValueError):
                geo.prepare_route(coords, distance)


class VisitTests(SimpleTestCase):
    def visits(self, points, radius=10.0):
        route = plane_route(points)
        return route, geo.point_visits(route, 40.0, -100.0, radius)

    def test_segment_passing_through_with_both_endpoints_outside(self):
        route, visits = self.visits([(-20, 5), (20, 5)])
        self.assertEqual(len(visits), 1)
        entry, exit_, mile, offset = visits[0]
        half_chord = math.sqrt(75)
        length = route.seg_miles[0]
        self.assertAlmostEqual(entry, (20 - half_chord) / 40 * length, delta=1e-6)
        self.assertAlmostEqual(exit_, (20 + half_chord) / 40 * length, delta=1e-6)
        self.assertAlmostEqual(mile, 0.5 * length, delta=1e-6)
        self.assertAlmostEqual(offset, 5.0, delta=1e-6)

    def test_exact_tangent_gives_zero_length_interval(self):
        distance, t, lo, hi = geo._segment_interval(-20.0, 10.0, 20.0, 10.0, False, False, 10.0)
        self.assertEqual((distance, t, lo, hi), (10.0, 0.5, 0.5, 0.5))

    def test_near_tangent_route_produces_zero_length_visit(self):
        route = plane_route([(-20, 10), (20, 10)])
        offset, _ = geo.corridor_distance_mi(40.0, -100.0, (route.lats[0], route.lons[0]), (route.lats[1], route.lons[1]))
        visits = geo.point_visits(route, 40.0, -100.0, offset)  # radius exactly equal to the closest distance
        self.assertEqual(len(visits), 1)
        entry, exit_, mile, _ = visits[0]
        self.assertLessEqual(entry, mile)
        self.assertLessEqual(mile, exit_)
        self.assertLess(exit_ - entry, 1e-3)
        self.assertEqual(geo.point_visits(route, 40.0, -100.0, offset * (1 - 1e-9)), [])

    def test_intervals_join_through_inside_vertex(self):
        _, visits = self.visits([(-20, 0), (0, 5), (20, 0)])
        self.assertEqual(len(visits), 1)

    def test_v_shape_with_outside_vertex_gives_two_visits(self):
        # Both arms pass about 9.4 mi from the station; the shared vertex is 15 mi away (outside).
        _, visits = self.visits([(-20, -10), (0, 15), (20, -10)])
        self.assertEqual(len(visits), 2)
        self.assertLess(visits[0][1], visits[1][0])
        for visit in visits:
            self.assertLess(visit[3], 10.0)

    def test_loop_inside_corridor_is_one_visit(self):
        _, visits = self.visits([(-30, -3), (-3, -3), (3, -3), (3, 3), (-3, 3), (-30, 3)])
        self.assertEqual(len(visits), 1)

    def test_out_and_back_gives_two_visits(self):
        _, visits = self.visits([(-30, 2), (30, 2), (30, -2), (-30, -2)])
        self.assertEqual(len(visits), 2)
        first, second = visits
        self.assertLess(first[1], second[0])
        self.assertNotEqual(first[2], second[2])

    def test_duplicate_vertices_do_not_change_visits(self):
        points = [(-20, -10), (0, 15), (20, -10)]
        coords = [to_lonlat(x, y, 40.0, -100.0) for x, y in points]
        doubled = [coords[0], coords[0], coords[1], coords[1], coords[2]]
        distance = 60.0
        clean = geo.point_visits(geo.prepare_route(coords, distance), 40.0, -100.0, 10.0)
        noisy = geo.point_visits(geo.prepare_route(doubled, distance), 40.0, -100.0, 10.0)
        self.assertEqual(clean, noisy)

    def test_interval_boundaries_match_dense_sampling(self):
        rng = random.Random(11)
        for _ in range(200):
            ax, ay, bx, by = (rng.uniform(-25, 25) for _ in range(4))
            radius = 10.0
            distance, t_closest, lo, hi = geo._segment_interval(
                ax, ay, bx, by, math.hypot(ax, ay) <= radius, math.hypot(bx, by) <= radius, radius
            )
            n = 20000
            inside = [k / n for k in range(n + 1) if math.hypot(ax + k / n * (bx - ax), ay + k / n * (by - ay)) <= radius]
            if lo is None:
                self.assertEqual(inside, [])
                continue
            self.assertTrue(0.0 <= lo <= t_closest <= hi <= 1.0)
            if inside:
                self.assertAlmostEqual(lo, inside[0], delta=1.5 / n)
                self.assertAlmostEqual(hi, inside[-1], delta=1.5 / n)

    def test_roots_close_to_segment_ends(self):
        radius = 10.0
        cases = [
            (0.0, 0.0, 10.0, 0.0),  # ends exactly on the circle
            (10.0, 0.0, 20.0, 0.0),  # starts exactly on the circle, heads away
            (10.0 + 1e-12, 0.0, 20.0, 0.0),  # starts just outside
            (-10.0 + 1e-12, 0.0, 0.0, 0.0),  # starts just inside
            (-10.0, 1e-9, 10.0, 1e-9),  # chord through the centre ending on the circle
        ]
        for ax, ay, bx, by in cases:
            a_in, b_in = math.hypot(ax, ay) <= radius, math.hypot(bx, by) <= radius
            distance, t, lo, hi = geo._segment_interval(ax, ay, bx, by, a_in, b_in, radius)
            if distance > radius:
                self.assertIsNone(lo)
            else:
                self.assertTrue(0.0 <= lo <= t <= hi <= 1.0, (ax, ay, bx, by, lo, t, hi))
                if a_in:
                    self.assertEqual(lo, 0.0)
                if b_in:
                    self.assertEqual(hi, 1.0)
        self.assertEqual(geo._segment_interval(10.0, 0.0, 20.0, 0.0, True, False, radius)[2:], (0.0, 0.0))

    def test_high_latitude_visit(self):
        route = plane_route([(-30, 4), (30, 4)], lat0=69.5, lon0=-150.0)
        visits = geo.point_visits(route, 69.5, -150.0, 10.0)
        self.assertEqual(len(visits), 1)
        self.assertAlmostEqual(visits[0][3], 4.0, delta=1e-6)


class PrefilterTests(SimpleTestCase):
    def random_route(self, rng, lat0, lon0, n=60):
        x = y = 0.0
        points = []
        for _ in range(n):
            points.append((x, y))
            angle = rng.uniform(0, 2 * math.pi)
            step = rng.choice([0.3, 2.0, 8.0, 25.0])
            x, y = x + math.cos(angle) * step, y + math.sin(angle) * step
        return plane_route(points, lat0, lon0)

    def test_prefilter_matches_reference(self):
        rng = random.Random(3)
        checked = with_visits = 0
        for case in range(40):
            lat0, lon0 = rng.uniform(25, 70), rng.uniform(-125, -67)
            route = self.random_route(rng, lat0, lon0)
            radius = rng.choice([1.0, 5.0, 10.0, 25.0])
            boxes = geo.segment_boxes(route, radius)
            for _ in range(60):
                i = rng.randrange(len(route.seg_miles))
                f = rng.random()
                base_lat = route.lats[i] + f * (route.lats[i + 1] - route.lats[i])
                base_lon = route.lons[i] + f * (route.lons[i + 1] - route.lons[i])
                # Points scattered around the route, many near the corridor edge.
                dist = rng.choice([rng.uniform(0, 2 * radius), radius * (1 - 1e-7), radius * (1 + 1e-7)])
                angle = rng.uniform(0, 2 * math.pi)
                lon, lat = to_lonlat(dist * math.cos(angle), dist * math.sin(angle), base_lat, base_lon)
                reference = geo.point_visits(route, lat, lon, radius)
                candidates = geo._candidate_segments(boxes, lat, lon)
                self.assertEqual(geo.point_visits(route, lat, lon, radius, candidates), reference)
                # Guarantee: every segment within the radius is a candidate.
                for s in range(len(route.seg_miles)):
                    d, _ = geo.corridor_distance_mi(lat, lon, (route.lats[s], route.lons[s]), (route.lats[s + 1], route.lons[s + 1]))
                    if d <= radius:
                        self.assertIn(s, candidates)
                checked += 1
                with_visits += bool(reference)
        self.assertEqual(checked, 2400)
        self.assertGreater(with_visits, 600)

    @override_settings(FUEL_CORRIDOR_MILES=5.0)
    def test_find_station_visits_uses_setting_and_skips_unlocated(self):
        route = plane_route([(-30, 0), (30, 0)])
        near = to_lonlat(0, 4, 40.0, -100.0)
        far = to_lonlat(0, 7, 40.0, -100.0)
        stations = [
            station(1, near[1], near[0], '3.5'),
            station(2, near[1], near[0], '3.1'),  # same point, cheaper
            station(3, far[1], far[0]),  # outside 5 mi, inside 10 mi
            station(4, None, None),
        ]
        visits = geo.find_station_visits(route, stations)
        self.assertEqual([v.station.opis_id for v in visits], [2, 1])
        self.assertEqual(len(geo.find_station_visits(route, stations, corridor_mi=10.0)), 3)

    def test_find_station_visits_keeps_separate_visits_per_station(self):
        route = plane_route([(-30, 2), (30, 2), (30, -2), (-30, -2)])
        stop = station(9, 40.0, -100.0)
        visits = geo.find_station_visits(route, [stop], corridor_mi=10.0)
        self.assertEqual([v.visit_index for v in visits], [0, 1])
        self.assertIs(visits[0].station, visits[1].station)
        self.assertLess(visits[0].exit_mile, visits[1].entry_mile)

    def test_invalid_radius_is_rejected(self):
        route = plane_route([(-30, 0), (30, 0)])
        for radius in (0, -1, float('nan')):
            with self.assertRaises(ValueError):
                geo.find_station_visits(route, [], corridor_mi=radius)
