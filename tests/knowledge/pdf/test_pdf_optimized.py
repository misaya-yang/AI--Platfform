#!/usr/bin/env python3
"""测试优化后的PDF处理"""

import time

import requests

API_BASE = "http://localhost:8080"
EMAIL = "admin@hejazfs.com.au"
PASSWORD = "123456.dc"
DATASET_ID = "kb_3161be800a7d"
PDF_PATH = "/Users/misaya.yanghejazfs.com.au/AI-Imam-pdf/english_Tawheed_Made_Easy.pdf"

print("=" * 70)
print("测试优化后的PDF处理（加速批处理）")
print("=" * 70)

# 1. 登录
resp = requests.post(f"{API_BASE}/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
if resp.status_code != 200:
    print(f"❌ 登录失败: {resp.text}")
    exit(1)
token = resp.json().get("access_token")
print("✅ 登录成功")

headers = {"Authorization": f"Bearer {token}"}

# 2. 删除旧文档（如果存在）
doc_id_old = "5b37642e-2551-41be-9e02-1b0b1b11a96e"
resp = requests.delete(
    f"{API_BASE}/api/v1/knowledge/{DATASET_ID}/documents/{doc_id_old}", headers=headers
)
if resp.status_code == 200:
    print(f"✅ 删除旧文档: {doc_id_old}")
else:
    print("ℹ️ 旧文档可能不存在")

time.sleep(2)

# 3. 上传新文档
print(f"\n📤 上传PDF: {PDF_PATH}")
start_time = time.time()

with open(PDF_PATH, "rb") as f:
    files = {"file": ("english_Tawheed_Made_Easy.pdf", f, "application/pdf")}
    resp = requests.post(
        f"{API_BASE}/api/v1/knowledge/{DATASET_ID}/documents/upload",
        files=files,
        headers=headers,
        timeout=60,
    )

if resp.status_code != 200:
    print(f"❌ 上传失败: {resp.text}")
    exit(1)

data = resp.json()
doc_id = data.get("document_id")
print(f"✅ 上传成功，文档ID: {doc_id}")
print(f"⏱️ 上传耗时: {time.time() - start_time:.2f}秒")

# 4. 监控处理（最多3分钟）
print("\n🔄 监控处理进度...")
last_progress = 0
processing_start = time.time()

for _i in range(90):  # 3分钟 = 90 * 2秒
    time.sleep(2)
    resp = requests.get(
        f"{API_BASE}/api/v1/knowledge/{DATASET_ID}/documents/{doc_id}", headers=headers
    )

    if resp.status_code == 200:
        data = resp.json()
        status = data.get("status")
        progress = data.get("progress", 0)
        error = data.get("error")

        # 只在进度变化时打印
        if progress != last_progress:
            elapsed = time.time() - processing_start
            speed = progress / elapsed * 60 if elapsed > 0 else 0
            print(
                f"[{elapsed:.0f}s] 状态: {status:12s} | 进度: {progress:6.2f}% | 速度: {speed:.1f}%/分钟"
            )
            last_progress = progress

        if status == "completed":
            total_time = time.time() - processing_start
            print(f"\n✅ 处理完成！总耗时: {total_time:.2f}秒")
            break
        elif status == "failed":
            print(f"\n❌ 处理失败: {error}")
            exit(1)
else:
    print("\n⏰ 监控超时（3分钟）")

# 5. 检查切片
resp = requests.get(
    f"{API_BASE}/api/v1/knowledge/{DATASET_ID}/segments",
    params={"document_id": doc_id},
    headers=headers,
)

if resp.status_code == 200:
    segments = resp.json()
    print("\n📊 切片统计:")
    print(f"  总数: {len(segments)}")

    if segments:
        # 统计类型
        text_segs = [s for s in segments if s.get("content_type") != "image"]
        image_segs = [s for s in segments if s.get("content_type") == "image"]
        print(f"  文本切片: {len(text_segs)}")
        print(f"  图片切片: {len(image_segs)}")

        # 显示第一个切片
        seg = segments[0]
        print("\n📝 第一个切片:")
        print(f"  类型: {seg.get('content_type', 'text')}")
        print(f"  Token数: {seg.get('token_count')}")
        if seg.get("content_type") == "image":
            print(f"  描述: {seg.get('vlm_description', '')[:100]}...")
        else:
            print(f"  内容: {seg.get('content', '')[:100]}...")

print("\n" + "=" * 70)
print("测试完成")
print("=" * 70)
