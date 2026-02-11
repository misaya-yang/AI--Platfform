#!/usr/bin/env python3
"""简化的PDF测试 - 分步骤"""

import time

import requests

API_BASE = "http://localhost:8080"
EMAIL = "admin@hejazfs.com.au"
PASSWORD = "123456.dc"
DATASET_ID = "kb_3161be800a7d"
PDF_PATH = "/Users/misaya.yanghejazfs.com.au/AI-Imam-pdf/english_Tawheed_Made_Easy.pdf"

print("=" * 60)
print("步骤1: 登录")
print("=" * 60)
resp = requests.post(f"{API_BASE}/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
print(f"状态码: {resp.status_code}")
if resp.status_code == 200:
    token = resp.json().get("access_token")
    print(f"✅ 登录成功, token前缀: {token[:30]}...")
else:
    print(f"❌ 登录失败: {resp.text}")
    exit(1)

print("\n" + "=" * 60)
print("步骤2: 上传PDF")
print("=" * 60)
with open(PDF_PATH, "rb") as f:
    files = {"file": ("english_Tawheed_Made_Easy.pdf", f, "application/pdf")}
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.post(
        f"{API_BASE}/api/v1/knowledge/{DATASET_ID}/documents/upload",
        files=files,
        headers=headers,
        timeout=60,
    )

print(f"状态码: {resp.status_code}")
if resp.status_code == 200:
    data = resp.json()
    doc_id = data.get("document_id")
    print("✅ 上传成功")
    print(f"文档ID: {doc_id}")
    print(f"状态: {data.get('status')}")
else:
    print(f"❌ 上传失败: {resp.text}")
    exit(1)

print("\n" + "=" * 60)
print("步骤3: 监控处理 (30秒)")
print("=" * 60)
for i in range(15):
    time.sleep(2)
    resp = requests.get(
        f"{API_BASE}/api/v1/knowledge/{DATASET_ID}/documents/{doc_id}", headers=headers
    )
    if resp.status_code == 200:
        data = resp.json()
        status = data.get("status")
        progress = data.get("progress", 0)
        error = data.get("error")
        print(f"[{i * 2}s] 状态: {status}, 进度: {progress}%", end="")
        if error:
            print(f", 错误: {error}")
        else:
            print()

        if status == "completed":
            print("\n✅ 处理完成!")
            break
        elif status == "failed":
            print(f"\n❌ 处理失败: {error}")
            break
    else:
        print(f"获取状态失败: {resp.status_code}")

print("\n" + "=" * 60)
print("步骤4: 检查切片")
print("=" * 60)
resp = requests.get(
    f"{API_BASE}/api/v1/knowledge/{DATASET_ID}/segments",
    params={"document_id": doc_id},
    headers=headers,
)
if resp.status_code == 200:
    segments = resp.json()
    print(f"✅ 共生成 {len(segments)} 个切片")
    if segments:
        seg = segments[0]
        print("\n第一个切片:")
        print(f"  Token数: {seg.get('token_count')}")
        print(f"  内容前100字: {seg.get('content', '')[:100]}...")
else:
    print(f"❌ 获取切片失败: {resp.status_code}")

print("\n" + "=" * 60)
print("测试完成")
print("=" * 60)
