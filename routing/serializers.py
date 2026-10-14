import re

from rest_framework import serializers

from routing.ors import MAX_QUERY_LENGTH

# "40.71,-74.00" or "40.71 -74.00": the API accepts place names only.
COORDINATES_RE = re.compile(r'^\s*[-+]?\d+(?:\.\d+)?\s*[,;\s]\s*[-+]?\d+(?:\.\d+)?\s*$')


class PlaceNameField(serializers.CharField):
    """A non-blank place name string; numbers, lists, objects and coordinate pairs are rejected."""

    default_error_messages = {
        'not_a_string': 'Must be a place name string, e.g. "Boston, MA".',
        'coordinates': 'Coordinates are not accepted; give a US place name such as "Boston, MA".',
    }

    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail('not_a_string')
        value = super().to_internal_value(data)
        if COORDINATES_RE.match(value):
            self.fail('coordinates')
        return value


class RouteRequestSerializer(serializers.Serializer):
    start = PlaceNameField(max_length=MAX_QUERY_LENGTH)
    finish = PlaceNameField(max_length=MAX_QUERY_LENGTH)
