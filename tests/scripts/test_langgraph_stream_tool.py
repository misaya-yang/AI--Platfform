# -*- coding: utf-8 -*-
import json

url = 'http://127.0.0.1:2024/runs/stream'
assistant_id = 'a1ea47f8-00de-5254-bef7-27dcc88be0be'
payload = {
    'assistant_id': assistant_id,
    'input': {'messages': [{'role': 'user', 'content': '介绍Hejaz产品并给出来源'}]},
    'stream_mode': ['messages', 'updates'],
}
headers = {'Authorization': 'Bearer gateway-debug'}

import httpx
with httpx.Client(timeout=20.0) as client:
    with client.stream('POST', url, headers=headers, json=payload) as resp:
        print('status', resp.status_code)
        print('headers', resp.headers.get('content-type'))
        for i, line in enumerate(resp.iter_lines()):
            if line:
                print('line', line)
            if i > 200:
                break
