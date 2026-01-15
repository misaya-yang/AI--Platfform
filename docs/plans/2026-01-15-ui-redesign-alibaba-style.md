# UI 重设计方案 - 阿里云风格

## 概述

将 AI Gateway 前端从当前的"AI 生成感"设计改造为类似阿里云百炼的企业级专业风格。

**目标用户**: 内部各部门 + 对外 API 网关
**参考产品**: 阿里云百炼
**改造范围**: 完整重设计（AppLayout + Dashboard + Knowledge）

---

## 1. 设计原则

| 原则 | 说明 |
|-----|------|
| **克制** | 移除所有装饰性元素（渐变圆球、glow、多层阴影） |
| **留白** | 增大组件间距，让内容呼吸 |
| **扁平** | 卡片仅用 1px 边框，无阴影或极浅阴影 |
| **功能色** | 颜色只用于传达状态，不做装饰 |
| **静态** | 移除大部分动画，仅保留必要过渡（opacity、hover） |

---

## 2. 配色系统

### 浅色模式

```css
/* 主色（品牌）- 仅用于主按钮、激活状态 */
--color-primary: #6366F1;        /* Indigo-500 */
--color-primary-hover: #4F46E5;  /* Indigo-600 */

/* 语义色 */
--color-success: #22C55E;  /* Green-500 */
--color-warning: #F59E0B;  /* Amber-500 */
--color-error: #EF4444;    /* Red-500 */
--color-info: #3B82F6;     /* Blue-500 */

/* 中性色 */
--color-bg-page: #F9FAFB;      /* Gray-50 - 页面背景 */
--color-bg-card: #FFFFFF;      /* 卡片背景 */
--color-border: #E5E7EB;       /* Gray-200 - 边框 */
--color-border-hover: #D1D5DB; /* Gray-300 - hover 边框 */

/* 文字 */
--color-text-primary: #111827;   /* Gray-900 - 标题 */
--color-text-secondary: #6B7280; /* Gray-500 - 正文 */
--color-text-muted: #9CA3AF;     /* Gray-400 - 次要 */
```

### 深色模式

```css
--color-bg-page: #0F172A;      /* Slate-900 */
--color-bg-card: #1F2937;      /* Gray-800 */
--color-border: #374151;       /* Gray-700 */
--color-border-hover: #4B5563; /* Gray-600 */

--color-text-primary: #F9FAFB;   /* Gray-50 */
--color-text-secondary: #9CA3AF; /* Gray-400 */
--color-text-muted: #6B7280;     /* Gray-500 */
```

---

## 3. AppLayout 框架

### 3.1 侧边栏

```
┌─────────────────────────┐
│  [Logo] AI Gateway      │  ← 纯色背景，无动画
├─────────────────────────┤
│                         │
│  + 创建应用              │  ← 主按钮，唯一使用品牌色
│                         │
├─────────────────────────┤
│  ○ 总览                 │
│  ○ 服务管理              │
│  ● 知识库               │  ← 选中态：左边框 + 浅背景
│  ○ 智能对话              │
│                         │
│  MCP ──────────         │  ← 分组标题
│  ○ MCP 广场             │
│  ○ MCP 管理             │
│                         │
├─────────────────────────┤
│  ◁ 收起侧栏             │
└─────────────────────────┘
```

**改动要点:**
- Logo: 移除渐变背景和 pulse 动画，纯白底静态
- 菜单 hover: 移除 `scale + translateY`，仅背景色变化 `bg-gray-100`
- 选中态: 移除渐变背景，改为左侧 3px 品牌色边框 + `bg-indigo-50`
- 分组标题: 添加灰色小字分隔（如 MCP、组件、数据）
- 动画: 移除 Framer Motion，仅保留 CSS `transition: 150ms`

### 3.2 顶栏

- 移除渐变用户头像，改为灰色圆形 + 首字母
- 移除所有 hover 动画
- 保留：面包屑、主题切换、用户下拉

---

## 4. 基础组件

### 4.1 Card

```css
.card {
  background: var(--color-bg-card);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  box-shadow: none;
}

.card:hover {
  border-color: var(--color-border-hover);
}
```

### 4.2 Button

```css
/* Primary */
.btn-primary {
  background: var(--color-primary);
  color: white;
  border: none;
  border-radius: 6px;
  transition: background 150ms;
}
.btn-primary:hover {
  background: var(--color-primary-hover);
}

/* Secondary */
.btn-secondary {
  background: white;
  border: 1px solid var(--color-border);
  border-radius: 6px;
}
.btn-secondary:hover {
  background: #F3F4F6;
}
```

### 4.3 Input

```css
.input {
  border: 1px solid var(--color-border);
  border-radius: 6px;
  height: 36px;
  transition: border-color 150ms;
}
.input:focus {
  border-color: var(--color-primary);
  outline: none;
  box-shadow: 0 0 0 2px rgba(99, 102, 241, 0.1);
}
```

### 4.4 Badge

```css
.badge-success { background: #DCFCE7; color: #166534; }
.badge-warning { background: #FEF3C7; color: #92400E; }
.badge-error   { background: #FEE2E2; color: #991B1B; }
.badge-info    { background: #DBEAFE; color: #1E40AF; }
```

### 4.5 统一规范

| 元素 | 圆角 |
|-----|------|
| 大组件（Card、Modal） | 8px |
| 小组件（Button、Input） | 6px |
| 标签（Badge、Tag） | 4px |

---

## 5. 页面布局

### 5.1 Dashboard

```
┌─────────────────────────────────────────────────────────┐
│  总览                                                    │
├─────────────────────────────────────────────────────────┤
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐       │
│  │ 总请求  │ │ 成功率   │ │ 活跃服务 │ │ 今日成本 │       │
│  │ 12,847  │ │ 99.2%   │ │ 8       │ │ ¥1,234  │       │
│  └─────────┘ └─────────┘ └─────────┘ └─────────┘       │
│                                                         │
│  ┌─────────────────────────┐ ┌─────────────────────────┐│
│  │ 请求趋势 (24h)          │ │ 服务用量分布            ││
│  │ [折线图]                │ │ [饼图/条形图]           ││
│  └─────────────────────────┘ └─────────────────────────┘│
│                                                         │
│  ┌─────────────────────────────────────────────────────┐│
│  │ 最近活动                                            ││
│  └─────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────┘
```

**统计卡片:**
```jsx
// 移除渐变图标容器
<div className="flex items-center gap-3">
  <DatabaseOutlined className="text-gray-400 text-xl" />
  <div>
    <div className="text-2xl font-semibold">12,847</div>
    <div className="text-sm text-gray-500">总请求数</div>
  </div>
</div>
```

### 5.2 Knowledge

```
┌─────────────────────────────────────────────────────────┐
│  知识库                        [全部 ▼] [+ 创建数据集]   │
├─────────────────────────────────────────────────────────┤
│  ┌─────────────────────────────────────────────────────┐│
│  │ 🔍 搜索数据集名称                                    ││
│  └─────────────────────────────────────────────────────┘│
│                                                         │
│  ┌─────────────────────────┐ ┌─────────────────────────┐│
│  │ 知识问答                │ │ 产品文档                ││
│  │ ○ 未发布               │ │ ● 已发布               ││
│  │ 应用ID: 29bca5a85...   │ │ 应用ID: 8f2e1c34...    ││
│  │ 模型: deepseek-v3.1    │ │ 模型: gpt-4           ││
│  │ 更新于 2026-01-15      │ │ 更新于 2026-01-14      ││
│  └─────────────────────────┘ └─────────────────────────┘│
└─────────────────────────────────────────────────────────┘
```

---

## 6. 需要移除的元素

### 6.1 装饰性元素
- 所有 `blur-2xl` 渐变圆球
- 所有 `glow` 效果
- 渐变图标容器背景

### 6.2 动画
- Framer Motion 的 `whileHover={{ scale, rotate, y }}`
- `animate-ping` 脉冲效果
- 页面入口 `initial={{ opacity: 0, y: 20 }}`

### 6.3 样式
- 多层阴影叠加
- `135deg` 渐变背景
- `rounded-2xl`（改为 `rounded-lg`）

---

## 7. 实施步骤

### Phase 1: 主题系统
1. 更新 `themeConfig.ts` 配色
2. 更新 `index.css` CSS 变量
3. 更新 Tailwind 配置

### Phase 2: 基础组件
1. 重写 `Card` 组件
2. 重写 `Button` 组件
3. 更新 `Input`、`Badge` 等

### Phase 3: AppLayout
1. 简化侧边栏样式
2. 移除所有 Framer Motion 动画
3. 更新菜单选中态

### Phase 4: 页面
1. Dashboard 布局重构
2. Knowledge 列表页重构
3. 其他页面跟进

---

## 8. 验收标准

- [ ] 无渐变装饰元素
- [ ] 无 scale/rotate hover 动画
- [ ] 卡片仅 1px 边框，无阴影
- [ ] 主色仅用于按钮和激活状态
- [ ] 页面加载无动画
- [ ] 视觉风格接近阿里云百炼
