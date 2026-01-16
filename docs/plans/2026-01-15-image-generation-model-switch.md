# 智能图片生成与模型切换功能设计

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在"+"菜单添加智能图片生成功能，根据当前模型自动路由到对应的图片生成后端

**Architecture:** 前端添加图片生成模式，后端根据 model provider 智能路由到 DashScope 或 Gemini Native Image

**Tech Stack:** React, FastAPI, Google Gemini API, DashScope API

---

## 功能设计

### 1. 智能图片生成

**交互流程：**
1. 点击"+"号菜单
2. 选择"生成图片"
3. 输入框进入图片生成模式（显示提示）
4. 用户输入 prompt，点击发送
5. 根据当前选择的模型 provider 智能路由：
   - DashScope 模型 → 阿里云 Wanx
   - Google 模型 → Gemini Native Image
   - 其他 → 回退到 DashScope

**后端路由表：**
```
Provider     | Image Generation Backend
-------------|---------------------------
dashscope    | DashScope Wanx API
google       | Gemini Native Image API
openai       | DashScope (fallback)
anthropic    | DashScope (fallback)
deepseek     | DashScope (fallback)
```

### 2. 无缝模型切换

**已实现**：切换模型时保持对话历史，无需额外开发。

---

## 实现任务

### Task 1: 创建 Gemini Native Image 服务

**Files:**
- Create: `src/services/assistant/tools/gemini_image_tool.py`

**实现要点：**
- 使用 `gemini-2.5-flash-image` 模型
- 支持 prompt 到图片生成
- 返回 base64 图片数据

### Task 2: 更新图片生成 API 端点

**Files:**
- Modify: `src/api/v1/assistant.py`

**实现要点：**
- 添加 `POST /api/v1/assistant/generate-image`
- 接收 `prompt`, `model_id` 参数
- 根据 model_id 获取 provider，智能路由

### Task 3: 前端 - 更新 QuickActionsMenu

**Files:**
- Modify: `web/src/pages/assistant/components/QuickActionsMenu.tsx`

**实现要点：**
- 添加"生成图片"菜单项（Palette 图标）
- 点击后调用 `onImageGenerate` 回调

### Task 4: 前端 - 添加图片生成模式

**Files:**
- Modify: `web/src/pages/assistant/index.tsx`
- Modify: `web/src/api/assistant.ts`

**实现要点：**
- 添加 `isImageMode` 状态
- 图片模式下输入框显示不同 placeholder
- 发送时调用图片生成 API
- 添加 `generateImage` API 函数

---

## API 设计

### POST /api/v1/assistant/generate-image

**Request:**
```json
{
  "prompt": "画一只可爱的猫",
  "model_id": "qwen-turbo",  // 用于确定 provider
  "style": "default"  // 可选
}
```

**Response:**
```json
{
  "success": true,
  "images": [
    {
      "url": "data:image/png;base64,...",
      "width": 1024,
      "height": 1024
    }
  ],
  "provider": "dashscope",
  "duration_ms": 5000
}
```
