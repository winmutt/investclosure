#!/usr/bin/env python3
"""Check Clay/Polk records — what fields populate?"""
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
session.headers.update({"Accept": "application/json"})
resp = session.get(CANIA_API_URL, timeout=30)
data = resp.json()
records = data if isinstance(data, list) else data.get('data', [])

# Find Clay and Polk county records
for rec in records:
    if not isinstance(rec, dict):
        continue
    val = rec.get('value', {})
    if not isinstance(val, dict):
        continue
    county = (val.get('county') or '').strip().lower()
    if county in ('clay', 'polk'):
        print(f"\n=== {val.get('courtfile','?')} | {val.get('county','?')} ===")
        for k in sorted(val.keys()):
            if val[k]:
                v = val[k]
                if isinstance(v, str) and len(v) > 80:
                    v = v[:80] + "..."
                print(f"  {k}: {v}")
            else:
                print(f"  {k}: (empty)")
