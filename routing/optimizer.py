"""Greedy fuel-stop optimizer for a fixed route.

Rules: 50-gallon tank, 10 miles per gallon (500-mile full-tank range). The vehicle starts with a
full tank whose fuel is free, cannot buy at the origin, and may pass any station without stopping.
Fuel can be bought in any amount (continuous purchases). Candidate stops are the StationVisit
positions from routing.geo, each priced at its station's lowest listed price.

Strategy (optimal for this model; verified against a reference solver in the tests):
* Origin: if the destination is within range, finish. Otherwise go to the cheapest station within
  range (ties: nearest, then station ID, then visit index), passing every other station.
* At a station: if the destination is reachable with the fuel on board, finish. Otherwise, if a
  strictly cheaper station is within a full-tank range, buy just enough to reach the nearest one.
  Otherwise, if the destination is within a full-tank range, buy just enough to finish. Otherwise
  fill the tank and go to the cheapest station within range (same tie-breaks).

Numbers: route miles come from geometry floats and cross into Decimal once, via a 3-decimal string
(numerical precision of 0.001 mile, not real-world accuracy). Fuel, prices and costs are Decimal.
Stop costs are kept unrounded; only the total is rounded to cents, once (ROUND_HALF_UP). Rounded
stop costs shown for display may therefore not add up exactly to the rounded total.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

TANK_GALLONS = Decimal('50')
MILES_PER_GALLON = Decimal('10')
MAX_RANGE_MILES = TANK_GALLONS * MILES_PER_GALLON  # 500
CENT = Decimal('0.01')


@dataclass(frozen=True)
class Candidate:
    mile: Decimal
    price: Decimal
    station_id: int  # OPIS Truckstop ID
    visit_index: int
    station: object = None  # FuelStation, when available
    offset_mi: Decimal = Decimal('0')


@dataclass(frozen=True)
class FuelStop:
    station_id: int
    visit_index: int
    mile: Decimal
    arrival_fuel_gal: Decimal  # fuel in the tank on arrival, before buying
    gallons: Decimal  # unrounded
    price: Decimal
    cost: Decimal  # unrounded gallons * price
    station: object = None
    offset_mi: Decimal = Decimal('0')

    @property
    def cost_display(self):
        """Stop cost rounded to cents for display only."""
        return self.cost.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class FuelPlan:
    feasible: bool
    distance_mi: Decimal
    stops: tuple
    fuel_consumed_gal: Decimal
    fuel_purchased_gal: Decimal
    ending_fuel_gal: Decimal
    cost_unrounded: Decimal
    total_cost: Decimal  # cost_unrounded rounded to cents, once
    starting_fuel_gal: Decimal = TANK_GALLONS
    gap_start_mi: Decimal = None  # set when infeasible
    gap_end_mi: Decimal = None
    reason: str = ''


def to_miles(value):
    """The single float->Decimal boundary for route miles: round to 0.001 mile via a decimal string."""
    return Decimal(format(float(value), '.3f'))


def candidates_from_visits(visits, distance_mi):
    """Convert geo.StationVisit objects to Candidates, sorted (mile, price, station ID, visit index)."""
    result = []
    for visit in visits:
        mile = min(max(to_miles(visit.mile), Decimal('0')), distance_mi)  # clamp sub-quantum overshoot
        result.append(Candidate(
            mile, visit.station.lowest_price, visit.station.opis_id, visit.visit_index,
            visit.station, to_miles(visit.offset_mi),
        ))
    return sort_candidates(result)


def sort_candidates(candidates):
    return sorted(candidates, key=lambda c: (c.mile, c.price, c.station_id, c.visit_index))


def plan_route_fuel(route_distance_mi, visits):
    """Plan fuel stops for a geo.Route distance (float miles) and its StationVisit list."""
    distance = to_miles(route_distance_mi)
    return plan_fuel(distance, candidates_from_visits(visits, distance))


def plan_fuel(distance_mi, candidates):
    """Plan fuel stops. ``candidates`` must be sorted by sort_candidates() and lie within [0, distance_mi]."""
    if not isinstance(distance_mi, Decimal):
        raise TypeError('distance_mi must be a Decimal; convert route floats with to_miles()')
    distance = distance_mi
    if distance <= 0:
        raise ValueError('route distance must be positive')
    for c in candidates:
        if not (0 <= c.mile <= distance) or c.price <= 0:
            raise ValueError(f'invalid candidate {c}')
    if list(candidates) != sort_candidates(candidates):
        raise ValueError('candidates must be sorted by (mile, price, station_id, visit_index)')

    def gallons(miles):
        return miles / MILES_PER_GALLON

    def rank(c):
        return (c.price, c.mile, c.station_id, c.visit_index)

    def infeasible(start, after_index):
        later = [c.mile for c in candidates[after_index:] if c.mile > start + MAX_RANGE_MILES]
        end = later[0] if later else distance
        return _plan(distance, False, [], gap=(start, end),
                     reason=f'no fuel station within {MAX_RANGE_MILES} miles after mile {start}')

    stops = []
    fuel = TANK_GALLONS
    if distance <= MAX_RANGE_MILES:  # the free starting tank covers the whole route
        return _plan(distance, True, stops)
    initial = [i for i, c in enumerate(candidates) if c.mile <= MAX_RANGE_MILES]
    if not initial:
        return infeasible(Decimal('0'), 0)
    i = min(initial, key=lambda k: rank(candidates[k]))
    fuel -= gallons(candidates[i].mile)

    while True:
        here = candidates[i]
        # Defensive: with the rules below, arrival fuel never reaches past the destination (the previous
        # decision would already have finished), so this check is not expected to fire.
        if gallons(distance - here.mile) <= fuel:
            return _plan(distance, True, stops)
        window = [k for k in range(i + 1, len(candidates)) if candidates[k].mile <= here.mile + MAX_RANGE_MILES]
        cheaper = next((k for k in window if candidates[k].price < here.price), None)
        if cheaper is not None:
            target, buy = cheaper, max(Decimal('0'), gallons(candidates[cheaper].mile - here.mile) - fuel)
        elif distance - here.mile <= MAX_RANGE_MILES:
            buy = gallons(distance - here.mile) - fuel
            stops.append(_stop(here, fuel, buy))
            return _plan(distance, True, stops)
        elif not window:
            return infeasible(here.mile, i + 1)
        else:
            target, buy = min(window, key=lambda k: rank(candidates[k])), TANK_GALLONS - fuel
        if buy > 0:
            stops.append(_stop(here, fuel, buy))
        fuel = fuel + buy - gallons(candidates[target].mile - here.mile)
        if fuel < 0:
            raise AssertionError(f'negative fuel {fuel} on arrival at {candidates[target]}')
        i = target


def _stop(candidate, arrival_fuel, buy):
    if not (0 <= arrival_fuel and 0 < buy and arrival_fuel + buy <= TANK_GALLONS):
        raise AssertionError(f'invalid purchase {buy} with {arrival_fuel} gal on board at {candidate}')
    return FuelStop(
        candidate.station_id, candidate.visit_index, candidate.mile, arrival_fuel, buy,
        candidate.price, buy * candidate.price, candidate.station, candidate.offset_mi,
    )


def _plan(distance, feasible, stops, gap=(None, None), reason=''):
    consumed = distance / MILES_PER_GALLON
    purchased = sum((s.gallons for s in stops), Decimal('0'))
    cost = sum((s.cost for s in stops), Decimal('0'))
    ending = TANK_GALLONS + purchased - consumed if feasible else None
    if feasible and not (0 <= ending <= TANK_GALLONS):
        raise AssertionError(f'ending fuel {ending} out of bounds')
    return FuelPlan(
        feasible, distance, tuple(stops),
        consumed, purchased, ending, cost, cost.quantize(CENT, rounding=ROUND_HALF_UP),
        gap_start_mi=gap[0], gap_end_mi=gap[1], reason=reason,
    )
