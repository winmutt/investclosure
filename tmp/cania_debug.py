#!/usr/bin/env python3
import json
import curl_cffi.requests as curl

CANIA_API_URL = 'https://kanialawfirm.com/wp-json/kania-api/v1/foreclosure-listings'
session = curl.Session(impersonate='chrome131')
resp = session.get(CANIA_API_URL, timeout=30)
data = resp.json()
print(type(data), len(data) if isinstance(data, (list, dict)) else 'n/a')
if isinstance(data, dict):
    print(list(data.keys())[:10])
    for k, v in list(data.items())[:3]:
        print(f'  {k}: {type(v).__name__} {str(v)[:200]}...')
elif isinstance(data, list):
    print(f'First item type: {type(data[0]).__name__}')
    if isinstance(data[0], dict):
        print(list(data[0].keys())[:10])
