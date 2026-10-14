import json
import math
from decimal import ROUND_HALF_UP, Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from routing import geo, ors
from routing.models import FuelPriceRecord, FuelStation

API_KEY = 'test-key-not-real-456'
URL = '/api/route/'
LAT = 39.0
START_LON = -100.0
MILES_PER_DEG_LON = geo.EARTH_RADIUS_MI * math.radians(1) * math.cos(math.radians(LAT))


def lon_at(mile):
    return START_LON + mile / MILES_PER_DEG_LON


def route_coords(miles):
    steps = int(miles // 5)
    return [[lon_at(k * miles / steps), LAT] for k in range(steps + 1)]


def geometry_metres(coords):
    return math.fsum(geo.haversine_mi(a[1], a[0], b[1], b[0]) for a, b in zip(coords, coords[1:])) * 1609.344


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def place(lon, lat, label, country='USA', confidence=1.0):
    return {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
            'properties': {'label': label, 'country_a': country, 'layer': 'locality', 'confidence': confidence}}


class FakeORS:
    """Answers requests the way the documented ORS endpoints do, for a straight east-west test route."""

    def __init__(self, route_miles=1000.0):
        self.calls = []
        self.coords = route_coords(route_miles)
        self.places = {
            'Westville, KS': place(self.coords[0][0], LAT, 'Westville, KS, USA'),
            'Eastville, OH': place(self.coords[-1][0], LAT, 'Eastville, OH, USA'),
            'Midville, IN': place(lon_at(450), LAT, 'Midville, IN, USA'),  # within the 500-mile tank
        }
        self.overrides = {}

    def __call__(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append((method, url))
        assert headers['Authorization'] == API_KEY and timeout == ors.TIMEOUT
        if url == ors.GEOCODE_URL:
            if params['text'] in self.overrides:
                return self.overrides[params['text']]
            feature = self.places.get(params['text'])
            return FakeResponse(body={'type': 'FeatureCollection', 'features': [feature] if feature else []})
        assert url == ors.DIRECTIONS_URL
        if 'directions' in self.overrides:
            return self.overrides['directions']
        start, end = json['coordinates']
        coords = [c for c in self.coords if min(start[0], end[0]) - 1e-9 <= c[0] <= max(start[0], end[0]) + 1e-9]
        if start[0] > end[0]:
            coords = coords[::-1]
        return FakeResponse(body={'type': 'FeatureCollection', 'features': [{
            'type': 'Feature', 'geometry': {'type': 'LineString', 'coordinates': coords},
            'properties': {'summary': {'distance': geometry_metres(coords), 'duration': 36000}},
        }]})


@override_settings(
    ORS_API_KEY=API_KEY, FUEL_CORRIDOR_MILES=10.0,
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'api-tests'}},
)
class APITestCase(TestCase):
    def setUp(self):
        cache.clear()
        guard = mock.patch('requests.adapters.HTTPAdapter.send', side_effect=AssertionError('live HTTP attempted'))
        guard.start()
        self.addCleanup(guard.stop)
        self.fake = FakeORS()
        patcher = mock.patch('routing.ors.requests.request', side_effect=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    @classmethod
    def setUpTestData(cls):
        def station(opis_id, mile, price, line, lat=LAT, extra_prices=()):
            s = FuelStation.objects.create(
                opis_id=opis_id, name=f'STOP {opis_id}', address=f'I-70, EXIT {opis_id}', city=f'Town {opis_id}',
                state='KS', rack_id=1, latitude=lat, longitude=None if lat is None else lon_at(mile),
                lowest_price=Decimal(price), lowest_price_source_line=line,
            )
            FuelPriceRecord.objects.create(station=s, source_line=line, listed_name=s.name, retail_price=Decimal(price))
            for offset, other in enumerate(extra_prices, start=1):
                FuelPriceRecord.objects.create(station=s, source_line=line + offset, listed_name=s.name, retail_price=Decimal(other))
            return s

        station(101, 100, '3.50000000', 10)
        station(102, 300, '3.00733333', 20, extra_prices=['3.20000000'])
        station(103, 700, '3.19900000', 30)
        station(104, 500, '2.00000000', 40, lat=LAT + 3)  # cheap but ~200 miles off the route
        station(105, 400, '1.00000000', 50, lat=None)  # never located

    def post(self, body, raw=False, **kwargs):
        data = body if raw else json.dumps(body)
        response = self.client.post(URL, data=data, content_type=kwargs.pop('content_type', 'application/json'), **kwargs)
        return response, json.loads(response.content, parse_float=Decimal)

    def assert_error(self, response, payload, http_status, code):
        self.assertEqual(response.status_code, http_status, payload)
        self.assertEqual(set(payload), {'error'})
        self.assertEqual(set(payload['error']), {'code', 'message', 'details'})
        self.assertEqual(payload['error']['code'], code)
        text = response.content.decode()
        self.assertNotIn(API_KEY, text)
        self.assertNotIn('Traceback', text)
        return payload['error']


class SuccessTests(APITestCase):
    def test_successful_plan_schema_and_values(self):
        response, body = self.post({'start': 'Westville, KS', 'finish': 'Eastville, OH'})
        self.assertEqual(response.status_code, 200, body)
        self.assertEqual(set(body), {'start', 'finish', 'route', 'fuel_stops', 'fuel_summary',
                                     'candidate_station_visits', 'corridor_miles', 'assumptions'})
        self.assertEqual(body['start']['label'], 'Westville, KS, USA')
        self.assertEqual(body['finish']['query'], 'Eastville, OH')
        self.assertEqual(body['route']['geometry']['type'], 'LineString')
        self.assertEqual(body['route']['geometry']['coordinates'][0], self.fake.coords[0])
        self.assertAlmostEqual(Decimal(body['route']['distance_miles']), Decimal('1000'), delta=Decimal('0.01'))
        self.assertEqual(body['candidate_station_visits'], 3)  # off-route and unlocated stations excluded

        # Origin goes to the cheapest station in range (102), fills, then tops up at 103 to finish.
        stops = body['fuel_stops']
        self.assertEqual([s['station']['opis_id'] for s in stops], [102, 103])
        self.assertEqual([s['sequence'] for s in stops], [1, 2])
        self.assertLess(Decimal(stops[0]['route_mile']), Decimal(stops[1]['route_mile']))
        first = stops[0]
        self.assertEqual(set(first), {
            'sequence', 'station', 'visit_index', 'route_mile', 'offset_from_route_miles', 'arrival_fuel_gallons',
            'gallons_purchased', 'price_per_gallon', 'price_source_csv_line', 'listed_price_max',
            'listed_price_count', 'stop_cost_rounded', 'stop_cost_unrounded',
        })
        self.assertEqual(first['station']['city'], 'Town 102')
        self.assertEqual(first['price_per_gallon'], '3.00733333')
        self.assertEqual(first['price_source_csv_line'], 20)
        self.assertEqual((first['listed_price_max'], first['listed_price_count']), ('3.20000000', 2))
        self.assertEqual(Decimal(first['arrival_fuel_gallons']) + Decimal(first['gallons_purchased']), Decimal('50'))

        summary = body['fuel_summary']
        self.assertEqual(summary['starting_fuel_gallons'], '50')
        self.assertEqual(Decimal(summary['fuel_consumed_gallons']), Decimal(body['route']['distance_miles']) / 10)
        self.assertEqual(Decimal(summary['ending_fuel_gallons']), Decimal('0'))
        self.assertEqual(len(body['assumptions']), 6)

    def test_costs_are_decimal_strings_and_rounded_once(self):
        response, body = self.post({'start': 'Westville, KS', 'finish': 'Eastville, OH'})
        self.assertEqual(response.status_code, 200)
        unrounded = Decimal('0')
        for stop in body['fuel_stops']:
            for field in ('route_mile', 'arrival_fuel_gallons', 'gallons_purchased', 'price_per_gallon',
                          'stop_cost_rounded', 'stop_cost_unrounded'):
                self.assertIsInstance(stop[field], str, field)
                self.assertNotIn('E', stop[field])
            cost = Decimal(stop['gallons_purchased']) * Decimal(stop['price_per_gallon'])
            self.assertEqual(Decimal(stop['stop_cost_unrounded']), cost)
            self.assertEqual(Decimal(stop['stop_cost_rounded']), cost.quantize(Decimal('0.01'), ROUND_HALF_UP))
            unrounded += cost
        summary = body['fuel_summary']
        for field in ('fuel_consumed_gallons', 'fuel_purchased_gallons', 'ending_fuel_gallons', 'total_cost_usd',
                      'total_cost_unrounded_usd'):
            self.assertIsInstance(summary[field], str, field)
        self.assertEqual(Decimal(summary['total_cost_unrounded_usd']), unrounded)
        self.assertEqual(Decimal(summary['total_cost_usd']), unrounded.quantize(Decimal('0.01'), ROUND_HALF_UP))
        self.assertEqual(Decimal(summary['fuel_purchased_gallons']),
                         sum(Decimal(s['gallons_purchased']) for s in body['fuel_stops']))

    def test_short_trip_needs_no_stops(self):
        response, body = self.post({'start': 'Westville, KS', 'finish': 'Midville, IN'})
        self.assertEqual(response.status_code, 200, body)
        self.assertEqual(body['fuel_stops'], [])
        self.assertEqual(body['fuel_summary']['total_cost_usd'], '0.00')

    def test_repeated_requests_reuse_cache(self):
        self.post({'start': 'Westville, KS', 'finish': 'Eastville, OH'})
        self.assertEqual(len(self.fake.calls), 3)  # two geocodes + one route
        response, body = self.post({'start': '  Westville,  KS ', 'finish': 'Eastville, OH'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.fake.calls), 3)  # everything from cache
        self.post({'start': 'Westville, KS', 'finish': 'Midville, IN'})
        self.assertEqual(len(self.fake.calls), 5)  # one new geocode + one new route


class InputValidationTests(APITestCase):
    def test_missing_blank_and_wrong_type_fields(self):
        cases = [
            ({}, {'start', 'finish'}),
            ({'start': 'Westville, KS'}, {'finish'}),
            ({'start': '', 'finish': 'Eastville, OH'}, {'start'}),
            ({'start': '   ', 'finish': 'Eastville, OH'}, {'start'}),
            ({'start': None, 'finish': 'Eastville, OH'}, {'start'}),
            ({'start': 12345, 'finish': 'Eastville, OH'}, {'start'}),
            ({'start': ['Westville'], 'finish': {'city': 'x'}}, {'start', 'finish'}),
            ({'start': 'x' * 201, 'finish': 'Eastville, OH'}, {'start'}),
            ({'start': '40.7128,-74.0060', 'finish': 'Eastville, OH'}, {'start'}),
            ({'start': 'Westville, KS', 'finish': '42.36 -71.06'}, {'finish'}),
        ]
        for body, fields in cases:
            with self.subTest(body=body):
                response, payload = self.post(body)
                error = self.assert_error(response, payload, 400, 'invalid_request')
                self.assertEqual(set(error['details']['fields']), fields)
        self.assertEqual(self.fake.calls, [])

    def test_malformed_bodies_and_methods(self):
        response, payload = self.post('{"start": "Westville', raw=True)
        self.assert_error(response, payload, 400, 'invalid_request')
        response, payload = self.post(['Westville, KS', 'Eastville, OH'])
        self.assert_error(response, payload, 400, 'invalid_request')
        response, payload = self.post('start=Westville', raw=True, content_type='application/x-www-form-urlencoded')
        self.assert_error(response, payload, 415, 'unsupported_media_type')
        response = self.client.get(URL)
        self.assert_error(response, json.loads(response.content), 405, 'method_not_allowed')
        self.assertEqual(self.fake.calls, [])


class LocationAndRouteErrorTests(APITestCase):
    def test_location_failures(self):
        springfields = FakeResponse(body={'type': 'FeatureCollection', 'features': [
            place(-89.65, 39.80, 'Springfield, IL, USA'), place(-93.29, 37.21, 'Springfield, MO, USA')]})
        self.fake.overrides = {
            'Springfield': springfields,
            'Toronto': FakeResponse(body={'type': 'FeatureCollection', 'features': [place(-79.38, 43.65, 'Toronto, ON', country='CAN')]}),
        }
        for start, finish, field, reason in (
            ('Nowhereville, ZZ', 'Eastville, OH', 'start', 'not_found'),
            ('Westville, KS', 'Springfield', 'finish', 'ambiguous'),
            ('Toronto', 'Eastville, OH', 'start', 'outside_usa'),
        ):
            with self.subTest(start=start, finish=finish):
                response, payload = self.post({'start': start, 'finish': finish})
                error = self.assert_error(response, payload, 400, 'invalid_location')
                self.assertEqual(error['details'], {'field': field, 'reason': reason})

    def test_same_location(self):
        response, payload = self.post({'start': 'Westville, KS', 'finish': 'Westville,  KS'})
        self.assert_error(response, payload, 400, 'same_location')
        self.assertEqual(len(self.fake.calls), 1)  # second geocode came from the cache; no route requested

    def test_unroutable_pair_and_provider_route_limit_are_distinct(self):
        self.fake.overrides = {'directions': FakeResponse(404, {'error': {'code': 2009, 'message': 'Route could not be found'}})}
        response, payload = self.post({'start': 'Westville, KS', 'finish': 'Eastville, OH'})
        self.assertEqual(self.assert_error(response, payload, 422, 'route_not_found')['details'], {'provider_code': 2009})
        cache.clear()
        self.fake.overrides = {'directions': FakeResponse(400, {'error': {'code': 2004, 'message': 'limit exceeded'}})}
        response, payload = self.post({'start': 'Westville, KS', 'finish': 'Eastville, OH'})
        error = self.assert_error(response, payload, 422, 'route_exceeds_provider_limits')
        self.assertEqual(error['details'], {'provider_code': 2004})
        self.assertNotIn('feasible', error['message'])

    def test_infeasible_fuel_plan(self):
        FuelStation.objects.filter(opis_id__in=[102, 103]).delete()  # only the mile-100 station remains
        response, payload = self.post({'start': 'Westville, KS', 'finish': 'Eastville, OH'})
        error = self.assert_error(response, payload, 422, 'no_feasible_fuel_plan')
        details = error['details']
        self.assertEqual((details['gap_start_mile'], details['max_range_miles']), ('100.000', '500'))
        self.assertEqual(details['gap_end_mile'], details['route_distance_miles'])
        self.assertEqual(details['candidate_station_visits'], 1)


class ProviderErrorTests(APITestCase):
    def test_provider_failures_map_to_statuses(self):
        secret_detail = 'internal provider detail xyz'
        cases = [
            (ors.ProviderAuthError(secret_detail, 403), 502, 'provider_auth_error'),
            (ors.ProviderBadResponse(secret_detail), 502, 'provider_bad_response'),
            (ors.ProviderRateLimited(secret_detail, 429), 503, 'provider_rate_limited'),
            (ors.ProviderRateLimited(secret_detail, 403, quota_exhausted=True), 503, 'provider_rate_limited'),
            (ors.ProviderTimeout(secret_detail), 504, 'provider_timeout'),
            (ors.ProviderUnavailable(secret_detail, 503), 503, 'provider_unavailable'),
            (ors.ProviderError(secret_detail), 502, 'provider_error'),
        ]
        for exc, http_status, code in cases:
            with self.subTest(code=code, exc=type(exc).__name__), mock.patch('routing.ors.directions', side_effect=exc), \
                    self.assertLogs('routing.views', level='WARNING') as logs:
                response, payload = self.post({'start': 'Westville, KS', 'finish': 'Eastville, OH'})
            self.assertIn(secret_detail, ''.join(logs.output))  # kept for operators, not returned to clients
            error = self.assert_error(response, payload, http_status, code)
            self.assertNotIn(secret_detail, response.content.decode())
            if code == 'provider_rate_limited':
                self.assertEqual(error['details']['quota_exhausted'], exc.quota_exhausted)

    def test_provider_timeout_through_real_client(self):
        import requests
        self.fake.overrides = {'directions': None}
        with mock.patch('routing.ors.requests.request', side_effect=[
            self.fake('GET', ors.GEOCODE_URL, {'text': 'Westville, KS'}, None, {'Authorization': API_KEY}, ors.TIMEOUT),
            self.fake('GET', ors.GEOCODE_URL, {'text': 'Eastville, OH'}, None, {'Authorization': API_KEY}, ors.TIMEOUT),
            requests.ReadTimeout('slow'),
        ]):
            with self.assertLogs('routing.views', level='WARNING'):
                response, payload = self.post({'start': 'Westville, KS', 'finish': 'Eastville, OH'})
        self.assert_error(response, payload, 504, 'provider_timeout')

    def test_missing_configuration(self):
        with override_settings(ORS_API_KEY=''), self.assertLogs('routing.views', level='WARNING'):
            response, payload = self.post({'start': 'Westville, KS', 'finish': 'Eastville, OH'})
        error = self.assert_error(response, payload, 500, 'server_misconfigured')
        self.assertNotIn('ORS_API_KEY', error['message'])
        self.assertEqual(self.fake.calls, [])

    def test_unexpected_error_is_a_generic_500(self):
        with mock.patch('routing.geo.find_station_visits', side_effect=RuntimeError('boom at /secret/path')), \
                self.assertLogs('routing.views', level='ERROR'):
            response, payload = self.post({'start': 'Westville, KS', 'finish': 'Eastville, OH'})
        self.assert_error(response, payload, 500, 'internal_error')
        self.assertNotIn('boom', response.content.decode())
