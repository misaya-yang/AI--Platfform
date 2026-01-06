import json
import sys

url = 'http://127.0.0.1:2024/runs/stream'
assistant_id = 'a1ea47f8-00de-5254-bef7-27dcc88be0be'
payload = {
    'assistant_id': assistant_id,
    'input': {'messages': [{'role': 'user', 'content': 'hello'}]},
    'stream_mode': ['messages-tuple', 'updates'],
}
headers = {'Authorization': 'Bearer gateway-debug'}

try:
    import httpx
except Exception as e:
    print('httpx import failed:', e)
    httpx = None

if httpx:
    with httpx.Client(timeout=10.0) as client:
        with client.stream('POST', url, headers=headers, json=payload) as resp:
            print('status', resp.status_code)
            print('headers', resp.headers.get('content-type'))
            for i, line in enumerate(resp.iter_lines()):
                if line:
                    print('line', line)
                if i > 40:
                    break
else:
    import urllib.request
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={**headers, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as resp:
        print('status', resp.status)
        print(resp.read(200))
