"""Import the fuel-price CSV snapshot and locate station cities via the Census Gazetteer.

The CSV is treated as a fixed snapshot: every run validates the whole file first and
then replaces all FuelStation/FuelPriceRecord rows inside one transaction.

Station coordinates are a Gazetteer internal point (INTPTLAT/INTPTLONG): of the Places
entry for the station's city, or, only when no place matches, of a uniquely named active
town/township in the County Subdivisions file. They approximate that area's location,
not the truckstop's.
"""

import csv
import re
import unicodedata
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Exists, OuterRef

from routing.models import PRICE_DECIMAL_PLACES, PRICE_MAX_DIGITS, FuelPriceRecord, FuelStation

DEFAULT_CSV = Path(settings.BASE_DIR) / 'data' / 'fuel-prices-for-be-assessment.csv'
DEFAULT_GAZETTEER = Path(settings.BASE_DIR) / 'data' / '2026_Gaz_place_national.txt'

CSV_COLUMNS = ['OPIS Truckstop ID', 'Truckstop Name', 'Address', 'City', 'State', 'Rack ID', 'Retail Price']
GAZETTEER_COLUMNS = ['USPS', 'GEOID', 'NAME', 'LSAD', 'INTPTLAT', 'INTPTLONG']
COUSUB_COLUMNS = ['USPS', 'GEOID', 'NAME', 'FUNCSTAT', 'INTPTLAT', 'INTPTLONG']

# CSV (city, state) -> Gazetteer GEOID, for names that differ from the Gazetteer's
# or match several places. Each choice is checked against the station addresses.
CITY_OVERRIDES = {
    ('Boise', 'ID'): '1608830',                     # "Boise City city"
    ('Hot Springs National Park', 'AR'): '0533400',  # postal name of "Hot Springs city"
    ('Middlesboro', 'KY'): '2151924',               # "Middlesborough city"
    ('Pecos', 'TX'): '4873493',                     # "Town of Pecos city"
    ('Wilmington', 'IL'): '1782101',                # I-55 exit 240 is in the city (Will Co.), not the village
    ('Liberty', 'PA'): '4243128',                   # US-15 & SR-414: Tioga Co. borough
    ('Newburg', 'PA'): '4253344',                   # I-76 exit 226: Cumberland Co. borough
    ('Superior', 'WI'): '5578650',                  # city, not the adjacent village
    ('Waukesha', 'WI'): '5584250',                  # city, not the adjacent village
    ('Pewaukee', 'WI'): '5562240',                  # city, not the adjacent village
}

US_STATES = frozenset(
    'AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH '
    'NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY'.split()
)

PRICE_RE = re.compile(r'^\d+(?:\.\d+)?$')
INTEGER_RE = re.compile(r'^\d+$')
STATE_RE = re.compile(r'^[A-Z]{2}$')
MAX_LENGTHS = {'Truckstop Name': 100, 'Address': 100, 'City': 64}
MAX_OPIS_ID = 2_147_483_647
MAX_RACK_ID = 32_767
CDP_LSAD = '57'
# Consolidated-government places whose Gazetteer name is "<City>-<County> ..." or "<City>/<County> ...".
CONSOLIDATED_LSADS = frozenset({'00', 'CG', 'MG', 'UC', 'UG'})
# Legal/statistical descriptors that end Gazetteer place names (case-sensitive, longest first).
PLACE_DESCRIPTORS = re.compile(
    r'\s+(?:city and borough|consolidated government|metropolitan government|metro government|'
    r'unified government|urban county|zona urbana|comunidad|municipality|corporation|government|'
    r'borough|village|city|town|CDP)$'
)
# Descriptors that end County Subdivision names (the file has no LSAD column).
COUSUB_DESCRIPTORS = re.compile(
    r'\s+(charter township|township|town|city|borough|village|CCD|district|precinct|UT|plantation|gore|'
    r'grant|purchase|location|municipality|barrio-pueblo|barrio|subarea|reservation)$',
    re.IGNORECASE,
)
# Only functioning general-purpose governments with town-sized areas are accepted.
ACCEPTED_COUSUB_TYPES = frozenset({'town', 'township', 'charter township'})
ACTIVE_FUNCSTAT = 'A'
MAX_ERRORS_SHOWN = 50

LOCATED_OUTCOMES = ('place name', 'consolidated-city name', 'override', 'county subdivision')
UNLOCATED_OUTCOMES = ('ambiguous place', 'ambiguous subdivision', 'rejected subdivision', 'no match')


def place_key(name):
    """Normalize a place name for matching only: case, accents, punctuation, spacing, Saint/St."""
    text = unicodedata.normalize('NFKD', name)
    text = ''.join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = re.sub(r'\bsainte\b', 'ste', text)
    text = re.sub(r'\bsaint\b', 'st', text)
    text = re.sub(r'\bft\b', 'fort', text)
    text = re.sub(r'\bmt\b', 'mount', text)
    return re.sub(r'[^a-z0-9]', '', text)


def gazetteer_base_name(name):
    name = re.sub(r'\s*\(balance\)$', '', name.strip())
    return PLACE_DESCRIPTORS.sub('', name)


def read_gazetteer(path, columns, label):
    """Yield (line_number, {column: value, 'lat': float, 'lon': float}) from a pipe-delimited Gazetteer file."""
    if not path.is_file():
        raise CommandError(f'{label} file not found: {path}')
    with path.open(encoding='utf-8', newline='') as handle:
        reader = csv.reader(handle, delimiter='|')
        header = [column.strip() for column in next(reader, [])]
        missing = [column for column in columns if column not in header]
        if missing:
            raise CommandError(f'{label} {path} is missing columns {missing}; found {header}')
        position = {column: header.index(column) for column in columns}
        for line_number, fields in enumerate(reader, start=2):
            if len(fields) != len(header):
                raise CommandError(f'{label} line {line_number}: expected {len(header)} fields, got {len(fields)}')
            row = {column: fields[position[column]].strip() for column in columns}
            try:
                row['lat'], row['lon'] = float(row['INTPTLAT']), float(row['INTPTLONG'])
            except ValueError:
                raise CommandError(f'{label} line {line_number}: invalid coordinates') from None
            if not (-90 <= row['lat'] <= 90 and -180 <= row['lon'] <= 180):
                raise CommandError(f'{label} line {line_number}: coordinates out of range')
            yield line_number, row


class Command(BaseCommand):
    help = (
        'Replace all fuel stations and price records with the contents of the fuel-price CSV, '
        'locating each US station at an approximate Census Gazetteer internal point.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--csv', type=Path, default=DEFAULT_CSV, help=f'Fuel-price CSV (default: {DEFAULT_CSV}).')
        parser.add_argument(
            '--gazetteer', type=Path, default=DEFAULT_GAZETTEER,
            help=f'Census Gazetteer places file, pipe-delimited (default: {DEFAULT_GAZETTEER}).',
        )
        parser.add_argument(
            '--cousub', type=Path, default=None,
            help='Optional Census Gazetteer county subdivisions file, used only for cities with no place match.',
        )

    def handle(self, *args, **options):
        gazetteer = self.load_gazetteer(options['gazetteer'])
        cousubs = self.load_cousubs(options['cousub']) if options['cousub'] else None
        rows, trimmed_fields = self.read_csv(options['csv'])

        us_rows = [row for row in rows if row['State'] in US_STATES]
        non_us = Counter(row['State'] for row in rows if row['State'] not in US_STATES)
        stations = self.group_stations(us_rows)

        for station in stations.values():
            outcome, coordinates, detail = self.locate(gazetteer, cousubs, station['City'], station['State'])
            station['outcome'], station['detail'] = outcome, detail
            station['lat'], station['lon'] = coordinates or (None, None)

        previous = (FuelStation.objects.count(), FuelPriceRecord.objects.count())
        with transaction.atomic():
            self.replace_data(stations)

        self.report(options, rows, trimmed_fields, non_us, stations, us_rows, previous)

    def load_gazetteer(self, path):
        index = defaultdict(list)
        for _, row in read_gazetteer(path, GAZETTEER_COLUMNS, 'Gazetteer'):
            state, name, lsad = row['USPS'], row['NAME'], row['LSAD']
            base = gazetteer_base_name(name)
            entry = {'name': name, 'lsad': lsad, 'lat': row['lat'], 'lon': row['lon']}
            index[('GEOID', row['GEOID'])] = [entry]
            index[(state, place_key(base), False)].append(entry)
            if lsad in CONSOLIDATED_LSADS or name.endswith('(balance)'):
                city_part = re.split(r'[-/]', base, maxsplit=1)[0]
                if city_part != base:
                    index[(state, place_key(city_part), True)].append(entry)
        if not index:
            raise CommandError(f'Gazetteer {path} contains no places')
        missing = sorted(geoid for geoid in CITY_OVERRIDES.values() if ('GEOID', geoid) not in index)
        if missing:
            raise CommandError(f'City override GEOIDs not found in gazetteer {path}: {missing}')
        return index

    @staticmethod
    def load_cousubs(path):
        """Index every county subdivision by (state, normalized name without its descriptor)."""
        index = defaultdict(list)
        for _, row in read_gazetteer(path, COUSUB_COLUMNS, 'County subdivisions'):
            descriptor = COUSUB_DESCRIPTORS.search(row['NAME'])
            entry = {
                'name': row['NAME'], 'geoid': row['GEOID'], 'funcstat': row['FUNCSTAT'],
                'type': descriptor.group(1).lower() if descriptor else 'other', 'lat': row['lat'], 'lon': row['lon'],
            }
            index[(row['USPS'], place_key(COUSUB_DESCRIPTORS.sub('', row['NAME'])))].append(entry)
        if not index:
            raise CommandError(f'County subdivisions file {path} contains no rows')
        return index

    @classmethod
    def locate(cls, gazetteer, cousubs, city, state):
        """Return (outcome, (lat, lon) or None, detail). Subdivisions are tried only when no place matches."""
        match, problem = cls.match_city(gazetteer, city, state)
        if match:
            lat, lon, how = match
            return ('place name' if how == 'name' else how), (lat, lon), ''
        if problem:
            return 'ambiguous place', None, problem
        if cousubs is None:
            return 'no match', None, ''
        candidates = cousubs.get((state, place_key(city)), [])
        if not candidates:
            return 'no match', None, ''
        if len(candidates) > 1:
            return 'ambiguous subdivision', None, '; '.join(f'{c["name"]} [{c["geoid"]}]' for c in candidates)
        subdivision = candidates[0]
        description = f'{subdivision["name"]} [{subdivision["geoid"]}, FUNCSTAT {subdivision["funcstat"]}]'
        if subdivision['funcstat'] != ACTIVE_FUNCSTAT or subdivision['type'] not in ACCEPTED_COUSUB_TYPES:
            return 'rejected subdivision', None, description
        return 'county subdivision', (subdivision['lat'], subdivision['lon']), description

    @staticmethod
    def match_city(gazetteer, city, state):
        """Return ((lat, lon, how), None), or (None, reason) when ambiguous, or (None, None)."""
        if (city, state) in CITY_OVERRIDES:
            place = gazetteer[('GEOID', CITY_OVERRIDES[(city, state)])][0]
            return (place['lat'], place['lon'], 'override'), None
        key = place_key(city)
        for used_alias in (False, True):
            candidates = gazetteer.get((state, key, used_alias), [])
            if len(candidates) > 1:
                incorporated = [c for c in candidates if c['lsad'] != CDP_LSAD]
                candidates = incorporated or candidates
            if len({(c['lat'], c['lon']) for c in candidates}) > 1:
                return None, 'ambiguous: ' + '; '.join(c['name'] for c in candidates)
            if candidates:
                return (candidates[0]['lat'], candidates[0]['lon'], 'consolidated-city name' if used_alias else 'name'), None
        return None, None

    def read_csv(self, path):
        if not path.is_file():
            raise CommandError(f'CSV file not found: {path}')
        errors = []
        rows = []
        trimmed_fields = 0
        with path.open(encoding='utf-8', newline='') as handle:
            reader = csv.reader(handle)
            header = next(reader, [])
            missing = [column for column in CSV_COLUMNS if column not in header]
            if missing:
                raise CommandError(f'CSV {path} is missing columns {missing}; found {header}')
            position = {column: header.index(column) for column in CSV_COLUMNS}
            for fields in reader:
                line = reader.line_num
                if len(fields) != len(header):
                    errors.append(f'line {line}: expected {len(header)} fields, got {len(fields)}')
                    continue
                row = {'line': line}
                for column in CSV_COLUMNS:
                    raw = fields[position[column]]
                    row[column] = raw.strip()
                    trimmed_fields += raw != row[column]
                errors.extend(f'line {line}: {problem}' for problem in self.validate_row(row))
                rows.append(row)
        if not rows and not errors:
            errors.append('no data rows')
        if errors:
            self.fail(f'CSV validation failed for {path}', errors)
        return rows, trimmed_fields

    @staticmethod
    def validate_row(row):
        problems = [f'{column} is empty' for column in CSV_COLUMNS if not row[column]]
        if problems:
            return problems
        if not INTEGER_RE.match(row['OPIS Truckstop ID']) or int(row['OPIS Truckstop ID']) > MAX_OPIS_ID:
            problems.append(f'invalid OPIS Truckstop ID {row["OPIS Truckstop ID"]!r}')
        if not INTEGER_RE.match(row['Rack ID']) or int(row['Rack ID']) > MAX_RACK_ID:
            problems.append(f'invalid Rack ID {row["Rack ID"]!r}')
        if not STATE_RE.match(row['State']):
            problems.append(f'invalid State {row["State"]!r}')
        for column, limit in MAX_LENGTHS.items():
            if len(row[column]) > limit:
                problems.append(f'{column} longer than {limit} characters')
        price_text = row['Retail Price']
        if not PRICE_RE.match(price_text):
            problems.append(f'invalid Retail Price {price_text!r}')
        else:
            price = Decimal(price_text)
            exponent = price.as_tuple().exponent
            if price <= 0:
                problems.append(f'Retail Price must be positive, got {price_text!r}')
            elif -exponent > PRICE_DECIMAL_PLACES or len(price.as_tuple().digits) > PRICE_MAX_DIGITS:
                problems.append(f'Retail Price {price_text!r} exceeds {PRICE_MAX_DIGITS} digits / {PRICE_DECIMAL_PLACES} decimals')
            else:
                row['price'] = price
        return problems

    def group_stations(self, rows):
        """One station per OPIS ID; its descriptive fields must agree on every line."""
        stations = {}
        errors = []
        for row in rows:
            opis_id = int(row['OPIS Truckstop ID'])
            station = stations.get(opis_id)
            if station is None:
                stations[opis_id] = {**row, 'opis_id': opis_id, 'records': [row]}
                continue
            for column in ('Address', 'City', 'State', 'Rack ID'):
                if row[column] != station[column]:
                    errors.append(
                        f'line {row["line"]}: OPIS ID {opis_id} has {column} {row[column]!r}, '
                        f'but line {station["line"]} has {station[column]!r}'
                    )
            station['records'].append(row)
        if errors:
            self.fail('Rows sharing an OPIS Truckstop ID disagree', errors)
        for station in stations.values():
            lowest = min(station['records'], key=lambda record: (record['price'], record['line']))
            station['lowest_price'], station['lowest_line'] = lowest['price'], lowest['line']
        return stations

    def replace_data(self, stations):
        """Delete and re-insert all imported rows. Must run inside transaction.atomic()."""
        FuelPriceRecord.objects.all().delete()
        FuelStation.objects.all().delete()
        created = FuelStation.objects.bulk_create(
            [
                FuelStation(
                    opis_id=s['opis_id'], name=s['Truckstop Name'], address=s['Address'], city=s['City'],
                    state=s['State'], rack_id=int(s['Rack ID']), latitude=s['lat'], longitude=s['lon'],
                    lowest_price=s['lowest_price'], lowest_price_source_line=s['lowest_line'],
                )
                for s in stations.values()
            ],
            batch_size=500,
        )
        by_opis_id = {station.opis_id: station for station in created}
        records = [
            FuelPriceRecord(
                station=by_opis_id[s['opis_id']], source_line=r['line'],
                listed_name=r['Truckstop Name'], retail_price=r['price'],
            )
            for s in stations.values()
            for r in s['records']
        ]
        FuelPriceRecord.objects.bulk_create(records, batch_size=500)

        expected = (len(stations), len(records))
        actual = (FuelStation.objects.count(), FuelPriceRecord.objects.count())
        if actual != expected:
            raise CommandError(f'Inserted {actual} stations/records, expected {expected}; rolled back')
        selected_record = FuelPriceRecord.objects.filter(
            station=OuterRef('pk'),
            source_line=OuterRef('lowest_price_source_line'),
            retail_price=OuterRef('lowest_price'),
        )
        inconsistent = list(
            FuelStation.objects.exclude(Exists(selected_record)).values_list('opis_id', flat=True)[:MAX_ERRORS_SHOWN]
        )
        if inconsistent:
            raise CommandError(
                f'lowest_price does not match its source record for OPIS IDs {inconsistent}; rolled back'
            )

    def fail(self, title, errors):
        for error in errors[:MAX_ERRORS_SHOWN]:
            self.stderr.write(f'  {error}')
        if len(errors) > MAX_ERRORS_SHOWN:
            self.stderr.write(f'  ... and {len(errors) - MAX_ERRORS_SHOWN} more')
        raise CommandError(f'{title}: {len(errors)} problem(s); nothing was changed')

    def report(self, options, rows, trimmed_fields, non_us, stations, us_rows, previous):
        out = self.stdout.write
        multi_price = sum(1 for s in stations.values() if len(s['records']) > 1)
        lowest_ties = sum(
            1 for s in stations.values() if sum(r['price'] == s['lowest_price'] for r in s['records']) > 1
        )
        # Each station has exactly one outcome, so these groups do not overlap.
        groups = defaultdict(lambda: defaultdict(list))
        for s in stations.values():
            groups[s['outcome']][(s['City'], s['State'], s['detail'])].append(s)

        def line(outcome):
            cities = groups[outcome]
            return f'{sum(map(len, cities.values()))} stations in {len(cities)} cities'

        located = sum(1 for s in stations.values() if s['lat'] is not None)
        out(f'CSV:        {options["csv"]}')
        out(f'Gazetteer:  {options["gazetteer"]}')
        out(f'Subdivisions: {options["cousub"] or "not used"}')
        out(f'Replaced previous data: {previous[0]} stations, {previous[1]} price records')
        out(f'CSV data rows read:              {len(rows)} (all valid)')
        out(f'Text fields trimmed of surrounding whitespace: {trimmed_fields}')
        out(f'Excluded non-US rows:            {sum(non_us.values())} ' + str(dict(sorted(non_us.items()))))
        out(f'Imported price records:          {len(us_rows)}')
        out(f'Imported stations (OPIS IDs):    {len(stations)}')
        out(f'  with more than one price row:  {multi_price}')
        out(f'  lowest price on several lines: {lowest_ties} (earliest line used)')
        out(f'Located at an approximate Gazetteer internal point: {located}')
        for outcome in LOCATED_OUTCOMES:
            out(f'  {outcome + ":":26s}{line(outcome)}')
        out(f'Unlocated: {len(stations) - located}')
        for outcome in UNLOCATED_OUTCOMES:
            out(f'  {outcome + ":":26s}{line(outcome)}')
        for outcome in ('consolidated-city name', 'override', 'county subdivision', *UNLOCATED_OUTCOMES):
            if groups[outcome]:
                out(f'[{outcome}]')
            for (city, state, detail), members in sorted(groups[outcome].items(), key=lambda i: (i[0][1], i[0][0])):
                out(f'    {city}, {state}: {len(members)} station(s)' + (f'  -> {detail}' if detail else ''))
        self.stdout.write(self.style.SUCCESS('Import committed.'))
