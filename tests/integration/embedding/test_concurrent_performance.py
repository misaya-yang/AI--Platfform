#!/usr/bin/env python3
"""测试并发embedding优化的性能提升"""

import time

import requests

API_BASE = "http://localhost:8080"
EMAIL = "admin@hejazfs.com.au"
PASSWORD = "123456.dc"
DATASET_ID = "kb_3161be800a7d"
PDF_PATH = "/Users/misaya.yanghejazfs.com.au/AI-Imam-pdf/english_Tawheed_Made_Easy.pdf"

print("=" * 80)
print("🚀 并发Embedding性能测试")
print("=" * 80)

# 1. 登录
resp = requests.post(f"{API_BASE}/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
if resp.status_code != 200:
    print("❌ 登录失败")
    exit(1)
token = resp.json().get("access_token")
headers = {"Authorization": f"Bearer {token}"}
print("✅ 登录成功")

# 2. 删除旧文档
old_docs = ["998617ac-c0b5-48a9-bc30-cc975f423b4c", "5b37642e-2551-41be-9e02-1b0b1b11a96e"]
for doc_id in old_docs:
    requests.delete(f"{API_BASE}/api/v1/knowledge/{DATASET_ID}/documents/{doc_id}", headers=headers)
print("🗑️ 清理旧文档")
time.sleep(2)

# 3. 上传并计时
print(f"\n📤 上传PDF: {PDF_PATH.split('/')[-1]}")
print("-" * 80)

upload_start = time.time()
with open(PDF_PATH, "rb") as f:
    files = {"file": (PDF_PATH.split("/")[-1], f, "application/pdf")}
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
upload_time = time.time() - upload_start
print(f"✅ 上传成功（耗时: {upload_time:.2f}秒）")
print(f"   文档ID: {doc_id}")

# 4. 监控处理（最多5分钟）
print("\n🔄 监控Embedding处理...")
print("-" * 80)
print(f"{'时间':>6s} | {'状态':^12s} | {'进度':>8s} | {'速度':>12s} | {'预计剩余':>10s}")
print("-" * 80)

processing_start = time.time()
last_progress = 0
progress_history = []

for _i in range(150):  # 5分钟 = 150 * 2秒
    time.sleep(2)

    resp = requests.get(
        f"{API_BASE}/api/v1/knowledge/{DATASET_ID}/documents/{doc_id}", headers=headers, timeout=10
    )

    if resp.status_code == 200:
        data = resp.json()
        status = data.get("status")
        progress = data.get("progress", 0)
        error = data.get("error")

        if progress != last_progress:
            elapsed = time.time() - processing_start
            speed = progress / elapsed * 60 if elapsed > 0 else 0
            remaining = (100 - progress) / speed if speed > 0 else 999

            progress_history.append((elapsed, progress, speed))

            print(
                f"{elapsed:6.0f}s | {status:^12s} | {progress:7.2f}% | {speed:9.1f}%/分 | {remaining:7.1f}分"
            )
            last_progress = progress

        if status == "completed":
            total_time = time.time() - processing_start
            print("-" * 80)
            print("✅ 处理完成！")
            print(f"   总耗时: {total_time:.2f}秒 ({total_time / 60:.2f}分钟)")

            # 计算平均速度
            if progress_history:
                avg_speed = sum(s for _, _, s in progress_history) / len(progress_history)
                print(f"   平均速度: {avg_speed:.1f}%/分钟")
            break
        elif status == "failed":
            print(f"\n❌ 处理失败: {error}")
            exit(1)
else:
    print("\n⏰ 监控超时（5分钟）")

# 5. 检查切片数量
resp = requests.get(
    f"{API_BASE}/api/v1/knowledge/{DATASET_ID}/segments",
    params={"document_id": doc_id},
    headers=headers,
)

if resp.status_code == 200:
    segments = resp.json()
    print("\n📊 切片统计:")
    print(f"   总数: {len(segments)}")

    if segments:
        text_segs = [s for s in segments if s.get("content_type") != "image"]
        image_segs = [s for s in segments if s.get("content_type") == "image"]
        print(f"   文本: {len(text_segs)}, 图片: {len(image_segs)}")

        avg_tokens = (
            sum(s.get("token_count", 0) for s in text_segs) / len(text_segs) if text_segs else 0
        )
        print(f"   平均Token数: {avg_tokens:.0f}")

print("\n" + "=" * 80)
print("📈 性能对比:")
print("   优化前: ~25%/分钟 (预计需要4分钟)")
print(f"   优化后: {avg_speed if 'avg_speed' in dir() else '测试中'}%/分钟")
if "avg_speed" in dir() and avg_speed > 25:
    improvement = (avg_speed / 25 - 1) * 100
    print(f"   性能提升: {improvement:.0f}%")
print("=" * 80)
