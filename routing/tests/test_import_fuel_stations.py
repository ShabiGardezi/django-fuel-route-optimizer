import io
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from routing.models import FuelPriceRecord, FuelStation

CSV_HEADER = 'OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price'
PLACES_HEADER = 'USPS|GEOID|GEOIDFQ|ANSICODE|NAME|LSAD|FUNCSTAT|ALAND|AWATER|ALAND_SQMI|AWATER_SQMI|INTPTLAT|INTPTLONG'
COUSUB_HEADER = 'USPS|GEOID|GEOIDFQ|ANSICODE|NAME|FUNCSTAT|ALAND|AWATER|ALAND_SQMI|AWATER_SQMI|INTPTLAT|INTPTLONG'
# Every GEOID in CITY_OVERRIDES must exist in the places file, so the fixture always includes them.
OVERRIDE_PLACES = [
    ('ID', '1608830'), ('AR', '0533400'), ('KY', '2151924'), ('TX', '4873493'), ('IL', '1782101'),
    ('PA', '4243128'), ('PA', '4253344'), ('WI', '5578650'), ('WI', '5584250'), ('WI', '5562240'),
]


def place(state, geoid, name, lsad, lat, lon):
    return f'{state}|{geoid}|x|x|{name}|{lsad}|A|0|0|0|0|{lat}|{lon}'


def cousub(state, geoid, name, funcstat, lat, lon):
    return f'{state}|{geoid}|x|x|{name}|{funcstat}|0|0|0|0|{lat}|{lon}'


class ImportFuelStationsTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name, header, lines):
        path = self.dir / name
        path.write_text('\n'.join([header, *lines]) + '\n', encoding='utf-8')
        return path

    def run_import(self, csv_lines, places, cousubs=None):
        base_places = [place(state, geoid, f'Override {geoid} city', '25', 40.0, -90.0) for state, geoid in OVERRIDE_PLACES]
        args = [
            '--csv', str(self.write('prices.csv', CSV_HEADER, csv_lines)),
            '--gazetteer', str(self.write('places.txt', PLACES_HEADER, base_places + places)),
        ]
        if cousubs is not None:
            args += ['--cousub', str(self.write('cousubs.txt', COUSUB_HEADER, cousubs))]
        out = io.StringIO()
        call_command('import_fuel_stations', *args, stdout=out, stderr=io.StringIO())
        return out.getvalue()

    def station(self, opis_id):
        return FuelStation.objects.get(opis_id=opis_id)

    def test_unique_active_town_is_accepted(self):
        output = self.run_import(
            ['1,STOP,"I-95, EXIT 1",Smalltown,CT,100,3.509'],
            places=[],
            cousubs=[cousub('CT', '0900100000', 'Smalltown town', 'A', 41.5, -72.5)],
        )
        station = self.station(1)
        self.assertEqual((station.latitude, station.longitude), (41.5, -72.5))
        self.assertIn('county subdivision:       1 stations in 1 cities', output)

    def test_active_charter_township_is_accepted(self):
        self.run_import(
            ['1,STOP,I-275,Canton,MI,325,3.699'],
            places=[],
            cousubs=[cousub('MI', '2616312560', 'Canton charter township', 'A', 42.3, -83.5)],
        )
        self.assertEqual((self.station(1).latitude, self.station(1).longitude), (42.3, -83.5))

    def test_ccd_is_rejected(self):
        output = self.run_import(
            ['1,STOP,"I-65, EXIT 164",Hope Hull,AL,720,3.139'],
            places=[],
            cousubs=[cousub('AL', '0110191665', 'Hope Hull CCD', 'S', 32.2, -86.4)],
        )
        self.assertIsNone(self.station(1).latitude)
        self.assertIn('rejected subdivision:     1 stations in 1 cities', output)

    def test_non_town_types_are_rejected_even_when_active(self):
        self.run_import(
            ['1,STOP,SR-4,Baker,FL,127,3.299', '2,STOP,US-58,Drewryville,VA,130,3.049'],
            places=[],
            cousubs=[
                cousub('FL', '1209190104', 'Baker CCD', 'A', 30.8, -86.7),
                cousub('VA', '5117593407', 'Drewryville district', 'A', 36.7, -77.3),
            ],
        )
        self.assertIsNone(self.station(1).latitude)
        self.assertIsNone(self.station(2).latitude)

    def test_inactive_township_is_rejected(self):
        self.run_import(
            ['1,STOP,"I-40, EXIT 208",Colfax,NC,65,3.099'],
            places=[],
            cousubs=[cousub('NC', '3716190720', 'Colfax township', 'N', 36.1, -80.0)],
        )
        self.assertIsNone(self.station(1).latitude)

    def test_ambiguous_subdivisions_are_rejected(self):
        output = self.run_import(
            ['1,STOP,US-61,Taylor,MO,494,3.016'],
            places=[],
            cousubs=[
                cousub('MO', '2907772466', 'Taylor township', 'A', 39.9, -91.5),
                cousub('MO', '2920572520', 'Taylor township', 'A', 39.7, -92.4),
            ],
        )
        self.assertIsNone(self.station(1).latitude)
        self.assertIn('ambiguous subdivision:    1 stations in 1 cities', output)

    def test_town_sharing_a_name_with_another_subdivision_is_ambiguous(self):
        self.run_import(
            ['1,STOP,SR-1,Lewis,NY,12,3.719'],
            places=[],
            cousubs=[
                cousub('NY', '3603142114', 'Lewis town', 'A', 43.4, -74.6),
                cousub('NY', '3603199999', 'Lewis CCD', 'S', 43.5, -74.7),
            ],
        )
        self.assertIsNone(self.station(1).latitude)

    def test_place_match_never_falls_through_to_subdivision(self):
        output = self.run_import(
            ['1,STOP,US-1,Springfield,MA,100,3.599'],
            places=[place('MA', '2567000', 'Springfield city', '25', 42.1, -72.5)],
            cousubs=[cousub('MA', '2501367000', 'Springfield town', 'A', 10.0, -10.0)],
        )
        self.assertEqual((self.station(1).latitude, self.station(1).longitude), (42.1, -72.5))
        self.assertIn('place name:               1 stations', output)
        self.assertIn('county subdivision:       0 stations', output)

    def test_subdivisions_unused_without_cousub_option(self):
        output = self.run_import(['1,STOP,"I-95, EXIT 1",Smalltown,CT,100,3.509'], places=[])
        self.assertIsNone(self.station(1).latitude)
        self.assertIn('Subdivisions: not used', output)

    def test_failed_insert_rolls_back_previous_data(self):
        self.run_import(
            ['1,STOP,US-1,Springfield,MA,100,3.599', '1,STOP 2,US-1,Springfield,MA,100,3.499'],
            places=[place('MA', '2567000', 'Springfield city', '25', 42.1, -72.5)],
        )
        before = list(FuelPriceRecord.objects.values_list('source_line', 'retail_price'))
        self.assertEqual(self.station(1).lowest_price, Decimal('3.499'))

        with mock.patch.object(FuelPriceRecord.objects, 'bulk_create', side_effect=RuntimeError('insert failed')):
            with self.assertRaises(RuntimeError):
                self.run_import(['2,OTHER,US-2,Springfield,MA,100,2.999'], places=[])

        self.assertEqual(list(FuelStation.objects.values_list('opis_id', flat=True)), [1])
        self.assertEqual(list(FuelPriceRecord.objects.values_list('source_line', 'retail_price')), before)
