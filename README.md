# Fuel Route Optimizer

A Django REST API that takes a start and finish location in the USA and returns:

- the driving route (GeoJSON line and distance),
- where to stop for fuel along the way, and how much to buy at each stop,
- the total money spent on fuel.

The vehicle has a 50-gallon tank, drives 10 miles per gallon (a 500-mile range), and starts with a full tank.
Fuel prices come from the supplied `fuel-prices-for-be-assessment.csv`.

> **Read this first: how results should be interpreted**
> - **Cost counts fuel bought during the trip.** The initial 50-gallon tank is assumed full and free, so `total_cost_usd` is the cost of fuel *purchased along the route*, not the cost of all fuel consumed. For example, a 1,000-mile trip consumes 100 gallons but buys only about 50.
> - **Station locations are approximate.** They are Census place or subdivision points for each station's city or town, used as stand-ins. They are not verified fuel-station coordinates.
> - **"Optimal" means optimal within the model.** Stops and costs minimise fuel expenditure under the documented model (section 5). That is not a guarantee of the cheapest real-world trip.

**Stack:** Python 3.12, Django 6.1.2, Django REST Framework 3.18.3, `requests`, SQLite.
**External services:** OpenRouteService on `api.heigit.org` — geocoding (Pelias) and driving directions.
Fuel stations are **not** geocoded through the API; they are located once, offline, from US Census Gazetteer files.

---

## 1. Setup

Requires Python 3.12 (developed with 3.12.8).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then edit .env (see below); .env is git-ignored
python manage.py migrate
```

Load the fuel data (section 3), then start the server:

```bash
python manage.py runserver    # http://127.0.0.1:8000/api/route/
```

These settings are for local development. `python manage.py check --deploy` reports the usual production-hardening items: HSTS, SSL redirect, secure cookies, and the console email backend from the project template.

### Environment variables

`config/settings.py` reads these from the environment, and also from a `.env` file in the project root.
Real environment variables take precedence over `.env`.

| Variable | Required | Purpose |
|---|---|---|
| `ORS_API_KEY` | yes | OpenRouteService API key. Without it the endpoint returns `500 server_misconfigured`. |
| `DJANGO_DEBUG` | no | `true` for local development. Any other value, or unset, means debug is off. |
| `DJANGO_SECRET_KEY` | when debug is off | Required when `DJANGO_DEBUG` is not `true`; startup fails without it. With `DJANGO_DEBUG=true` a development-only key is used if it is blank. |
| `DJANGO_ALLOWED_HOSTS` | no | Comma-separated host names. Default: `localhost,127.0.0.1`. |
| `FUEL_CORRIDOR_MILES` | no | How close (in miles) a station's approximate location must be to the route. Default: `10`. Must be a number greater than 0; otherwise Django refuses to start with `ImproperlyConfigured`. |

**`.env` format:** one `KEY=value` per line. Blank lines and lines starting with `#` are ignored. A leading `export ` is allowed, and one pair of matching single or double quotes around a value is removed. Inline comments, escapes and variable expansion are **not** supported: put comments on their own line.

`.env.example` contains the intended local-development values with an empty key.

**Getting an ORS key:**

1. Create a free account at [openrouteservice.org](https://openrouteservice.org/plans/) and copy the API key from its dashboard.
2. Put it in `.env` as `ORS_API_KEY=...`.
3. Never commit it. `.env` is git-ignored, and the key is sent only in the `Authorization` header — never in URLs, cache keys, logs or API responses.

The free plan allows about 2,000 directions and 1,000 geocoding requests per day.

---

## 2. Project layout

```
config/                     settings, URLs
routing/models.py           FuelStation, FuelPriceRecord
routing/management/commands/import_fuel_stations.py   CSV + Gazetteer import
routing/geo.py              route geometry and station corridor visits
routing/optimizer.py        fuel-stop optimizer (Decimal arithmetic)
routing/ors.py              OpenRouteService client (geocoding, directions, caching, typed errors)
routing/serializers.py      request validation
routing/views.py            POST /api/route/ and error mapping
routing/tests/              tests for every module above
scripts/benchmark_corridor.py   corridor-matching benchmark
data/README.md              where the source data files go (the files themselves are not committed)
```

---

## 3. Fuel dataset setup

Everything in `data/` except `data/README.md` is ignored by Git. Put three files there:

```bash
cp /path/to/fuel-prices-for-be-assessment.csv data/

# Download into data/ (git-ignored), never the repository root
curl -L -o data/2026_Gaz_place_national.zip https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2026_Gazetteer/2026_Gaz_place_national.zip
curl -L -o data/2026_Gaz_cousubs_national.zip https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2026_Gazetteer/2026_Gaz_cousubs_national.zip
unzip -o data/2026_Gaz_place_national.zip -d data/
unzip -o data/2026_Gaz_cousubs_national.zip -d data/
```

This should produce `data/2026_Gaz_place_national.txt` and `data/2026_Gaz_cousubs_national.txt`, which are the importer's default `--gazetteer` path and the `--cousub` path used below. The zips can then be deleted.

The CSV is supplied with the assessment. The two Gazetteer files are the official US Census 2026 Places and County Subdivisions files.

Then import:

```bash
python manage.py import_fuel_stations --cousub data/2026_Gaz_cousubs_national.txt
```

### What the import does

**Transaction and validation**
- The CSV is treated as a fixed snapshot. Every run validates the whole file, then replaces all station and price rows inside **one transaction**.
- If any row is malformed, the command lists the problems, exits non-zero, and leaves the database unchanged. Re-running it produces the same data.
- Options: `--csv` and `--gazetteer` override the default paths under `data/`. `--cousub` is optional.

**Stations and prices**
- Each OPIS Truckstop ID becomes one `FuelStation`. Rows sharing an ID always have the same address, city, state and rack ID.
- **Every** CSV price row is kept as a `FuelPriceRecord`, with its CSV line number and the station name as written on that line.
- The price the optimizer uses is the station's **lowest listed price**; ties go to the earliest line. The station records which CSV line it came from.
- Prices are parsed as `Decimal` directly from the CSV text.

**Excluded rows**
- **Canadian rows are excluded:** 620 rows from AB, BC, MB, NB, NS, ON, QC, SK and YT.
- Text fields are trimmed of surrounding whitespace, and the report counts how many were trimmed.

**Locating stations.** The CSV has no coordinates, and its `Address` column holds highway descriptions such as "I-44, EXIT 283", so stations are located at the **approximate Census internal point** of their city:
1. **Census Places:** the city name, after normalizing case, punctuation, Saint/St and suffixes such as "city" or "town". This includes consolidated cities such as Nashville-Davidson.
2. **Overrides:** 10 small, documented `CITY_OVERRIDES` for names that differ or match several places.
3. **County Subdivisions (`--cousub`):** only when no place matches, and only an **unambiguous, active town, township or charter township**. Census County Divisions, districts and ambiguous names are rejected.

**Unlocated stations** keep null coordinates and can never be fuel stops. The report lists them.

### Totals from the supplied data

| Item | Count |
|---|---|
| CSV rows read | 8,151 (all valid) |
| Imported price records | 7,531 |
| Imported stations | 6,626 |
| Stations with more than one price row | 568 |
| **Located** | **6,425**: 6,272 by place name, 28 by consolidated-city name, 28 by override, 97 by county subdivision |
| **Unlocated** | **201**: 5 ambiguous subdivision, 17 rejected subdivision, 179 no match |

---

## 4. API

### `POST /api/route/`

```http
POST /api/route/
Content-Type: application/json

{"start": "New York, NY", "finish": "Boston, MA"}
```

**Trailing slash:** `/api/route/` is the documented form. `/api/route` (no slash) is routed to the same view and served directly, with no redirect, so a POST to either works the same.

**Request rules**
- `start` and `finish` are required, non-blank strings of at most 200 characters.
- Only US place names are accepted. Coordinate pairs such as `"40.71,-74.00"`, numbers, null, lists and objects are rejected.
- Only JSON bodies are accepted. No authentication is required.

**How a request is handled:**
1. Geocode both places (restricted to the USA).
2. Get one `driving-car` route.
3. Find the stations within the corridor.
4. Plan the fuel stops.

### Successful response (200)

Illustrative example, generated by running the real view with a **mocked** provider: a fictional straight 1,000-mile route and fictional "Example" stations. The geometry is shortened here. A real request returns real places, stations and the full ORS route.

```json
{
  "start":  {"query": "Westville, KS", "label": "Westville, KS, USA", "lat": 39.0, "lon": -100.0},
  "finish": {"query": "Eastville, OH", "label": "Eastville, OH, USA", "lat": 39.0, "lon": -81.37652493122926},
  "route": {
    "distance_miles": "1000.000",
    "geometry": {"type": "LineString", "coordinates": [[-100.0, 39.0], [-99.90688262465615, 39.0], "...", [-81.37652493122926, 39.0]]}
  },
  "fuel_stops": [
    {
      "sequence": 1,
      "station": {"opis_id": 102, "name": "EXAMPLE STOP 102", "address": "I-70, EXIT 102", "city": "Exampletown 102",
                  "state": "KS", "approximate_location": {"lat": 39.0, "lon": -94.41295747936879}},
      "visit_index": 0,
      "route_mile": "300.000",
      "offset_from_route_miles": "0.000",
      "arrival_fuel_gallons": "20.000",
      "gallons_purchased": "30.000",
      "price_per_gallon": "3.00733333",
      "price_source_csv_line": 20,
      "listed_price_max": "3.20000000",
      "listed_price_count": 2,
      "stop_cost_rounded": "90.22",
      "stop_cost_unrounded": "90.21999990000"
    },
    {
      "sequence": 2,
      "station": {"opis_id": 103, "name": "EXAMPLE STOP 103", "address": "I-70, EXIT 103", "city": "Exampletown 103",
                  "state": "KS", "approximate_location": {"lat": 39.0, "lon": -86.96356745186048}},
      "visit_index": 0,
      "route_mile": "700.000",
      "offset_from_route_miles": "0.000",
      "arrival_fuel_gallons": "10.000",
      "gallons_purchased": "20.000",
      "price_per_gallon": "3.19900000",
      "price_source_csv_line": 30,
      "listed_price_max": "3.19900000",
      "listed_price_count": 1,
      "stop_cost_rounded": "63.98",
      "stop_cost_unrounded": "63.98000000000"
    }
  ],
  "fuel_summary": {
    "tank_capacity_gallons": "50",
    "miles_per_gallon": "10",
    "starting_fuel_gallons": "50",
    "fuel_consumed_gallons": "100.000",
    "fuel_purchased_gallons": "50.000",
    "ending_fuel_gallons": "0.000",
    "total_cost_usd": "154.20",
    "total_cost_unrounded_usd": "154.19999990000",
    "currency": "USD"
  },
  "candidate_station_visits": 3,
  "corridor_miles": 10.0,
  "assumptions": ["Station locations are approximate: ...", "... (6 entries in total)"]
}
```

In this example the vehicle passes the $3.50 station at mile 100 without stopping. It goes to the cheapest station within its first 500 miles (mile 300, $3.00733333) and fills the tank. It then buys only what it needs at mile 700 to reach the destination with 0 gallons left.

### Key response fields

**Units and formats**
- **Decimal values are strings.** Every mile, gallon, price and cost value is an exact decimal string, never a float. Coordinates (`lat`, `lon`, `geometry`) are JSON numbers.

**Route**
- `route.distance_miles` is the distance ORS reports. `route.geometry.coordinates` is in GeoJSON order, `[longitude, latitude]`.

**Stops**
- `fuel_stops` lists only stops where fuel is bought, in route order.
- `route_mile` is the station's closest approach along the route.
- `offset_from_route_miles` is how far the station's approximate point is from the route.
- `visit_index` separates repeat passes of the same station; it is 0 on the first pass.
- `arrival_fuel_gallons` is the fuel in the tank before buying.
- `price_source_csv_line` is the CSV line of the price used. `listed_price_max` and `listed_price_count` describe all of that station's listed prices.

**Costs and rounding**
- `gallons_purchased` is the exact, unrounded quantity, as are `arrival_fuel_gallons` and the other gallon fields.
- `stop_cost_unrounded` is exactly `gallons_purchased` × `price_per_gallon`. `stop_cost_rounded` rounds it to cents **for display only**. Recomputing a cost from a rounded gallon figure can be off by a cent: for example, 5.6614 gal × $2.92066666 = $16.535062… → $16.54, whereas 5.66 gal gives $16.53.
- `total_cost_usd` is the unrounded sum, rounded **once** (half up). It's the money spent on fuel bought during the trip; the free starting tank isn't counted. Displayed stop costs can differ from it by a cent or so.

**Fuel totals**
- `fuel_consumed_gallons` = distance ÷ 10.
- `ending_fuel_gallons` = 50 + purchased − consumed.

**Other**
- `candidate_station_visits` is how many station passes were within the corridor.
- `assumptions` repeats the main limitations in every response.

### Errors

Every error has the same shape:

```json
{"error": {"code": "invalid_request", "message": "The request is invalid.",
           "details": {"fields": {"start": ["This field may not be blank."],
                                  "finish": ["Must be a place name string, e.g. \"Boston, MA\"."]}}}}
```

| Status | `code` | When |
|---|---|---|
| 400 | `invalid_request` | Missing, blank, wrong-type, too-long or coordinate input; malformed JSON; body is not a JSON object |
| 400 | `invalid_location` | A place was not found, is ambiguous, is outside the USA, or is a whole state or county (`details.field`, `details.reason`) |
| 400 | `same_location` | Start and finish resolve to the same point |
| 405 / 415 | `method_not_allowed` / `unsupported_media_type` | Not a POST / not JSON |
| 422 | `route_not_found` | ORS found no drivable route (codes 2009, 2010, 2016, or a zero-length route) |
| 422 | `route_exceeds_provider_limits` | ORS route limits were exceeded (codes 2004, 2017); this is not a fuel-plan result |
| 422 | `no_feasible_fuel_plan` | A route exists, but no sequence of candidate stations keeps every gap within 500 miles. `details` gives the gap and the corridor. |
| 500 | `server_misconfigured` | `ORS_API_KEY` is not set |
| 500 | `internal_error` | Unexpected error (logged; no details returned) |
| 502 | `provider_auth_error` / `provider_bad_response` / `provider_error` | ORS rejected the credentials, returned an unusable response, or rejected the request this service built (for example, a geocoder HTTP 400: user input that Pelias would reject is already refused locally with a 400) |
| 503 | `provider_rate_limited` | ORS rate limit (429) or confirmed daily quota (403 "Quota exceeded"); `details.quota_exhausted` |
| 503 | `provider_unavailable` | Connection failure or ORS 5xx |
| 504 | `provider_timeout` | ORS timed out (5 s connect, 30 s read) |

Example `422 no_feasible_fuel_plan`, from the same mocked route with only the mile-100 station left:

```json
{"error": {"code": "no_feasible_fuel_plan",
  "message": "No sequence of candidate fuel stations keeps the vehicle within its 500-mile range on this route.",
  "details": {"reason": "no fuel station within 500 miles after mile 100.000", "gap_start_mile": "100.000",
              "gap_end_mile": "1000.000", "max_range_miles": "500", "route_distance_miles": "1000.000",
              "corridor_miles": 10.0, "candidate_station_visits": 1,
              "note": "Candidates depend on approximate station locations and the corridor width."}}}
```

Provider error details are logged for operators, never returned to clients. Responses never include the API key or stack traces.

### External calls and caching

**Calls per request**
- **First request for a pair:** at most 3 calls (2 geocodes + 1 directions).
- **Identical repeat:** 0 calls.

**What is cached**
- Successful, validated geocoding and directions results are cached for 7 days in Django's file-based cache at `cache/django/` (git-ignored).
- Errors, timeouts and malformed responses are never cached.
- Station matching and the fuel plan are recomputed on every request; only the provider responses are cached.

---

## 5. Algorithm

### Vehicle model
- 50-gallon tank, 10 mpg, 500-mile full-tank range.
- Starts with a full tank whose fuel is free; nothing can be bought at the origin.

### Finding candidate stations (`routing/geo.py`)
- **Route miles:** the route's segment lengths are scaled so the last point equals the ORS distance.
- **Corridor test:** a station is a candidate when its approximate point is within `FUEL_CORRIDOR_MILES` of the route. The distance is measured in a local flat projection centred on the station.
- **Visits:** each continuous pass through a station's corridor is one *visit*, placed at its closest approach. If the route leaves the corridor and comes back, that's a separate visit; the station's price records are not duplicated.
- **Prefilter:** a bounding-box check, proven never to discard a valid candidate, makes this fast.

### Choosing stops (`routing/optimizer.py`)
- **At the origin:**
  - If the destination is within 500 miles, finish with no stops.
  - Otherwise, go to the **cheapest** station within the first 500 miles; ties go to the nearest, then station ID, then visit index. Stations before it are passed.
- **At each station:**
  - If a **strictly cheaper** station is within 500 miles, buy just enough to reach the nearest one.
  - Otherwise, if the destination is within 500 miles, buy just enough to finish.
  - Otherwise, **fill the tank** and go to the cheapest station within 500 miles.
- **No feasible plan:** if some gap between consecutive candidates, or to the destination, is longer than 500 miles, there is no feasible plan (422).
- **Arithmetic:** route miles enter `Decimal` once, rounded to 0.001 mile. That's numerical precision, not real-world accuracy. Fuel, prices and costs are all `Decimal`.

### When the plan is optimal
The plan is the cheapest **under this model**:
- one fixed route;
- the candidate stations and their lowest listed prices;
- linear consumption;
- fuel can be bought in any amount;
- no per-stop fees and no detour costs.

**How this is checked:** in the tests, an independent exact dynamic-programming solver is compared with the optimizer on 2,000 seeded random cases, 500 of them aimed at the origin rule. Exact cost and feasibility match in every case. The solver itself is checked against brute force and against finer fuel steps.

The plan is **not** a claim about the cheapest real-world trip.

---

## 6. Data quality and limitations

**Station data**
- **Approximate locations:** each station sits at the Census point of its city, town or township, which can be several miles from the actual truckstop. Mile markers and corridor membership are therefore approximate. A station can be wrongly included or excluded, and a route can be reported as infeasible when it isn't.
- **Prices:** these come from the supplied dataset, not live pump prices. The lowest listed price is used, so the cost may be understated if a station's multiple listed prices weren't all available. The highest listed price is shown for comparison.
- **Unlocated stations:** 201 US stations (3%) couldn't be located without guessing and are never used.

**Route and corridor**
- **Corridor eligibility:** results depend on `FUEL_CORRIDOR_MILES`. A wider corridor finds more candidates but allows larger, uncounted detours.
- **No detour cost:** fuel used to leave the route and reach a station isn't counted. Plans also arrive with exactly the fuel needed, with no reserve.

**Provider and caching**
- **Provider dependency:** geocoding and routing need OpenRouteService on `api.heigit.org` and a valid key, with daily quotas. Geocoding accepts a place only if the top result is a city-level or finer place in the USA. A name shared by several places, such as "Springfield", may be rejected as ambiguous, depending on the geocoder's ranking. Adding a state avoids that.
- **Cache:** the file-based cache is local to one machine; delete `cache/django/` to clear it.
- **Live verification:** only the success path has been verified against the real service; see [OpenRouteService integration verification](#openrouteservice-integration-verification) below.
- **Response size:** the full ORS geometry is returned. Observed: the live Chicago → Denver response was 119 KB for 1,000.4 miles (5,048 points), roughly 119 KB per 1,000 miles. This is one measurement, not a guarantee; size depends on how many points ORS returns.
- **Public endpoint, no throttling:** the API has no authentication or rate limiting, which is fine for a local assessment. If deployed publicly, anyone could use up the shared ORS daily quota. Caching only helps for repeated identical trips, and a per-IP throttle alone wouldn't prevent distributed use. A deployment should add authentication or an API gateway quota.

### OpenRouteService integration verification

**Live success path verified on October 14, 2026.** A real request to `POST /api/route/` for Chicago, IL → Denver, CO returned HTTP 200 in approximately 4.2 seconds using the `api.heigit.org` geocoding and directions endpoints. The response contained route geometry, eligible fuel-station visits, an optimized fuel plan, and cost accounting.

The verified route was approximately 1,000.4 miles, with 5,048 geometry points and 202 station visits within the configured 10-mile corridor. The returned plan selected two fuel stops, with a reported total fuel expenditure of $140.76.

This verifies the normal successful integration path for this request. It does **not** independently verify provider error responses, quota-exhaustion detection, or every supported route and location. Those behaviors remain covered by mocked tests and documented assumptions rather than live failure scenarios. In particular:
- Error codes follow the official ORS/Pelias documentation.
- Quota detection relies on the Tyk gateway's documented "Quota exceeded" message.
- The actual 403 and quota bodies from `api.heigit.org`, and the HTTP status that comes with each routing error code, have not been observed.

Station coordinates are approximate Census Gazetteer locations, not verified pump locations. Actual access roads, detours, and fuel costs may differ from the model's results.

---

## 7. Testing and verification

All tests use Django's test runner. Every provider call is mocked, and live HTTP is blocked inside the tests.

```bash
python manage.py test routing               # 103 tests
python manage.py check
python manage.py makemigrations --check --dry-run
```

No extra test packages are needed; the suite runs on Django's built-in runner.

**What the tests cover**
- **Import:** validation, transaction rollback, County Subdivisions matching rules.
- **Geometry:** projection, corridor visits, the V-shape, loops and tangent cases, the prefilter against a brute-force reference, and approximation error against a great-circle oracle.
- **Optimizer:** the reference-solver comparisons described in section 5, invariants, boundary cases and `Decimal` rounding.
- **ORS client:** request format, validation, caching, and error and quota mapping.
- **API:** schema, `Decimal` strings, every error mapping (including a geocoder HTTP 400), cache reuse, and both URL forms.
- **Settings:** `FUEL_CORRIDOR_MILES` validation (including a startup check in a subprocess) and `.env` parsing.

### Benchmark (synthetic only)

```bash
python scripts/benchmark_corridor.py            # or: python scripts/benchmark_corridor.py route.json
```

**Setup:** a **synthetic** Los Angeles → New York stress route — not a real road route — with 25,182 points spaced about 0.1 mile apart. It runs against the 6,425 imported located stations.

**Result:** corridor matching took **1.72–1.89 s** on the development machine, and a prefilter check on 100 points found 0 mismatches.

**Not yet measured:** a real ORS route. Save an ORS response as `{"coordinates": [[lon, lat], ...], "distance_mi": ...}` and pass its file to the script to measure one.

---

## 8. Demonstration with Postman

No Postman collection is included; create the requests by hand.

1. **Start the server:** with a valid `ORS_API_KEY` in `.env` and the data imported, run `python manage.py runserver`.
2. **Normal request:**
   - Method `POST`, URL `http://127.0.0.1:8000/api/route/`.
   - Header `Content-Type: application/json`.
   - Body (raw, JSON): `{"start": "Chicago, IL", "finish": "Denver, CO"}`.
   - Expect `200`. Show `route.distance_miles`, the `fuel_stops` list (route miles, gallons, prices) and `fuel_summary.total_cost_usd`. Paste `route.geometry` into a GeoJSON viewer to see the route.
3. **Cache:** send the same request again. It should return faster, because the geocodes and route come from the cache.
4. **Validation error:** body `{"start": "", "finish": 40.7}` returns `400 invalid_request`, with per-field messages under `error.details.fields`.
5. **Location error:** `{"start": "Springfield", "finish": "Denver, CO"}` should return `400 invalid_location` with reason `ambiguous`, if the geocoder returns several Springfields of similar confidence. Adding a state fixes it.
6. **Infeasible plan:** set `FUEL_CORRIDOR_MILES=0.5` in `.env` and restart the server. On a long route, few stations fall inside such a narrow corridor, so a `422 no_feasible_fuel_plan` with the gap miles in `details` is likely. Restore `10` afterwards.

Steps 2, 5 and 6 depend on live ORS results, so the exact stops and errors can't be guaranteed here.
