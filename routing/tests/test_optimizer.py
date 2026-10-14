import itertools
import random
from decimal import ROUND_HALF_UP, Decimal

from django.test import SimpleTestCase

from routing.geo import StationVisit
from routing.models import FuelStation
from routing.optimizer import (
    MAX_RANGE_MILES, TANK_GALLONS, Candidate, plan_fuel, plan_route_fuel, sort_candidates, to_miles,
)

D = Decimal
PRICES = [D('2.899'), D('3.0'), D('3.00733333'), D('3.199'), D('3.25'), D('3.5'), D('4.1'), D('5')]


def cand(mile, price, station_id=1, visit_index=0):
    return Candidate(D(str(mile)), D(str(price)), station_id, visit_index)


def plan(distance, candidates):
    return plan_fuel(D(str(distance)), sort_candidates(candidates))


# ---------------------------------------------------------------- reference solvers (test-only)

def reference_cost(distance_units, stations, capacity_units, unit_gallons):
    """Exact DP over whole fuel units. Origin: full tank, free, no purchase. Stations: buy 0..room.

    ``stations`` are (position_units, price) pairs; positions and capacity in fuel units
    (1 unit of fuel covers 1 unit of distance). Returns the minimum Decimal cost or None.
    """
    INF = None
    dp = [INF] * (capacity_units + 1)
    dp[capacity_units] = D('0')
    previous = 0
    for position, price in sorted(stations):
        leg = position - previous
        moved = [INF] * (capacity_units + 1)
        for g in range(leg, capacity_units + 1):
            moved[g - leg] = dp[g]
        unit_cost = price * unit_gallons
        best = INF
        for g in range(capacity_units + 1):
            step = None if best is None else best + unit_cost  # buy one more unit than at g-1
            here = moved[g]
            best = here if step is None else (step if here is None or step < here else here)
            moved[g] = best
        dp, previous = moved, position
    leg = distance_units - previous
    finals = [dp[g] for g in range(leg, capacity_units + 1) if dp[g] is not None]
    return min(finals) if finals else None


def brute_force_cost(distance_units, stations, capacity_units, unit_gallons):
    """Enumerate every whole-unit purchase at every station (tiny cases only)."""
    stations = sorted(stations)
    best = None
    for buys in itertools.product(range(capacity_units + 1), repeat=len(stations)):
        fuel, previous, cost, ok = capacity_units, 0, D('0'), True
        for (position, price), buy in zip(stations, buys):
            fuel -= position - previous
            if fuel < 0 or fuel + buy > capacity_units:
                ok = False
                break
            fuel += buy
            cost += buy * price * unit_gallons
            previous = position
        if ok and fuel - (distance_units - previous) >= 0 and (best is None or cost < best):
            best = cost
    return best


def reference_for(distance, candidates, unit_gallons):
    """Reference cost for an optimizer instance (50 gal, 10 mpg) at the given fuel unit."""
    unit_miles = unit_gallons * 10

    def units(miles):
        value = D(miles) / unit_miles
        assert value == value.to_integral_value(), 'positions must be multiples of the unit'
        return int(value)

    return reference_cost(
        units(distance), [(units(c.mile), c.price) for c in candidates], int(TANK_GALLONS / unit_gallons), unit_gallons,
    )


# ---------------------------------------------------------------- invariant checks

class PlanChecks:
    def assert_consistent(self, result):
        fuel, previous = TANK_GALLONS, D('0')
        for stop in result.stops:
            self.assertGreaterEqual(stop.mile, previous)
            fuel -= (stop.mile - previous) / 10
            self.assertEqual(stop.arrival_fuel_gal, fuel)
            self.assertGreaterEqual(fuel, 0)
            self.assertGreater(stop.gallons, 0)
            self.assertLessEqual(fuel + stop.gallons, TANK_GALLONS)
            self.assertEqual(stop.cost, stop.gallons * stop.price)
            fuel += stop.gallons
            previous = stop.mile
        if result.feasible:
            fuel -= (result.distance_mi - previous) / 10
            self.assertEqual(result.ending_fuel_gal, fuel)
            self.assertTrue(0 <= result.ending_fuel_gal <= TANK_GALLONS)
            self.assertEqual(
                result.starting_fuel_gal + result.fuel_purchased_gal - result.fuel_consumed_gal,
                result.ending_fuel_gal,
            )
        self.assertEqual(result.fuel_consumed_gal, result.distance_mi / 10)
        self.assertEqual(result.cost_unrounded, sum((s.cost for s in result.stops), D('0')))
        self.assertEqual(result.total_cost, cents(result.cost_unrounded))


def cents(value):
    return value.quantize(D('0.01'), rounding=ROUND_HALF_UP)


class OptimizerRuleTests(PlanChecks, SimpleTestCase):
    def test_destination_within_range_needs_no_stops(self):
        result = plan(450, [cand(100, 2.899)])
        self.assertTrue(result.feasible)
        self.assertEqual(result.stops, ())
        self.assertEqual(result.total_cost, D('0.00'))
        self.assertEqual(result.ending_fuel_gal, D('5'))
        self.assert_consistent(result)

    def test_no_reachable_station_from_origin_is_infeasible(self):
        result = plan(900, [cand('500.001', 3)])
        self.assertFalse(result.feasible)
        self.assertEqual((result.gap_start_mi, result.gap_end_mi), (D('0'), D('500.001')))
        self.assertEqual(result.stops, ())
        self.assertIsNone(result.ending_fuel_gal)

    def test_origin_example_skips_expensive_first_station(self):
        result = plan(700, [cand(50, 5, station_id=1), cand(300, 3, station_id=2)])
        self.assertTrue(result.feasible)
        self.assertEqual([(s.station_id, s.gallons, s.price) for s in result.stops], [(2, D('20'), D('3'))])
        self.assertEqual(result.stops[0].arrival_fuel_gal, D('20'))
        self.assertEqual(result.cost_unrounded, D('60'))
        self.assertEqual(result.total_cost, D('60.00'))
        self.assertEqual(result.ending_fuel_gal, D('0'))
        self.assert_consistent(result)
        self.assertEqual(reference_for(D('700'), [cand(50, 5), cand(300, 3)], D('0.1')), D('60'))

    def test_origin_price_tie_picks_nearest_then_ids(self):
        result = plan(900, [cand(400, 3, station_id=7), cand(100, 3, station_id=9), cand(100, 3, station_id=8)])
        # Cheapest $3 at mile 100 (IDs 8 and 9 tie; 8 wins); never buys at the origin.
        self.assertEqual(result.stops[0].station_id, 8)
        self.assertEqual(result.stops[0].mile, D('100'))
        self.assertEqual(result.stops[0].arrival_fuel_gal, D('40'))
        self.assert_consistent(result)

    def test_buys_only_enough_to_reach_cheaper_station(self):
        result = plan(1200, [cand(100, 4, 1), cand(550, 3, 2), cand(800, 2.899, 3)])
        self.assertEqual([s.station_id for s in result.stops], [1, 2, 3])
        first = result.stops[0]
        self.assertEqual(first.gallons, D('5'))  # 450 mi to the $3 station, 40 gal on board
        self.assertEqual(result.stops[1].arrival_fuel_gal, D('0'))
        self.assert_consistent(result)

    def test_fills_tank_when_no_cheaper_station_reachable(self):
        result = plan(1300, [cand(450, 3, 1), cand(850, 4, 2), cand(900, 5, 3)])
        self.assertEqual(result.stops[0].station_id, 1)
        self.assertEqual(result.stops[0].arrival_fuel_gal + result.stops[0].gallons, TANK_GALLONS)
        self.assert_consistent(result)
        self.assertEqual(result.cost_unrounded, reference_for(D('1300'), [cand(450, 3), cand(850, 4), cand(900, 5)], D('0.1')))

    def test_destination_reachable_with_current_fuel_buys_nothing(self):
        # At the origin: stations exist but the free tank already covers the route.
        result = plan('499.999', [cand(10, 2.899, 1), cand(300, 3, 2)])
        self.assertEqual(result.stops, ())
        self.assertEqual(result.total_cost, D('0.00'))
        # At a station within full-tank range of the destination: buy only what finishing needs (40 gal),
        # not a full tank, and ignore the equal-price twin and the pricier station ahead.
        result = plan(900, [cand(400, 3, 1), cand(400, 3, 2), cand(450, 3.5, 3)])
        self.assertEqual([(s.station_id, s.gallons) for s in result.stops], [(1, D('40'))])
        self.assertEqual(result.ending_fuel_gal, D('0'))
        self.assert_consistent(result)
        # The last purchase is only what the remaining distance needs.
        result = plan(960, [cand(10, 9, 1), cand(490, 3, 2)])
        self.assertEqual([(s.station_id, s.arrival_fuel_gal, s.gallons) for s in result.stops], [(2, D('1'), D('46'))])
        self.assertEqual(result.ending_fuel_gal, D('0'))

    def test_exact_and_fractional_500_mile_boundaries(self):
        self.assertEqual(plan('500.000', []).stops, ())
        self.assertTrue(plan('500.000', []).feasible)
        self.assertFalse(plan('500.001', []).feasible)
        reachable = plan('900', [cand('500.000', 3)])
        self.assertTrue(reachable.feasible)
        self.assertEqual(reachable.stops[0].arrival_fuel_gal, D('0'))
        self.assertFalse(plan('900', [cand('500.001', 3)]).feasible)
        # Gaps between stations: exactly 500 is allowed, 500.001 is not.
        self.assertTrue(plan('1400', [cand(400, 3, 1), cand(900, 3, 2)]).feasible)
        gap = plan('1400', [cand(400, 3, 1), cand('900.001', 3, 2)])
        self.assertFalse(gap.feasible)
        self.assertEqual((gap.gap_start_mi, gap.gap_end_mi), (D('400'), D('900.001')))
        self.assertTrue(plan('1400', [cand(400, 3, 1), cand(900, 3, 2), cand('1400', 3, 3)]).feasible)
        fractional = plan('1000.005', [cand('499.999', '3.007'), cand('750.004', '3.1')])
        self.assert_consistent(fractional)
        self.assertEqual(
            fractional.cost_unrounded,
            reference_for(D('1000.005'), [cand('499.999', '3.007'), cand('750.004', '3.1')], D('0.0001')),
        )

    def test_unreachable_gap_between_stations_reports_gap(self):
        result = plan(1500, [cand(300, 3, 1), cand(850, 3, 2)])
        self.assertFalse(result.feasible)
        self.assertEqual((result.gap_start_mi, result.gap_end_mi), (D('300'), D('850')))
        self.assertIn('mile 300', result.reason)

    def test_multiple_visits_and_same_mile_stations(self):
        candidates = [
            cand(200, 3.5, station_id=5, visit_index=0),
            cand(450, 3.0, station_id=6, visit_index=0),
            cand(450, 3.0, station_id=4, visit_index=0),  # same mile and price, lower ID
            cand(800, 3.5, station_id=5, visit_index=1),  # second visit of station 5
        ]
        result = plan(1250, candidates)
        self.assertEqual([(s.station_id, s.visit_index) for s in result.stops], [(4, 0), (5, 1)])
        self.assert_consistent(result)
        self.assertEqual(result.cost_unrounded, reference_for(D('1250'), candidates, D('0.1')))

    def test_zero_purchase_visits_are_not_stops(self):
        # Station 1 fills up and moves to station 2 at the same mile and price (the nearest cheapest);
        # station 2 is visited with a full tank, buys nothing, and must not be listed.
        result = plan(1300, [cand(400, 3, 1), cand(400, 3, 2), cand(850, 3.5, 3)])
        self.assertEqual([(s.station_id, s.gallons) for s in result.stops], [(1, D('40')), (3, D('40'))])
        self.assertTrue(all(s.gallons > 0 for s in result.stops))
        self.assert_consistent(result)

    def test_decimal_accounting_and_rounding(self):
        candidates = [cand('400.001', '3.00733333', 1), cand('800.003', '3.00733334', 2), cand('1100.007', '2.89900001', 3)]
        result = plan('1500.009', candidates)
        self.assert_consistent(result)
        self.assertNotEqual(result.cost_unrounded, result.total_cost)
        display_sum = sum(s.cost_display for s in result.stops)
        self.assertLessEqual(abs(display_sum - result.total_cost), D('0.005') * len(result.stops) + D('0.005'))
        for stop in result.stops:
            self.assertIsInstance(stop.gallons, Decimal)
            self.assertEqual(stop.cost_display, cents(stop.cost))

    def test_display_rounding_can_differ_from_total(self):
        # Each stop costs x.xx5 exactly: every displayed cost rounds up, the total rounds once.
        candidates = [cand(450, '3.0001', 1), cand(500, '3.00005', 2), cand(950, '3.0001', 3)]
        result = plan(1400, candidates)
        self.assert_consistent(result)
        display_sum = sum(s.cost_display for s in result.stops)
        self.assertEqual(result.total_cost, cents(result.cost_unrounded))
        self.assertNotEqual(display_sum, result.total_cost)

    def test_float_distance_is_rejected(self):
        with self.assertRaises(TypeError):
            plan_fuel(700.0, [])
        with self.assertRaises(ValueError):
            plan_fuel(D('700'), [cand(800, 3)])
        with self.assertRaises(ValueError):
            plan_fuel(D('900'), [cand(300, 3), cand(100, 3)])  # not sorted

    def test_route_visits_are_converted_and_ordered_deterministically(self):
        a = FuelStation(opis_id=2, lowest_price=D('3.000'))
        b = FuelStation(opis_id=1, lowest_price=D('3.000'))
        visits = [
            StationVisit(a, 0, 290.0, 310.0, 300.0004, 1.2),
            StationVisit(b, 0, 290.0, 310.0, 300.0004, 2.5),
            StationVisit(a, 1, 640.0, 660.0, 650.0, 0.4),
        ]
        first = plan_route_fuel(900.0, visits)
        second = plan_route_fuel(900.0, list(reversed(visits)))
        self.assertEqual(first, second)
        self.assertEqual(first.stops[0].station_id, 1)  # same mile and price: lower OPIS ID
        self.assertEqual(first.stops[0].mile, D('300.000'))
        self.assertIs(first.stops[0].station, b)
        self.assertEqual(to_miles(1.0005), D('1.000') if format(1.0005, '.3f') == '1.000' else D('1.001'))


class ReferenceSolverTests(SimpleTestCase):
    def test_reference_matches_brute_force_on_tiny_cases(self):
        rng = random.Random(5)
        compared = 0
        for _ in range(300):
            capacity = rng.randint(1, 4)
            distance = rng.randint(1, 12)
            stations = [(rng.randint(0, distance), D(rng.choice(['1', '2', '3', '1.5']))) for _ in range(rng.randint(0, 3))]
            unit = D('0.5')
            self.assertEqual(reference_cost(distance, stations, capacity, unit), brute_force_cost(distance, stations, capacity, unit))
            compared += 1
        self.assertEqual(compared, 300)


def random_case(rng, origin_focused):
    """Random instance; integer miles in general cases, half-mile positions in origin-focused cases."""
    if origin_focused:
        distance = D(rng.randint(1002, 2000)) / 2  # 501.0 .. 1000.0
        stations = []
        expensive = D('5.25')
        for k in range(rng.randint(2, 6)):  # several stations inside the initial 500-mile range
            mile = D(rng.randint(0, 1000)) / 2
            price = expensive if k == 0 else rng.choice(PRICES)  # first generated is the most expensive
            stations.append(Candidate(mile, price, rng.randint(1, 5), 0))
        for _ in range(rng.randint(0, 3)):
            stations.append(Candidate(D(rng.randint(1000, int(distance * 2))) / 2, rng.choice(PRICES), rng.randint(1, 5), 1))
        return distance, stations, D('0.05')
    distance = D(rng.randint(100, 2200))
    stations = []
    for _ in range(rng.randint(0, 8)):
        mile = D(rng.randint(0, int(distance)))
        station_id = rng.randint(1, 6)
        stations.append(Candidate(mile, rng.choice(PRICES), station_id, 0))
    if stations and rng.random() < 0.3:  # same station visited again later at the same price
        again = rng.choice(stations)
        stations.append(Candidate(min(distance, again.mile + D(rng.randint(1, 400))), again.price, again.station_id, 1))
    if stations and rng.random() < 0.3:  # a different station at the same mile
        twin = rng.choice(stations)
        stations.append(Candidate(twin.mile, rng.choice(PRICES), twin.station_id + 10, 0))
    return distance, stations, D('0.1')


class RandomComparisonTests(PlanChecks, SimpleTestCase):
    def compare(self, seed, count, origin_focused):
        rng = random.Random(seed)
        stats = {'cases': 0, 'feasible': 0, 'with_stops': 0}
        for _ in range(count):
            distance, stations, unit = random_case(rng, origin_focused)
            candidates = sort_candidates(stations)
            result = plan_fuel(distance, candidates)
            expected = reference_for(distance, candidates, unit)
            self.assertEqual(result.feasible, expected is not None, (distance, candidates))
            if result.feasible:
                self.assertEqual(result.cost_unrounded, expected, (distance, candidates))
                stats['feasible'] += 1
                stats['with_stops'] += bool(result.stops)
            self.assert_consistent(result)
            stats['cases'] += 1
        return stats

    def test_general_cases_match_reference(self):
        stats = self.compare(seed=1, count=1500, origin_focused=False)
        self.assertEqual(stats['cases'], 1500)
        self.assertGreater(stats['with_stops'], 400)

    def test_origin_cases_match_reference(self):
        stats = self.compare(seed=2, count=500, origin_focused=True)
        self.assertEqual(stats['cases'], 500)
        self.assertGreater(stats['with_stops'], 300)

    def test_half_and_quarter_unit_refinements_agree(self):
        rng = random.Random(9)
        checked = 0
        while checked < 60:
            distance, stations, unit = random_case(rng, origin_focused=False)
            if distance > 1200 or len(stations) > 5:
                continue
            candidates = sort_candidates(stations)
            base = reference_for(distance, candidates, unit)
            self.assertEqual(reference_for(distance, candidates, unit / 2), base)
            self.assertEqual(reference_for(distance, candidates, unit / 4), base)
            checked += 1
