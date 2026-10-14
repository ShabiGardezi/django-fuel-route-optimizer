from django.db import models
from django.db.models import Q

# Prices in the CSV have one integer digit and up to 8 decimal places
# (e.g. 3.00733333); 12/8 leaves headroom without float rounding.
PRICE_MAX_DIGITS = 12
PRICE_DECIMAL_PLACES = 8


class FuelStation(models.Model):
    """One truckstop, identified by its OPIS Truckstop ID.

    Rows sharing an OPIS ID in the source CSV always have the same address,
    city, state and rack ID, so the ID is a stable identity across imports.
    Their prices are kept individually in FuelPriceRecord.

    Coordinates are the Census Gazetteer internal point of the place (or, failing
    that, the town/township county subdivision) named by the station's city. They
    approximate that area, not the station's real location. They are null when no
    unambiguous match was found, and such stations cannot be placed on a route.
    """

    opis_id = models.PositiveIntegerField(unique=True, help_text='OPIS Truckstop ID from the source CSV.')
    name = models.CharField(max_length=100, help_text='Name from the first CSV line for this station.')
    address = models.CharField(max_length=100)
    city = models.CharField(max_length=64)
    state = models.CharField(max_length=2)
    rack_id = models.PositiveSmallIntegerField(help_text='Rack ID from the source CSV (metadata only).')
    latitude = models.FloatField(
        null=True, blank=True, help_text='Approximate: Gazetteer internal point of the matched area, not the station.'
    )
    longitude = models.FloatField(
        null=True, blank=True, help_text='Approximate: Gazetteer internal point of the matched area, not the station.'
    )
    # Denormalized from FuelPriceRecord so route queries need no aggregation.
    lowest_price = models.DecimalField(
        max_digits=PRICE_MAX_DIGITS,
        decimal_places=PRICE_DECIMAL_PLACES,
        help_text='Lowest listed retail price in the dataset (USD/gal); not a live pump price.',
    )
    lowest_price_source_line = models.PositiveIntegerField(
        help_text='CSV line number of the record that supplied lowest_price.'
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(latitude__isnull=True, longitude__isnull=True)
                | Q(latitude__isnull=False, longitude__isnull=False),
                name='fuelstation_coordinates_both_or_neither',
            ),
            models.CheckConstraint(
                condition=Q(latitude__isnull=True) | Q(latitude__gte=-90, latitude__lte=90),
                name='fuelstation_latitude_range',
            ),
            models.CheckConstraint(
                condition=Q(longitude__isnull=True) | Q(longitude__gte=-180, longitude__lte=180),
                name='fuelstation_longitude_range',
            ),
            models.CheckConstraint(condition=Q(lowest_price__gt=0), name='fuelstation_lowest_price_positive'),
        ]

    def __str__(self):
        return f'{self.name} ({self.city}, {self.state}) #{self.opis_id}'


class FuelPriceRecord(models.Model):
    """One price line from the source CSV, kept even when a station has several."""

    station = models.ForeignKey(FuelStation, on_delete=models.CASCADE, related_name='price_records')
    source_line = models.PositiveIntegerField(unique=True, help_text='Line number in the CSV (header is line 1).')
    listed_name = models.CharField(max_length=100, help_text='Truckstop name exactly as written on this line.')
    retail_price = models.DecimalField(max_digits=PRICE_MAX_DIGITS, decimal_places=PRICE_DECIMAL_PLACES)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(retail_price__gt=0), name='fuelpricerecord_retail_price_positive'),
        ]

    def __str__(self):
        return f'{self.station.opis_id} line {self.source_line}: {self.retail_price}'
