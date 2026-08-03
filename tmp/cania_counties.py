#!/usr/bin/env python3
"""Analyze raw Kania API data by county — run in container."""
import json
import curl_cffi.requests as curl

CANIA_API_URL = (
    "https://kanialawfirm.com/wp-admin/admin-ajax.php"
    "?action=wp_ajax_ninja_tables_public_action"
    "&table_id=216745"
    "&target_action=get-all-data"
    "&skip_rows=0"
    "&limit_rows=0"
    "&default_sorting=old_first"
)
session = curl.Session(impersonate='chrome131')
session.headers.update({
    "Accept": "application/json",
})
resp = session.get(CANIA_API_URL, timeout=30)
resp.raise_for_status()
data = resp.json()
if isinstance(data, list):
    records = data
elif isinstance(data, dict):
    records = data.get('data', [])
else:
    print(f"Unexpected response type: {type(data)}")
    exit(1)

print(f"Total records: {len(records)}")

# All 21 qualifying mountain counties
QUALIFYING = {
    'alleghany', 'ashe', 'avery', 'buncombe', 'burke', 'caldwell', 'cherokee',
    'clay', 'graham', 'haywood', 'henderson', 'jackson', 'madison', 'mcdowell',
    'mitchell', 'polk', 'macon', 'swain', 'transylvania', 'watauga', 'yancey',
}

counties_raw = {}
counties_in = {}
for rec in records:
    if not isinstance(rec, dict):
        continue
    val = rec.get('value', {})
    if not isinstance(val, dict):
        continue
    county = (val.get('county') or '').strip().lower()
    if county:
        counties_raw[county] = counties_raw.get(county, 0) + 1
        if county in QUALIFYING:
            counties_in[county] = counties_in.get(county, 0) + 1

print("\n=== All counties in Kania API ===")
for c in sorted(counties_raw.keys()):
    marker = " ✅" if c in QUALIFYING else ""
    print(f"  {c}: {counties_raw[c]}{marker}")

print("\n=== Qualifying counties: COUNTED on Kania ===")
for c in sorted(set(QUALIFYING) & set(counties_raw.keys())):
    print(f"  {c}: {counties_in[c]}")

print("\n=== Qualifying counties: MISSED by Kania ===")
missed = sorted(QUALIFYING - set(counties_raw.keys()))
for c in missed:
    print(f"  {c}")

print(f"\nKania coverage: {len(set(QUALIFYING) & set(counties_raw.keys()))}/{len(QUALIFYING)} qualifying counties")
