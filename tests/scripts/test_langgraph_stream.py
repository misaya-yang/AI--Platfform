import json
import sys
import time
from typing import Iterator

url = 'http://127.0.0.1:2024/runs/stream'
assistant_id = 'a1ea47f8-00de-5254-bef7-27dcc88be0be'
payload = {
    'assistant_id': assistant_id,
    'input': {'messages': [{'role': 'user', 'content': 'hello'}]},
    'stream_mode': ['messages', 'updates'],
}
headers = {'Authorization': 'Bearer gateway-debug'}

try:
    import httpx
except Exception as e:
    print('httpx import failed:', e)
    httpx = None

if httpx:
    with httpx.Client(timeout=10.0) as client:
        try:
            with client.stream('POST', url, headers=headers, json=payload) as resp:
                print('status', resp.status_code)
                print('headers', resp.headers.get('content-type'))
                # read a few lines
                for i, line in enumerate(resp.iter_lines()):
                    if line:
                        print('line', line)
                    if i > 20:
                        break
        except Exception as e:
            print('stream error', repr(e))
else:
    import urllib.request
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={**headers, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print('status', resp.status)
            print('headers', resp.headers.get('content-type'))
            print(resp.read(200))
    except Exception as e:
        print('error', e)
