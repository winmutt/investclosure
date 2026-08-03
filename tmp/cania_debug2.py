#!/usr/bin/env python3
"""Debug: why are some Kania properties filtered out?"""
import json
import re
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
session.headers.update({"Accept": "application/json"})
resp = session.get(CANIA_API_URL, timeout=30)
data = resp.json()
records = data if isinstance(data, list) else data.get('data', [])

QUALIFYING = {
    'alleghany', 'ashe', 'avery', 'buncombe', 'burke', 'caldwell', 'cherokee',
    'clay', 'graham', 'haywood', 'henderson', 'jackson', 'madison', 'mcdowell',
    'mitchell', 'polk', 'macon', 'swain', 'transylvania', 'watauga', 'yancey',
}

def parse_price(text):
    if not text:
        return 0
    clean = re.sub(r"[^\d.]", "", text)
    try:
        return int(float(clean) * 100)
    except (ValueError, TypeError):
        return 0

pass_count = 0
fail_counts = {'commercial': 0, 'zero_price': 0, 'no_bid': 0, 'parse_ok': 0}

for rec in records:
    if not isinstance(rec, dict):
        continue
    val = rec.get('value', {})
    if not isinstance(val, dict):
        continue
    county = (val.get('county') or '').strip().lower()
    if not county or county not in QUALIFYING:
        continue
    
    price = parse_price(val.get('openingbid') or '')
    prop_type = (val.get('propertytype') or '').strip().lower()
    
    if 'commercial' in prop_type:
        fail_counts['commercial'] += 1
        continue
    if price == 0:
        fail_counts['zero_price'] += 1
        print(f"  SKIP (price=0): {val.get('courtfile','?')} | {county} | openingbid={repr(val.get('openingbid',''))} | currentbid={repr(val.get('currentbid',''))}")
        continue
    
    if val.get('openingbid') and not str(val['openingbid']).strip():
        fail_counts['no_bid'] += 1
        continue
    
    pass_count += 1

print(f"\nPassed (would be inserted): {pass_count}")
print(f"Failed - commercial: {fail_counts['commercial']}")
print(f"Failed - zero_price: {fail_counts['zero_price']}")
print(f"Total qualifying: {pass_count + fail_counts['commercial'] + fail_counts['zero_price']}")

# Also list all openingbid values
print("\n=== Opening bid values (qualifying counties only) ===")
bid_values = {}
for rec in records:
    if not isinstance(rec, dict):
        continue
    val = rec.get('value', {})
    if not isinstance(val, dict):
        continue
    county = (val.get('county') or '').strip().lower()
    if county in bid_values and county in QUALIFYING:
        bid_values[county].append(val.get('openingbid'))
    elif county in QUALIFYING:
        bid_values[county] = [val.get('openingbid')]

for c in sorted(bid_values.keys()):
    bids = bid_values[c]
    print(f"\n  {c} ({len(bids)} records):")
    for b in bids:
        parsed = parse_price(b)
        marker = " <-- 0" if parsed == 0 else ""
        print(f"    openingbid={repr(b)} -> {parsed}{marker}")
