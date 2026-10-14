"""POST /api/route/: geocode, route, find corridor stations, and plan fuel stops.

All error responses share one shape: {"error": {"code": str, "message": str, "details": {...}}}.
Decimal quantities (miles, gallons, prices, costs) are serialized as decimal strings, never floats.
"""

import logging
from decimal import Decimal

from django.conf import settings
from django.db.models import Count, Max
from rest_framework import exceptions as drf_exceptions
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from routing import geo, optimizer, ors
from routing.models import PRICE_DECIMAL_PLACES, FuelPriceRecord, FuelStation
from routing.serializers import RouteRequestSerializer

logger = logging.getLogger(__name__)

# ORS routing codes that mean the request exceeds provider limits (e.g. maximum route length),
# as opposed to the locations genuinely having no drivable connection.
PROVIDER_LIMIT_CODES = {2004, 2017}
PRICE_QUANTUM = Decimal(1).scaleb(-PRICE_DECIMAL_PLACES)


def dec(value):
    """Decimal -> plain decimal string (no exponent); None stays None."""
    return None if value is None else format(value, 'f')


def error_response(http_status, code, message, details=None):
    return Response({'error': {'code': code, 'message': message, 'details': details or {}}}, status=http_status)


def assumptions(corridor_miles):
    return [
        'Station locations are approximate: each station is placed at the Census Gazetteer point of its '
        'city or town, not at the truckstop itself.',
        f'A station is a candidate when that point is within {corridor_miles:g} miles of the route; detours '
        'to reach a station are not added to the distance or fuel consumed.',
        'Vehicle model: 50-gallon tank, 10 miles per gallon (500-mile range), starting with a full tank '
        'whose fuel is not counted as expenditure; fuel can be bought in any amount, and the plan arrives '
        'with no more fuel than it needs.',
        "Prices are each station's lowest listed price in the supplied dataset, not live pump prices.",
        'Within this model the plan minimises fuel expenditure; it is not a guarantee of the cheapest '
        'real-world trip.',
        'Stop costs are rounded to cents individually for display; the total is the unrounded sum rounded '
        'once to cents (half up), so displayed stop costs may not add up exactly to the total.',
    ]


class RouteView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        if not isinstance(request.data, dict):
            raise drf_exceptions.ValidationError({'non_field_errors': ['Request body must be a JSON object.']})
        serializer = RouteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        start_text, finish_text = serializer.validated_data['start'], serializer.validated_data['finish']

        start = _geocode('start', start_text)
        finish = _geocode('finish', finish_text)
        if (start['lon'], start['lat']) == (finish['lon'], finish['lat']):
            return error_response(
                status.HTTP_400_BAD_REQUEST, 'same_location',
                'Start and finish resolve to the same location.', {'label': start['label']},
            )

        route = ors.directions(start, finish)
        corridor_miles = float(settings.FUEL_CORRIDOR_MILES)
        stations = FuelStation.objects.filter(latitude__isnull=False, longitude__isnull=False, lowest_price__gt=0)
        visits = geo.find_station_visits(route, stations, corridor_miles)
        plan = optimizer.plan_route_fuel(route.distance_mi, visits)

        if not plan.feasible:
            return error_response(
                status.HTTP_422_UNPROCESSABLE_ENTITY, 'no_feasible_fuel_plan',
                'No sequence of candidate fuel stations keeps the vehicle within its 500-mile range on this route.',
                {
                    'reason': plan.reason,
                    'gap_start_mile': dec(plan.gap_start_mi),
                    'gap_end_mile': dec(plan.gap_end_mi),
                    'max_range_miles': dec(optimizer.MAX_RANGE_MILES),
                    'route_distance_miles': dec(plan.distance_mi),
                    'corridor_miles': corridor_miles,
                    'candidate_station_visits': len(visits),
                    'note': 'Candidates depend on approximate station locations and the corridor width.',
                },
            )
        return Response(_success_payload(start_text, finish_text, start, finish, route, plan, visits, corridor_miles))


def _geocode(field, text):
    try:
        return ors.geocode(text)
    except ors.InvalidLocation as exc:
        exc.field = field
        raise


def _price(value):
    """Database aggregates can lose the column scale (SQLite MAX); restore the model's decimal places."""
    return None if value is None else Decimal(value).quantize(PRICE_QUANTUM)


def _location(query, place):
    return {'query': query, 'label': place['label'], 'lat': place['lat'], 'lon': place['lon']}


def _success_payload(start_text, finish_text, start, finish, route, plan, visits, corridor_miles):
    station_ids = [stop.station.pk for stop in plan.stops]
    price_stats = {
        row['station_id']: row
        for row in FuelPriceRecord.objects.filter(station_id__in=station_ids)
        .values('station_id').annotate(listed_price_max=Max('retail_price'), listed_price_count=Count('id'))
    }
    stops = []
    for number, stop in enumerate(plan.stops, start=1):
        station = stop.station
        stats = price_stats.get(station.pk, {})
        stops.append({
            'sequence': number,
            'station': {
                'opis_id': station.opis_id,
                'name': station.name,
                'address': station.address,
                'city': station.city,
                'state': station.state,
                'approximate_location': {'lat': station.latitude, 'lon': station.longitude},
            },
            'visit_index': stop.visit_index,
            'route_mile': dec(stop.mile),
            'offset_from_route_miles': dec(stop.offset_mi),
            'arrival_fuel_gallons': dec(stop.arrival_fuel_gal),
            'gallons_purchased': dec(stop.gallons),
            'price_per_gallon': dec(stop.price),
            'price_source_csv_line': station.lowest_price_source_line,
            'listed_price_max': dec(_price(stats.get('listed_price_max'))),
            'listed_price_count': stats.get('listed_price_count'),
            'stop_cost_rounded': dec(stop.cost_display),
            'stop_cost_unrounded': dec(stop.cost),
        })
    return {
        'start': _location(start_text, start),
        'finish': _location(finish_text, finish),
        'route': {
            'distance_miles': dec(plan.distance_mi),
            'geometry': {'type': 'LineString', 'coordinates': [[lon, lat] for lat, lon in zip(route.lats, route.lons)]},
        },
        'fuel_stops': stops,
        'fuel_summary': {
            'tank_capacity_gallons': dec(optimizer.TANK_GALLONS),
            'miles_per_gallon': dec(optimizer.MILES_PER_GALLON),
            'starting_fuel_gallons': dec(plan.starting_fuel_gal),
            'fuel_consumed_gallons': dec(plan.fuel_consumed_gal),
            'fuel_purchased_gallons': dec(plan.fuel_purchased_gal),
            'ending_fuel_gallons': dec(plan.ending_fuel_gal),
            'total_cost_usd': dec(plan.total_cost),
            'total_cost_unrounded_usd': dec(plan.cost_unrounded),
            'currency': 'USD',
        },
        'candidate_station_visits': len(visits),
        'corridor_miles': corridor_miles,
        'assumptions': assumptions(corridor_miles),
    }


def api_exception_handler(exc, context):
    """Map every exception raised by API views to the shared error shape."""
    if isinstance(exc, ors.ORSError):
        return _ors_error_response(exc)
    if isinstance(exc, drf_exceptions.ValidationError):
        detail = exc.detail if isinstance(exc.detail, dict) else {'non_field_errors': exc.detail}
        return error_response(status.HTTP_400_BAD_REQUEST, 'invalid_request', 'The request is invalid.', {'fields': detail})
    if isinstance(exc, drf_exceptions.APIException):
        code = exc.get_codes() if isinstance(exc.get_codes(), str) else exc.default_code
        return error_response(exc.status_code, 'invalid_request' if exc.status_code == 400 else code, str(exc.detail))
    logger.exception('Unexpected error while handling %s', context.get('view').__class__.__name__)
    return error_response(status.HTTP_500_INTERNAL_SERVER_ERROR, 'internal_error', 'An unexpected error occurred.')


def _ors_error_response(exc):
    # Order matters: subclasses before their bases.
    if isinstance(exc, ors.InvalidLocation):
        return error_response(
            status.HTTP_400_BAD_REQUEST, 'invalid_location', str(exc),
            {'field': getattr(exc, 'field', None), 'reason': exc.reason},
        )
    if isinstance(exc, ors.RouteNotFound):
        if exc.code in PROVIDER_LIMIT_CODES:
            return error_response(
                status.HTTP_422_UNPROCESSABLE_ENTITY, 'route_exceeds_provider_limits',
                "The trip exceeds the routing provider's limits, so no route could be calculated.",
                {'provider_code': exc.code},
            )
        return error_response(
            status.HTTP_422_UNPROCESSABLE_ENTITY, 'route_not_found',
            'No drivable route was found between start and finish.', {'provider_code': exc.code},
        )
    logger.warning('OpenRouteService failure (%s): %s', type(exc).__name__, exc)  # messages never contain the key
    if isinstance(exc, ors.ConfigurationError):
        return error_response(status.HTTP_500_INTERNAL_SERVER_ERROR, 'server_misconfigured',
                              'The routing service is not configured on this server.')
    if isinstance(exc, ors.ProviderRateLimited):
        return error_response(status.HTTP_503_SERVICE_UNAVAILABLE, 'provider_rate_limited',
                              'The routing provider rate limit or daily quota was reached; try again later.',
                              {'quota_exhausted': exc.quota_exhausted})
    if isinstance(exc, ors.ProviderTimeout):
        return error_response(status.HTTP_504_GATEWAY_TIMEOUT, 'provider_timeout', 'The routing provider timed out.')
    if isinstance(exc, ors.ProviderUnavailable):
        return error_response(status.HTTP_503_SERVICE_UNAVAILABLE, 'provider_unavailable',
                              'The routing provider is unavailable; try again later.')
    if isinstance(exc, ors.ProviderAuthError):
        return error_response(status.HTTP_502_BAD_GATEWAY, 'provider_auth_error',
                              'The routing provider rejected this server\'s credentials.')
    if isinstance(exc, ors.ProviderBadResponse):
        return error_response(status.HTTP_502_BAD_GATEWAY, 'provider_bad_response',
                              'The routing provider returned an unusable response.')
    return error_response(status.HTTP_502_BAD_GATEWAY, 'provider_error', 'The routing provider failed.')
