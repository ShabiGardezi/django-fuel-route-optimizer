# Source data (not committed)

Everything in this directory except this file is ignored by Git. Place these files here:

| File | Source |
|---|---|
| `fuel-prices-for-be-assessment.csv` | Supplied with the assessment |
| `2026_Gaz_place_national.txt` | US Census Gazetteer, Places: https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2026_Gazetteer/2026_Gaz_place_national.zip (unzip) |
| `2026_Gaz_cousubs_national.txt` | US Census Gazetteer, County Subdivisions: https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2026_Gazetteer/2026_Gaz_cousubs_national.zip (unzip) |

Then run:

```
python manage.py import_fuel_stations --cousub data/2026_Gaz_cousubs_national.txt
```
