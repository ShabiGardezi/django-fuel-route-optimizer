"""OpenRouteService client: geocode US trip endpoints and fetch one driving route.

Endpoints on api.heigit.org, which replaces the deprecated api.openrouteservice.org (old host shut down
November 2026). The API key is sent in the Authorization header, never in URLs:
* GET  https://api.heigit.org/pelias/v1/search  ?text=...&boundary.country=USA&size=2
       -> Pelias GeoJSON FeatureCollection
* POST https://api.heigit.org/openrouteservice/v2/directions/driving-car/geojson
       {"coordinates": [[lon, lat], [lon, lat]]}
       -> GeoJSON FeatureCollection; features[0].geometry is a LineString and
          features[0].properties.summary.distance is in metres (the default unit).

HTTP 429 is a rate limit. A 403 counts as quota exhaustion only when its JSON "error" field is exactly one
of the quota messages documented by the Tyk API gateway that fronts the service; any other 401/403 is an
authentication/authorisation failure (this includes the legacy ambiguous "Daily quota reached or API key
unauthorized", which cannot be attributed to quota).

Successful, validated results are cached with Django's cache. Errors are never cached.
Each request is made at most once (no retries); failures raise the exceptions below.
"""

import hashlib
import json
import math

import requests
from django.conf import settings
from django.core.cache import cache

from routing import geo

GEOCODE_URL = 'https://api.heigit.org/pelias/v1/search'
DIRECTIONS_URL = 'https://api.heigit.org/openrouteservice/v2/directions/driving-car/geojson'
# Tyk gateway quota messages (https://tyk.io/docs/5.6/error-response-codes/), compared case-insensitively.
QUOTA_MESSAGES = frozenset({
    'quota exceeded',
    'this organisation quota has been exceeded, please contact your api administrator',
})
TIMEOUT = (5, 30)  # seconds: (connect, read)
CACHE_SECONDS = 7 * 24 * 3600
CACHE_VERSION = 'v1'
METRES_PER_MILE = 1609.344
GEOCODE_COUNTRY = 'USA'  # ISO-3166 alpha-3, matched against the documented Pelias "country_a" property
GEOCODE_SIZE = 2  # the second result is used only to detect ambiguous place names
AMBIGUITY_MILES = 25
MAX_QUERY_LENGTH = 200
# Pelias layers too coarse to be a trip endpoint (a whole state or county is not a place to start from).
COARSE_LAYERS = frozenset({
    'continent', 'empire', 'country', 'dependency', 'macroregion', 'region', 'macrocounty', 'county',
    'ocean', 'marinearea',
})
# ORS routing error codes that mean "this journey cannot be routed" rather than a provider fault.
UNROUTABLE_CODES = {2004: 'exceeds the provider route limits', 2009: 'no route between the locations',
                    2010: 'a location is not near a routable road', 2016: 'no route between the locations',
                    2017: 'exceeds the provider route limits'}


class ORSError(Exception):
    """Base class for every error raised by this module."""


class ConfigurationError(ORSError):
    """ORS_API_KEY is not configured."""


class InvalidLocation(ORSError):
    """A start/finish place name is empty, not found, outside the USA, too coarse, or ambiguous."""

    def __init__(self, query, reason, message):
        super().__init__(message)
        self.query, self.reason = query, reason  # reason: invalid_input | not_found | outside_usa | too_coarse | ambiguous


class RouteNotFound(ORSError):
    """Both endpoints are valid but ORS cannot route a drivable journey between them."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class ProviderError(ORSError):
    """ORS failed or returned something unusable; not the user's fault."""

    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status, self.code = status, code


class ProviderAuthError(ProviderError):
    """The API key was rejected (HTTP 401, or a 403 that is not a documented quota message)."""


class ProviderRateLimited(ProviderError):
    """Rate limit (HTTP 429) or daily quota exhausted (HTTP 403 with a documented quota message).

    ``quota_exhausted`` is True for the quota case.
    """

    def __init__(self, message, status=None, code=None, quota_exhausted=False):
        super().__init__(message, status, code)
        self.quota_exhausted = quota_exhausted


class ProviderUnavailable(ProviderError):
    """Timeout, connection failure or HTTP 5xx."""


class ProviderTimeout(ProviderUnavailable):
    """The request timed out (connect or read)."""


class ProviderBadResponse(ProviderError):
    """Malformed JSON, unexpected schema, or a request ORS rejected as invalid."""


def _api_key():
    key = getattr(settings, 'ORS_API_KEY', '') or ''
    if not key.strip():
        raise ConfigurationError('ORS_API_KEY is not set; add it to the environment or .env')
    return key.strip()


def _cache_key(kind, payload):
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return f'ors:{CACHE_VERSION}:{kind}:{digest}'


def _error_details(body):
    """(code, message) from an ORS/Pelias error body, if present."""
    if isinstance(body, dict):
        error = body.get('error')
        if isinstance(error, dict):
            return error.get('code'), str(error.get('message', ''))[:300]
        if isinstance(error, str):
            return None, error[:300]
        geocoding = body.get('geocoding')
        if isinstance(geocoding, dict) and geocoding.get('errors'):
            return None, '; '.join(map(str, geocoding['errors']))[:300]
    return None, ''


def _is_quota_message(message):
    return ' '.join(message.split()).rstrip('.').casefold() in QUOTA_MESSAGES


def _request(method, url, *, params=None, payload=None):
    """Send one request and return the decoded JSON body, or raise. The key is never put in messages."""
    headers = {'Authorization': _api_key(), 'Accept': 'application/json, application/geo+json'}
    try:
        response = requests.request(method, url, params=params, json=payload, headers=headers, timeout=TIMEOUT)
    except requests.Timeout:
        raise ProviderTimeout(f'OpenRouteService timed out ({url})') from None
    except requests.ConnectionError:
        raise ProviderUnavailable(f'could not connect to OpenRouteService ({url})') from None
    except requests.RequestException as exc:
        raise ProviderUnavailable(f'OpenRouteService request failed ({url}): {type(exc).__name__}') from None

    try:
        body = response.json()
    except ValueError:
        body = None
    status = response.status_code
    if status >= 400:
        code, message = _error_details(body)
        detail = f'OpenRouteService HTTP {status}' + (f' (code {code})' if code else '') + (f': {message}' if message else '')
        if status == 403 and _is_quota_message(message):
            raise ProviderRateLimited(detail, status, code, quota_exhausted=True)
        if status in (401, 403):
            raise ProviderAuthError(detail, status, code)
        if status == 429:
            raise ProviderRateLimited(detail, status, code)
        if status >= 500 and code not in UNROUTABLE_CODES:
            raise ProviderUnavailable(detail, status, code)
        if code in UNROUTABLE_CODES:
            raise RouteNotFound(f'route not possible: {UNROUTABLE_CODES[code]}', code)
        raise ProviderBadResponse(detail, status, code)
    if not isinstance(body, dict):
        raise ProviderBadResponse(f'OpenRouteService returned a non-JSON or non-object body ({url})', status)
    return body


def _point(feature):
    """(lon, lat) from a GeoJSON Point feature, or None if unusable."""
    geometry = feature.get('geometry') if isinstance(feature, dict) else None
    if not isinstance(geometry, dict) or geometry.get('type') != 'Point':
        return None
    coords = geometry.get('coordinates')
    if not (isinstance(coords, list) and len(coords) >= 2 and all(isinstance(c, (int, float)) for c in coords[:2])):
        return None
    lon, lat = float(coords[0]), float(coords[1])
    if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
        return None
    return lon, lat


def geocode(text):
    """Resolve a US place name to {'lon', 'lat', 'label'}; raises InvalidLocation or a provider error."""
    _api_key()
    query = ' '.join(str(text or '').split())
    if not query:
        raise InvalidLocation(text, 'invalid_input', 'location must not be empty')
    if len(query) > MAX_QUERY_LENGTH:
        raise InvalidLocation(text, 'invalid_input', f'location must be at most {MAX_QUERY_LENGTH} characters')
    params = {'text': query, 'boundary.country': GEOCODE_COUNTRY, 'size': GEOCODE_SIZE}
    key = _cache_key('geocode', {'url': GEOCODE_URL, **params})
    cached = cache.get(key)
    if cached is not None:
        return cached

    body = _request('GET', GEOCODE_URL, params=params)
    features = body.get('features')
    if not isinstance(features, list):
        raise ProviderBadResponse('geocoding response has no "features" list')
    if not features:
        raise InvalidLocation(query, 'not_found', f'no location found for "{query}"')
    best = features[0]
    properties = best.get('properties') if isinstance(best, dict) else None
    point = _point(best)
    if not isinstance(properties, dict) or point is None:
        raise ProviderBadResponse('geocoding result has no usable point geometry or properties')
    if properties.get('country_a') != GEOCODE_COUNTRY:
        raise InvalidLocation(query, 'outside_usa', f'"{query}" resolved outside the USA ({properties.get("country_a")})')
    if properties.get('layer') in COARSE_LAYERS:
        raise InvalidLocation(query, 'too_coarse', f'"{query}" matched a whole {properties["layer"]}; give a city or address')
    if len(features) > 1:
        other = features[1]
        other_props = other.get('properties') if isinstance(other, dict) else None
        other_point = _point(other)
        confidence = properties.get('confidence')
        if (isinstance(other_props, dict) and other_point and isinstance(confidence, (int, float))
                and isinstance(other_props.get('confidence'), (int, float))
                and other_props['confidence'] >= confidence
                and other_props.get('country_a') == GEOCODE_COUNTRY
                and geo.haversine_mi(point[1], point[0], other_point[1], other_point[0]) > AMBIGUITY_MILES):
            raise InvalidLocation(
                query, 'ambiguous',
                f'"{query}" is ambiguous ({properties.get("label")} or {other_props.get("label")}); add a state',
            )
    result = {'lon': point[0], 'lat': point[1], 'label': str(properties.get('label') or query)}
    cache.set(key, result, CACHE_SECONDS)
    return result


def directions(start, finish):
    """One driving route between two geocoded locations, returned as a geo.Route (distance from ORS)."""
    _api_key()
    coordinates = [[start['lon'], start['lat']], [finish['lon'], finish['lat']]]
    if coordinates[0] == coordinates[1]:
        raise RouteNotFound('start and finish resolve to the same point')
    payload = {'coordinates': coordinates}
    key = _cache_key('directions', {'url': DIRECTIONS_URL, **payload})
    cached = cache.get(key)
    if cached is not None:
        return _build_route(cached)
    data = _validate_directions(_request('POST', DIRECTIONS_URL, payload=payload))
    route = _build_route(data)  # geo must accept it before it is cached
    cache.set(key, data, CACHE_SECONDS)
    return route


def _validate_directions(body):
    features = body.get('features')
    if not isinstance(features, list) or not features or not isinstance(features[0], dict):
        raise ProviderBadResponse('directions response has no route feature')
    feature = features[0]
    geometry = feature.get('geometry')
    if not isinstance(geometry, dict) or geometry.get('type') != 'LineString':
        raise ProviderBadResponse('directions route geometry is not a LineString')
    coords = geometry.get('coordinates')
    if not isinstance(coords, list) or len(coords) < 2 or not all(
        isinstance(c, list) and len(c) >= 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in c[:2])
        for c in coords
    ):
        raise ProviderBadResponse('directions route coordinates are malformed')
    properties = feature.get('properties')
    summary = properties.get('summary') if isinstance(properties, dict) else None
    distance = summary.get('distance') if isinstance(summary, dict) else None
    if not isinstance(distance, (int, float)) or isinstance(distance, bool) or not math.isfinite(distance) or distance < 0:
        raise ProviderBadResponse('directions route distance is missing or invalid')
    if distance == 0:
        raise RouteNotFound('route has zero length')
    return {'coordinates': [[float(c[0]), float(c[1])] for c in coords], 'distance_mi': distance / METRES_PER_MILE}


def _build_route(data):
    try:
        return geo.prepare_route(data['coordinates'], data['distance_mi'])
    except ValueError as exc:
        raise ProviderBadResponse(f'directions route geometry rejected: {exc}') from None
