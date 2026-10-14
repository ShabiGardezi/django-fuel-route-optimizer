import tempfile
from pathlib import Path
from unittest import mock

import requests
from django.conf import settings
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from routing import ors

API_KEY = 'test-key-not-real-123'


class FakeResponse:
    def __init__(self, status=200, body=None, raw=None):
        self.status_code, self._body, self._raw = status, body, raw

    def json(self):
        if self._raw is not None:
            raise ValueError('not JSON')
        return self._body


def place(lon, lat, label='Place', country='USA', layer='locality', confidence=1.0):
    return {
        'type': 'Feature',
        'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
        'properties': {'label': label, 'country_a': country, 'layer': layer, 'confidence': confidence},
    }


def geocode_body(*features):
    return {'type': 'FeatureCollection', 'features': list(features)}


def route_body(coords=None, distance=160934.4):
    coords = coords or [[-97.74, 30.27], [-97.5, 30.6], [-97.33, 32.75]]
    return {'type': 'FeatureCollection', 'features': [{
        'type': 'Feature', 'geometry': {'type': 'LineString', 'coordinates': coords},
        'properties': {'summary': {'distance': distance, 'duration': 3600}},
    }]}


AUSTIN = {'lon': -97.74, 'lat': 30.27, 'label': 'Austin, TX, USA'}
DALLAS = {'lon': -96.80, 'lat': 32.78, 'label': 'Dallas, TX, USA'}


@override_settings(
    ORS_API_KEY=API_KEY,
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'ors-tests'}},
)
class ORSTestCase(SimpleTestCase):
    def setUp(self):
        cache.clear()
        # Belt and braces: any request that escapes the mock below fails instead of reaching the network.
        guard = mock.patch('requests.adapters.HTTPAdapter.send', side_effect=AssertionError('live HTTP attempted'))
        guard.start()
        self.addCleanup(guard.stop)
        patcher = mock.patch('routing.ors.requests.request')
        self.http = patcher.start()
        self.addCleanup(patcher.stop)

    def respond(self, *responses):
        self.http.side_effect = list(responses)

    def assert_no_key_in(self, exc):
        self.assertNotIn(API_KEY, str(exc))
        self.assertNotIn(API_KEY, repr(exc.args))


class GeocodeTests(ORSTestCase):
    def test_geocodes_both_endpoints(self):
        self.respond(
            FakeResponse(body=geocode_body(place(-97.7431, 30.2672, 'Austin, TX, USA'))),
            FakeResponse(body=geocode_body(place(-96.797, 32.7767, 'Dallas, TX, USA'))),
        )
        start = ors.geocode('  Austin,   TX ')
        finish = ors.geocode('Dallas, TX')
        self.assertEqual(start, {'lon': -97.7431, 'lat': 30.2672, 'label': 'Austin, TX, USA'})
        self.assertEqual((finish['lon'], finish['lat']), (-96.797, 32.7767))
        method, url = self.http.call_args_list[0].args
        kwargs = self.http.call_args_list[0].kwargs
        self.assertEqual((method, url), ('GET', 'https://api.heigit.org/pelias/v1/search'))
        self.assertEqual(kwargs['params'], {'text': 'Austin, TX', 'boundary.country': 'USA', 'size': 2})
        self.assertEqual(kwargs['headers']['Authorization'], API_KEY)
        self.assertNotIn(API_KEY, url)
        self.assertNotIn(API_KEY, str(kwargs['params']))
        self.assertEqual(kwargs['timeout'], ors.TIMEOUT)

    def test_non_us_result_is_rejected(self):
        self.respond(FakeResponse(body=geocode_body(place(-79.38, 43.65, 'Toronto, ON, Canada', country='CAN'))))
        with self.assertRaises(ors.InvalidLocation) as ctx:
            ors.geocode('Toronto')
        self.assertEqual(ctx.exception.reason, 'outside_usa')

    def test_missing_country_field_is_rejected(self):
        feature = place(-97.74, 30.27)
        del feature['properties']['country_a']
        self.respond(FakeResponse(body=geocode_body(feature)))
        with self.assertRaises(ors.InvalidLocation) as ctx:
            ors.geocode('Austin')
        self.assertEqual(ctx.exception.reason, 'outside_usa')

    def test_ambiguous_place_is_rejected(self):
        self.respond(FakeResponse(body=geocode_body(
            place(-89.65, 39.80, 'Springfield, IL, USA'), place(-93.29, 37.21, 'Springfield, MO, USA'),
        )))
        with self.assertRaises(ors.InvalidLocation) as ctx:
            ors.geocode('Springfield')
        self.assertEqual(ctx.exception.reason, 'ambiguous')
        self.assertIn('add a state', str(ctx.exception))

    def test_nearby_or_weaker_second_result_is_not_ambiguous(self):
        self.respond(
            FakeResponse(body=geocode_body(place(-89.65, 39.80, 'Springfield, IL'), place(-89.60, 39.78, 'Springfield area'))),
            FakeResponse(body=geocode_body(place(-89.65, 39.80, 'Springfield, IL'), place(-93.29, 37.21, 'Other', confidence=0.6))),
        )
        self.assertEqual(ors.geocode('Springfield, IL')['lat'], 39.80)
        self.assertEqual(ors.geocode('Springfield IL')['lat'], 39.80)

    def test_unusable_results(self):
        cases = [
            (geocode_body(), ors.InvalidLocation, 'not_found'),
            (geocode_body(place(-99.0, 31.0, 'Texas, USA', layer='region')), ors.InvalidLocation, 'too_coarse'),
            (geocode_body({'type': 'Feature', 'geometry': {'type': 'Polygon'}, 'properties': {}}), ors.ProviderBadResponse, None),
            (geocode_body(place(-200.0, 30.0)), ors.ProviderBadResponse, None),
            ({'type': 'FeatureCollection'}, ors.ProviderBadResponse, None),
        ]
        for index, (body, error, reason) in enumerate(cases):
            self.respond(FakeResponse(body=body))
            with self.subTest(index=index), self.assertRaises(error) as ctx:
                ors.geocode(f'Place {index}')
            if reason:
                self.assertEqual(ctx.exception.reason, reason)

    def test_invalid_input_makes_no_request(self):
        for text in ('', '   ', None, 'x' * 201):
            with self.subTest(text=text), self.assertRaises(ors.InvalidLocation) as ctx:
                ors.geocode(text)
            self.assertEqual(ctx.exception.reason, 'invalid_input')
        self.http.assert_not_called()


class DirectionsTests(ORSTestCase):
    def test_successful_route(self):
        self.respond(FakeResponse(body=route_body(distance=321868.8)))
        route = ors.directions(AUSTIN, DALLAS)
        self.assertAlmostEqual(route.distance_mi, 200.0, places=9)
        self.assertEqual(len(route.lats), 3)
        method, url = self.http.call_args.args
        self.assertEqual((method, url), ('POST', 'https://api.heigit.org/openrouteservice/v2/directions/driving-car/geojson'))
        self.assertEqual(self.http.call_args.kwargs['json'], {'coordinates': [[-97.74, 30.27], [-96.80, 32.78]]})

    def test_malformed_route_responses(self):
        bad = [
            {'type': 'FeatureCollection', 'features': []},
            {'type': 'FeatureCollection'},
            {'features': [{'geometry': {'type': 'Point', 'coordinates': [-97, 30]}, 'properties': {'summary': {'distance': 5}}}]},
            route_body(coords=[[-97.74, 30.27]]),
            route_body(coords=[[-97.74, 30.27], ['x', 30.6]]),
            route_body(coords=[[-97.74, 30.27], [True, 30.6]]),
            route_body(coords=[[179.9, 50.0], [-179.9, 50.0]]),  # rejected by geo (antimeridian)
            route_body(distance=None),
            route_body(distance=-5),
            route_body(distance=True),
            route_body(distance=float('nan')),
            {'features': [{'geometry': {'type': 'LineString', 'coordinates': [[-97, 30], [-96, 31]]}}]},
        ]
        for index, body in enumerate(bad):
            self.respond(FakeResponse(body=body))
            with self.subTest(index=index), self.assertRaises(ors.ProviderBadResponse):
                ors.directions(AUSTIN, {**DALLAS, 'lon': DALLAS['lon'] + index * 0.001})

    def test_zero_length_and_identical_endpoints_are_not_routable(self):
        self.respond(FakeResponse(body=route_body(distance=0)))
        with self.assertRaises(ors.RouteNotFound):
            ors.directions(AUSTIN, DALLAS)
        with self.assertRaises(ors.RouteNotFound):
            ors.directions(AUSTIN, dict(AUSTIN))
        self.assertEqual(self.http.call_count, 1)

    def test_unroutable_journeys(self):
        for status, code in ((404, 2010), (404, 2009), (400, 2004), (500, 2009)):
            self.respond(FakeResponse(status, {'error': {'code': code, 'message': 'details'}}))
            with self.subTest(code=code), self.assertRaises(ors.RouteNotFound) as ctx:
                ors.directions(AUSTIN, DALLAS)
            self.assertEqual(ctx.exception.code, code)


class ProviderFailureTests(ORSTestCase):
    def assert_raises(self, error, response):
        self.http.side_effect = [response] if not isinstance(response, Exception) else response
        with self.assertRaises(error) as ctx:
            ors.directions(AUSTIN, DALLAS)
        self.assert_no_key_in(ctx.exception)
        return ctx.exception

    def test_missing_api_key(self):
        for key in ('', '   '):
            with self.subTest(key=key), override_settings(ORS_API_KEY=key):
                with self.assertRaises(ors.ConfigurationError):
                    ors.geocode('Austin, TX')
                with self.assertRaises(ors.ConfigurationError):
                    ors.directions(AUSTIN, DALLAS)
        self.http.assert_not_called()

    def test_quota_403_is_rate_limited_not_auth(self):
        for message in ('Quota exceeded', 'Quota Exceeded', ' quota  exceeded. ',
                        'This organisation quota has been exceeded, please contact your API administrator'):
            with self.subTest(message=message):
                error = self.assert_raises(ors.ProviderRateLimited, FakeResponse(403, {'error': message}))
                self.assertTrue(error.quota_exhausted)
                self.assertEqual(error.status, 403)
                self.assertNotIsInstance(error, ors.ProviderAuthError)

    def test_genuine_auth_403_and_401_are_auth_errors(self):
        for status, body in (
            (403, {'error': 'Access to this API has been disallowed'}),
            (403, {'error': 'Key is not active, please renew'}),
            (403, {'error': 'Daily quota reached or API key unauthorized'}),  # legacy text: ambiguous, not assumed quota
            (403, {'error': 'Quota exceeded for something else entirely'}),  # not an exact documented message
            (403, None),
            (401, {'error': 'Authorization Field Missing'}),
            (401, {'error': 'Key has expired, please renew'}),
        ):
            with self.subTest(status=status, body=body):
                error = self.assert_raises(ors.ProviderAuthError, FakeResponse(status, body, raw=None if body else 'x'))
                self.assertEqual(error.status, status)

    def test_429_is_rate_limited(self):
        for body in ({'error': 'API Rate Limit Exceeded'}, {'error': 'Rate limit exceeded'}, None):
            with self.subTest(body=body):
                error = self.assert_raises(ors.ProviderRateLimited, FakeResponse(429, body, raw=None if body else 'x'))
                self.assertEqual(error.status, 429)
                self.assertFalse(error.quota_exhausted)

    def test_http_failures_map_to_typed_errors(self):
        self.assertEqual(self.assert_raises(ors.ProviderAuthError, FakeResponse(401, {'error': 'Access to this API has been disallowed'})).status, 401)
        self.assert_raises(ors.ProviderAuthError, FakeResponse(403, {'error': 'Forbidden'}))
        self.assertEqual(self.assert_raises(ors.ProviderRateLimited, FakeResponse(429, {'error': 'Rate limit exceeded'})).status, 429)
        for status in (500, 502, 503, 504):
            self.assert_raises(ors.ProviderUnavailable, FakeResponse(status, None, raw='<html>oops</html>'))
        invalid = self.assert_raises(ors.ProviderBadResponse, FakeResponse(400, {'error': {'code': 2003, 'message': 'bad value'}}))
        self.assertEqual(invalid.code, 2003)

    def test_network_failures(self):
        self.assert_raises(ors.ProviderTimeout, requests.ConnectTimeout('connect timed out'))
        self.assert_raises(ors.ProviderTimeout, requests.ReadTimeout('read timed out'))
        self.assertNotIsInstance(self.assert_raises(ors.ProviderUnavailable, requests.ConnectionError('refused')), ors.ProviderTimeout)
        self.assertNotIsInstance(self.assert_raises(ors.ProviderUnavailable, requests.TooManyRedirects('loop')), ors.ProviderTimeout)
        self.assertTrue(issubclass(ors.ProviderTimeout, ors.ProviderUnavailable))

    def test_malformed_json_and_unexpected_schema(self):
        self.assert_raises(ors.ProviderBadResponse, FakeResponse(200, None, raw='not json'))
        self.assert_raises(ors.ProviderBadResponse, FakeResponse(200, ['a', 'list']))
        self.assert_raises(ors.ProviderBadResponse, FakeResponse(200, {'unexpected': True}))

    def test_all_errors_share_a_base_class(self):
        for error in (ors.ConfigurationError, ors.InvalidLocation, ors.RouteNotFound, ors.ProviderAuthError,
                      ors.ProviderRateLimited, ors.ProviderUnavailable, ors.ProviderBadResponse):
            self.assertTrue(issubclass(error, ors.ORSError))
        for error in (ors.ProviderAuthError, ors.ProviderRateLimited, ors.ProviderUnavailable, ors.ProviderBadResponse):
            self.assertTrue(issubclass(error, ors.ProviderError))


class CachingTests(ORSTestCase):
    def test_successful_geocode_and_route_are_cached(self):
        self.respond(
            FakeResponse(body=geocode_body(place(-97.74, 30.27, 'Austin, TX, USA'))),
            FakeResponse(body=route_body()),
        )
        first = ors.geocode('Austin, TX')
        self.assertEqual(ors.geocode('  Austin,  TX  '), first)  # same request after whitespace normalization
        route = ors.directions(AUSTIN, DALLAS)
        self.assertEqual(ors.directions(AUSTIN, DALLAS), route)
        self.assertEqual(self.http.call_count, 2)

    def test_failures_are_not_cached(self):
        self.respond(
            requests.ReadTimeout('slow'),
            FakeResponse(500, None, raw='err'),
            FakeResponse(200, {'unexpected': True}),
            FakeResponse(body=geocode_body(place(-79.38, 43.65, country='CAN'))),
            FakeResponse(body=geocode_body(place(-97.74, 30.27, 'Austin, TX, USA'))),
        )
        for error in (ors.ProviderUnavailable, ors.ProviderUnavailable, ors.ProviderBadResponse, ors.InvalidLocation):
            with self.assertRaises(error):
                ors.geocode('Austin, TX')
        self.assertEqual(ors.geocode('Austin, TX')['label'], 'Austin, TX, USA')
        self.assertEqual(self.http.call_count, 5)

        self.respond(FakeResponse(body=route_body(distance=None)), FakeResponse(body=route_body()))
        with self.assertRaises(ors.ProviderBadResponse):
            ors.directions(AUSTIN, DALLAS)
        ors.directions(AUSTIN, DALLAS)
        self.assertEqual(self.http.call_count, 7)

    def test_cache_keys_depend_on_request_parameters(self):
        url = ors.GEOCODE_URL
        geocode_key = ors._cache_key('geocode', {'url': url, 'text': 'Austin, TX', 'boundary.country': 'USA', 'size': 2})
        self.assertEqual(geocode_key, ors._cache_key('geocode', {'size': 2, 'boundary.country': 'USA', 'text': 'Austin, TX', 'url': url}))
        variants = [
            ors._cache_key('geocode', {'url': url, 'text': 'Austin, MN', 'boundary.country': 'USA', 'size': 2}),
            ors._cache_key('geocode', {'url': url, 'text': 'Austin, TX', 'boundary.country': 'CAN', 'size': 2}),
            ors._cache_key('geocode', {'url': url, 'text': 'Austin, TX', 'boundary.country': 'USA', 'size': 1}),
            ors._cache_key('directions', {'url': url, 'text': 'Austin, TX', 'boundary.country': 'USA', 'size': 2}),
            ors._cache_key('geocode', {'url': 'https://api.openrouteservice.org/geocode/search', 'text': 'Austin, TX',
                                       'boundary.country': 'USA', 'size': 2}),  # a different host is a different request
        ]
        self.assertEqual(len({geocode_key, *variants}), 6)
        self.assertTrue(geocode_key.startswith(f'ors:{ors.CACHE_VERSION}:geocode:'))
        self.assertNotIn(API_KEY, geocode_key)

        self.respond(FakeResponse(body=route_body()), FakeResponse(body=route_body()), FakeResponse(body=route_body()))
        ors.directions(AUSTIN, DALLAS)
        ors.directions(DALLAS, AUSTIN)  # reversed direction is a different request
        ors.directions(AUSTIN, {**DALLAS, 'lat': DALLAS['lat'] + 1e-6})
        ors.directions(AUSTIN, DALLAS)  # cached
        self.assertEqual(self.http.call_count, 3)


class FileBasedCacheTests(ORSTestCase):
    """The same caching rules with Django's FileBasedCache, as configured in settings."""

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cache_dir = Path(directory.name)
        file_cache = override_settings(CACHES={'default': {
            'BACKEND': 'django.core.cache.backends.filebased.FileBasedCache', 'LOCATION': str(self.cache_dir),
        }})
        file_cache.enable()
        self.addCleanup(file_cache.disable)

    def cache_files(self):
        return sorted(self.cache_dir.glob('*.djcache'))

    def test_successes_are_written_to_disk_and_reused(self):
        self.respond(FakeResponse(body=geocode_body(place(-97.74, 30.27, 'Austin, TX, USA'))), FakeResponse(body=route_body()))
        ors.geocode('Austin, TX')
        ors.directions(AUSTIN, DALLAS)
        self.assertEqual(len(self.cache_files()), 2)
        ors.geocode('Austin, TX')
        ors.directions(AUSTIN, DALLAS)
        self.assertEqual(self.http.call_count, 2)
        for path in self.cache_files():
            self.assertNotIn(API_KEY.encode(), path.read_bytes())

    def test_failures_are_never_written(self):
        self.respond(
            FakeResponse(403, {'error': 'Quota exceeded'}),
            FakeResponse(401, {'error': 'Authorization Field Missing'}),
            FakeResponse(429, {'error': 'Rate limit exceeded'}),
            requests.ReadTimeout('slow'),
            FakeResponse(200, None, raw='not json'),
            FakeResponse(body=geocode_body(place(-79.38, 43.65, country='CAN'))),
            FakeResponse(body=route_body(distance=None)),
            FakeResponse(404, {'error': {'code': 2010, 'message': 'Point was not found'}}),
        )
        for _ in range(6):
            with self.assertRaises(ors.ORSError):
                ors.geocode('Austin, TX')
        for _ in range(2):
            with self.assertRaises(ors.ORSError):
                ors.directions(AUSTIN, DALLAS)
        self.assertEqual(self.cache_files(), [])
        self.assertEqual(self.http.call_count, 8)


class CacheSettingsTests(SimpleTestCase):
    def test_project_uses_file_based_cache_under_base_dir(self):
        config = settings.CACHES['default']
        self.assertEqual(config['BACKEND'], 'django.core.cache.backends.filebased.FileBasedCache')
        self.assertTrue(Path(config['LOCATION']).is_relative_to(Path(settings.BASE_DIR) / 'cache'))
        self.assertEqual(config['TIMEOUT'], 7 * 24 * 3600)
        self.assertEqual(ors.CACHE_SECONDS, 7 * 24 * 3600)
