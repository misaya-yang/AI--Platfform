#!/usr/bin/env python3
"""
测试PDF上传和处理流程
"""
import requests
import time
import json
from pathlib import Path

# 配置
API_BASE = "http://localhost:8080"
PDF_PATH = "/Users/misaya.yanghejazfs.com.au/AI-Imam-pdf/english_Tawheed_Made_Easy.pdf"
DATASET_ID = "kb_3161be800a7d"  # 从日志中获取
EMAIL = "admin@hejazfs.com.au"
PASSWORD = "123456.dc"  # 实际密码

def login():
    """登录获取token"""
    print("🔐 正在登录...")
    resp = requests.post(
        f"{API_BASE}/api/v1/auth/login",
        json={"email": EMAIL, "password": PASSWORD}
    )
    if resp.status_code != 200:
        print(f"❌ 登录失败: {resp.status_code} {resp.text}")
        return None
    
    data = resp.json()
    token = data.get("access_token")
    print(f"✅ 登录成功，获取token: {token[:20]}...")
    return token

def upload_pdf(token):
    """上传PDF文件"""
    print(f"\n📤 正在上传PDF: {PDF_PATH}")
    
    pdf_file = Path(PDF_PATH)
    if not pdf_file.exists():
        print(f"❌ 文件不存在: {PDF_PATH}")
        return None
    
    print(f"📄 文件大小: {pdf_file.stat().st_size / 1024 / 1024:.2f}MB")
    
    with open(pdf_file, "rb") as f:
        files = {
            "file": (pdf_file.name, f, "application/pdf")
        }
        headers = {
            "Authorization": f"Bearer {token}"
        }
        
        resp = requests.post(
            f"{API_BASE}/api/v1/knowledge/{DATASET_ID}/documents/upload",
            files=files,
            headers=headers,
            timeout=60
        )
    
    if resp.status_code != 200:
        print(f"❌ 上传失败: {resp.status_code}")
        print(f"响应: {resp.text}")
        return None
    
    data = resp.json()
    doc_id = data.get("document_id")
    print(f"✅ 上传成功，文档ID: {doc_id}")
    print(f"📊 文档信息: {json.dumps(data, indent=2, ensure_ascii=False)}")
    return doc_id

def check_document_status(token, doc_id, max_wait=120):
    """检查文档处理状态"""
    print(f"\n🔍 监控文档处理状态 (最多等待{max_wait}秒)...")
    
    headers = {"Authorization": f"Bearer {token}"}
    start_time = time.time()
    last_status = None
    
    while time.time() - start_time < max_wait:
        try:
            resp = requests.get(
                f"{API_BASE}/api/v1/knowledge/{DATASET_ID}/documents/{doc_id}",
                headers=headers,
                timeout=10
            )
            
            if resp.status_code != 200:
                print(f"❌ 获取文档状态失败: {resp.status_code}")
                break
            
            data = resp.json()
            status = data.get("status")
            progress = data.get("progress", 0)
            error = data.get("error")
            
            if status != last_status:
                print(f"📍 状态变化: {status} (进度: {progress}%)")
                if error:
                    print(f"⚠️ 错误信息: {error}")
                last_status = status
            
            # 完成状态
            if status == "completed":
                print(f"✅ 处理完成！")
                print(f"📊 最终状态: {json.dumps(data, indent=2, ensure_ascii=False)}")
                return True
            
            # 失败状态
            if status == "failed":
                print(f"❌ 处理失败！")
                print(f"错误信息: {error}")
                print(f"📊 完整信息: {json.dumps(data, indent=2, ensure_ascii=False)}")
                return False
            
            time.sleep(2)
            
        except Exception as e:
            print(f"⚠️ 检查状态时出错: {e}")
            time.sleep(2)
    
    print(f"⏰ 超时！等待超过{max_wait}秒")
    return False

def check_segments(token, doc_id):
    """检查生成的切片"""
    print(f"\n📑 检查文档切片...")
    
    headers = {"Authorization": f"Bearer {token}"}
    
    try:
        resp = requests.get(
            f"{API_BASE}/api/v1/knowledge/{DATASET_ID}/segments",
            params={"document_id": doc_id},
            headers=headers,
            timeout=10
        )
        
        if resp.status_code != 200:
            print(f"❌ 获取切片失败: {resp.status_code}")
            return
        
        segments = resp.json()
        print(f"✅ 共生成 {len(segments)} 个切片")
        
        if segments:
            print(f"\n📝 前3个切片预览:")
            for i, seg in enumerate(segments[:3], 1):
                text = seg.get("content", "")[:100]
                token_count = seg.get("token_count", 0)
                print(f"\n切片 {i}:")
                print(f"  Token数: {token_count}")
                print(f"  内容: {text}...")
                
                # 检查是否有图片
                if seg.get("content_type") == "image":
                    print(f"  🖼️ 图片切片: {seg.get('image_url')}")
                    print(f"  描述: {seg.get('vlm_description', '')[:100]}")
        
    except Exception as e:
        print(f"⚠️ 检查切片时出错: {e}")

def main():
    print("=" * 60)
    print("🧪 PDF上传和处理测试")
    print("=" * 60)
    
    # 1. 登录
    token = login()
    if not token:
        print("\n❌ 测试失败：无法登录")
        return
    
    # 2. 上传PDF
    doc_id = upload_pdf(token)
    if not doc_id:
        print("\n❌ 测试失败：无法上传文件")
        return
    
    # 3. 监控处理状态
    success = check_document_status(token, doc_id, max_wait=180)
    
    # 4. 检查切片
    if success:
        check_segments(token, doc_id)
    
    print("\n" + "=" * 60)
    if success:
        print("✅ 测试完成：PDF处理成功")
    else:
        print("❌ 测试完成：PDF处理失败")
    print("=" * 60)

if __name__ == "__main__":
    main()
