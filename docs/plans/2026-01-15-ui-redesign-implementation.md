# UI Redesign Implementation Plan - Alibaba Cloud Style

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Transform the AI Gateway frontend from "AI-generated" aesthetic to professional Alibaba Cloud-like enterprise style.

**Architecture:** Modify CSS variables and Tailwind config for new color system, simplify shadcn/ui components (Card, Button), remove Framer Motion animations from AppLayout, update page layouts.

**Tech Stack:** React, TypeScript, Tailwind CSS, Ant Design, shadcn/ui (Card, Button, Input, Badge)

---

## Phase 1: Theme System

### Task 1: Update CSS Variables in index.css

**Files:**
- Modify: `web/src/index.css:10-89` (`:root` and `.dark` CSS variables)

**Step 1: Replace root CSS variables**

Replace lines 10-89 with simplified color system:

```css
@layer base {
  :root {
    /* New Indigo-based primary */
    --background: 210 40% 98%;
    --foreground: 222 47% 11%;

    --card: 0 0% 100%;
    --card-foreground: 222 47% 11%;

    --popover: 0 0% 100%;
    --popover-foreground: 222 47% 11%;

    --primary: 239 84% 67%;
    --primary-foreground: 0 0% 100%;

    --secondary: 210 40% 96%;
    --secondary-foreground: 222 47% 11%;

    --muted: 210 40% 96%;
    --muted-foreground: 215 16% 47%;

    --accent: 210 40% 96%;
    --accent-foreground: 222 47% 11%;

    --destructive: 0 84% 60%;
    --destructive-foreground: 0 0% 100%;

    --border: 214 32% 91%;
    --input: 214 32% 91%;
    --ring: 239 84% 67%;

    --radius: 0.5rem;

    /* Simplified transitions */
    --transition-fast: 150ms;
    --transition-normal: 200ms;
  }

  .dark {
    --background: 222 47% 11%;
    --foreground: 210 40% 98%;

    --card: 222 47% 14%;
    --card-foreground: 210 40% 98%;

    --popover: 222 47% 14%;
    --popover-foreground: 210 40% 98%;

    --primary: 239 84% 67%;
    --primary-foreground: 0 0% 100%;

    --secondary: 217 33% 17%;
    --secondary-foreground: 210 40% 98%;

    --muted: 217 33% 17%;
    --muted-foreground: 215 20% 65%;

    --accent: 217 33% 17%;
    --accent-foreground: 210 40% 98%;

    --destructive: 0 62% 50%;
    --destructive-foreground: 0 0% 100%;

    --border: 217 33% 22%;
    --input: 217 33% 22%;
    --ring: 239 84% 67%;
  }
}
```

**Step 2: Remove decorative CSS classes**

Delete the following classes from `@layer components` (lines 146-368):
- `.aurora-bg` and `.aurora-bg::before`
- `.glass`
- `.gradient-border`
- `.tech-btn`
- `.tech-card`
- `.glow`, `.glow-cyan`, `.glow-purple`
- `.hover-card`
- `.btn-gradient-primary`, `.btn-gradient-danger`
- `.icon-gradient-*` classes
- `.border-gradient-left`

Keep only essential utility classes.

**Step 3: Simplify Ant Design overrides**

Replace gradient button styles (lines 554-575) with solid colors:

```css
/* Ant Design 主按钮 - 纯色 */
.ant-btn-primary:not(.ant-btn-dangerous) {
  background: hsl(var(--primary)) !important;
  border: none !important;
  box-shadow: none !important;
}

.ant-btn-primary:not(.ant-btn-dangerous):hover:not(:disabled) {
  background: hsl(239 84% 60%) !important;
  box-shadow: none !important;
}

/* Ant Design 危险按钮 - 纯色 */
.ant-btn-dangerous.ant-btn-primary {
  background: hsl(var(--destructive)) !important;
  border: none !important;
  box-shadow: none !important;
}

.ant-btn-dangerous.ant-btn-primary:hover:not(:disabled) {
  background: hsl(0 84% 55%) !important;
  box-shadow: none !important;
}
```

**Step 4: Run dev server to verify**

Run: `cd web && pnpm dev`
Expected: App loads without CSS errors

**Step 5: Commit**

```bash
git add web/src/index.css
git commit -m "style: simplify CSS variables and remove decorative classes"
```

---

### Task 2: Update Tailwind Config

**Files:**
- Modify: `web/tailwind.config.ts:62-66` (borderRadius)

**Step 1: Update border radius values**

Replace borderRadius config:

```typescript
borderRadius: {
  lg: "0.5rem",    // 8px - cards, modals
  md: "0.375rem",  // 6px - buttons, inputs
  sm: "0.25rem",   // 4px - badges, tags
},
```

**Step 2: Commit**

```bash
git add web/tailwind.config.ts
git commit -m "style: update border radius to smaller values"
```

---

### Task 3: Update Ant Design Theme Config

**Files:**
- Modify: `web/src/theme/themeConfig.ts`

**Step 1: Simplify light theme**

Update `lightTheme.token` to remove gradients and shadows:

```typescript
export const lightTheme: ThemeConfig = {
  token: {
    colorPrimary: '#6366F1',
    colorInfo: '#3B82F6',
    colorSuccess: '#22C55E',
    colorWarning: '#F59E0B',
    colorError: '#EF4444',

    colorBgContainer: '#ffffff',
    colorBgElevated: '#ffffff',
    colorBgLayout: '#F9FAFB',

    colorText: '#111827',
    colorTextSecondary: '#6B7280',
    colorTextTertiary: '#9CA3AF',

    colorBorder: '#E5E7EB',
    colorBorderSecondary: '#F3F4F6',

    borderRadius: 8,
    borderRadiusLG: 8,
    borderRadiusSM: 6,
    borderRadiusXS: 4,

    boxShadow: 'none',
    boxShadowSecondary: '0 4px 6px -1px rgba(0, 0, 0, 0.1)',
  },
  components: {
    Button: {
      borderRadius: 6,
      primaryShadow: 'none',
    },
    Card: {
      borderRadiusLG: 8,
    },
    Menu: {
      itemBg: 'transparent',
      itemSelectedBg: '#EEF2FF',
      itemSelectedColor: '#6366F1',
      itemHoverBg: '#F3F4F6',
      itemBorderRadius: 6,
    },
  },
};
```

**Step 2: Simplify dark theme similarly**

**Step 3: Commit**

```bash
git add web/src/theme/themeConfig.ts
git commit -m "style: simplify Ant Design theme config"
```

---

## Phase 2: Base Components

### Task 4: Simplify Card Component

**Files:**
- Modify: `web/src/components/ui/card.tsx:11-14`

**Step 1: Update Card base styles**

Replace Card className:

```tsx
const Card = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement>
>(({ className, ...props }, ref) => (
  <div
    ref={ref}
    className={cn(
      "rounded-lg border bg-card text-card-foreground",
      className
    )}
    {...props}
  />
));
```

Changes:
- `rounded-2xl` → `rounded-lg` (8px)
- Remove `shadow-sm`
- Remove `transition-all duration-200`

**Step 2: Update CardTitle**

```tsx
const CardTitle = React.forwardRef<
  HTMLParagraphElement,
  React.HTMLAttributes<HTMLHeadingElement>
>(({ className, ...props }, ref) => (
  <h3
    ref={ref}
    className={cn("text-lg font-semibold leading-none tracking-tight", className)}
    {...props}
  />
));
```

Change: `text-2xl` → `text-lg`

**Step 3: Commit**

```bash
git add web/src/components/ui/card.tsx
git commit -m "style: simplify Card component - remove shadow and animations"
```

---

### Task 5: Simplify Button Component

**Files:**
- Modify: `web/src/components/ui/button.tsx:8-84`

**Step 1: Replace buttonVariants with simplified version**

```tsx
const buttonVariants = cva(
  "inline-flex items-center justify-center whitespace-nowrap rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        default: "bg-primary text-primary-foreground hover:bg-primary/90",
        secondary: "bg-secondary text-secondary-foreground border border-border hover:bg-secondary/80",
        outline: "border border-input bg-background hover:bg-accent hover:text-accent-foreground",
        ghost: "hover:bg-accent hover:text-accent-foreground",
        destructive: "bg-destructive text-destructive-foreground hover:bg-destructive/90",
        link: "text-primary underline-offset-4 hover:underline",
      },
      size: {
        default: "h-9 px-4 py-2",
        sm: "h-8 rounded-md px-3 text-xs",
        lg: "h-10 rounded-md px-6",
        icon: "h-9 w-9",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
);
```

Key changes:
- Remove all gradients
- Remove all shadows
- Remove scale/translate animations
- Remove `::before` pseudo-element effects
- Simplify to solid colors with hover state

**Step 2: Commit**

```bash
git add web/src/components/ui/button.tsx
git commit -m "style: simplify Button - remove gradients, shadows, animations"
```

---

## Phase 3: AppLayout

### Task 6: Simplify AppLayout Sidebar

**Files:**
- Modify: `web/src/layouts/AppLayout.tsx`

**Step 1: Simplify Logo component (lines 89-156)**

Replace Logo component:

```tsx
function Logo({ collapsed }: { collapsed: boolean }) {
  return (
    <div className="flex items-center gap-3">
      <div className="flex-shrink-0">
        <svg width="32" height="32" viewBox="0 0 32 32" fill="none">
          <rect x="2" y="2" width="28" height="28" rx="8" fill="#6366F1" opacity="0.1"/>
          <path
            d="M16 6L26 12V20L16 26L6 20V12L16 6Z"
            stroke="#6366F1"
            strokeWidth="2"
            fill="none"
          />
          <circle cx="16" cy="16" r="4" fill="#6366F1" />
        </svg>
      </div>
      {!collapsed && (
        <div className="flex flex-col">
          <span className="text-base font-semibold text-foreground">
            AI Platform
          </span>
          <span className="text-xs text-muted-foreground">
            Unified AI Services
          </span>
        </div>
      )}
    </div>
  );
}
```

Changes:
- Remove Framer Motion
- Remove gradient SVG
- Remove AnimatePresence
- Static simple logo

**Step 2: Simplify ThemeToggle (lines 159-199)**

```tsx
function ThemeToggle({ darkMode, onToggle, tooltip }: { darkMode: boolean; onToggle: () => void; tooltip: string }) {
  return (
    <Tooltip title={tooltip}>
      <button
        onClick={onToggle}
        className="flex items-center justify-center w-9 h-9 rounded-md hover:bg-accent transition-colors"
      >
        {darkMode ? (
          <MoonOutlined className="text-lg text-primary" />
        ) : (
          <SunOutlined className="text-lg text-amber-500" />
        )}
      </button>
    </Tooltip>
  );
}
```

Changes:
- Remove Framer Motion animations
- Simple hover background change

**Step 3: Remove motion imports and wrapper**

Remove line 22: `import { motion, AnimatePresence } from "framer-motion";`

**Step 4: Simplify Sider styles (lines 304-324)**

```tsx
<Sider
  collapsible
  collapsed={collapsed}
  onCollapse={setCollapsed}
  trigger={null}
  width={220}
  collapsedWidth={64}
  style={{
    position: 'fixed',
    left: 0,
    top: 0,
    bottom: 0,
    zIndex: 100,
    background: darkMode ? '#111827' : '#ffffff',
    borderRight: `1px solid ${darkMode ? '#374151' : '#E5E7EB'}`,
    transition: 'width 200ms',
  }}
>
```

Changes:
- Remove gradient backgrounds
- Solid colors only
- Simplified transition

**Step 5: Simplify menu styles in <style> tag (lines 556-598)**

```css
.app-sider .ant-menu-item {
  margin: 4px 8px !important;
  border-radius: 6px !important;
  height: 40px !important;
  line-height: 40px !important;
}

.app-sider .ant-menu-item:hover {
  background: ${darkMode ? 'rgba(255,255,255,0.05)' : '#F3F4F6'} !important;
}

.app-sider .ant-menu-item-selected {
  background: ${darkMode ? 'rgba(99, 102, 241, 0.15)' : '#EEF2FF'} !important;
  border-left: 3px solid #6366F1 !important;
}

.app-sider .ant-menu-item-selected::before,
.app-sider .ant-menu-item-selected::after {
  display: none !important;
}
```

Changes:
- Remove gradient selected background
- Simple left border for selected state
- Remove transform effects

**Step 6: Remove Content motion wrapper (lines 504-510)**

Change from:
```tsx
<Content>
  <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }}>
    <Outlet />
  </motion.div>
</Content>
```

To:
```tsx
<Content>
  <Outlet />
</Content>
```

**Step 7: Simplify user avatar (lines 480-491)**

```tsx
<div className="w-8 h-8 rounded-md bg-primary/10 flex items-center justify-center">
  <UserOutlined className="text-primary" />
</div>
```

Remove gradient background and shadow.

**Step 8: Commit**

```bash
git add web/src/layouts/AppLayout.tsx
git commit -m "style: simplify AppLayout - remove animations, gradients, glow effects"
```

---

## Phase 4: Dashboard Page

### Task 7: Simplify ServiceCard Component

**Files:**
- Modify: `web/src/components/ServiceCard.tsx`

**Step 1: Remove decorative elements**

Find and remove:
- Gradient background orbs (blur-2xl elements)
- Gradient icon containers
- Hover scale/translate animations
- Shadow effects

**Step 2: Simplify to flat design**

```tsx
<Card className="relative overflow-hidden">
  <CardContent className="p-5">
    <div className="flex items-start gap-4">
      {/* Simple icon - no gradient container */}
      <div className="text-2xl text-gray-400">
        {getServiceIcon(service.service_type)}
      </div>

      <div className="flex-1 min-w-0">
        <h3 className="font-medium text-foreground truncate">
          {service.display_name}
        </h3>
        <p className="text-sm text-muted-foreground mt-1">
          {service.service_id}
        </p>
      </div>

      {/* Status badge */}
      <Badge variant={health?.healthy ? 'default' : 'destructive'}>
        {health?.healthy ? t('common.active') : t('common.disabled')}
      </Badge>
    </div>
  </CardContent>
</Card>
```

**Step 3: Commit**

```bash
git add web/src/components/ServiceCard.tsx
git commit -m "style: simplify ServiceCard - flat design, no decorations"
```

---

### Task 8: Simplify ServiceCostAnalysis Component

**Files:**
- Modify: `web/src/components/ServiceCostAnalysis.tsx`

**Step 1: Remove gradient stat icons**

Replace gradient icon boxes with simple icons:

```tsx
// Before
<div style={{
  background: 'linear-gradient(135deg, #3b82f6 0%, #60a5fa 100%)',
  // ...
}}>
  <Icon />
</div>

// After
<Icon className="text-xl text-gray-400" />
```

**Step 2: Simplify stat cards**

Remove shadows and hover effects.

**Step 3: Commit**

```bash
git add web/src/components/ServiceCostAnalysis.tsx
git commit -m "style: simplify ServiceCostAnalysis - remove gradients"
```

---

## Phase 5: Knowledge Page

### Task 9: Simplify Datasets Page

**Files:**
- Modify: `web/src/pages/knowledge/Datasets.tsx`

**Step 1: Remove gradient stat boxes**

Find StatCard component and simplify:
- Remove gradient backgrounds
- Remove box shadows
- Remove hover animations

**Step 2: Simplify dataset cards**

- Remove decorative blur elements
- Remove gradient icon containers
- Remove hover scale effects

**Step 3: Commit**

```bash
git add web/src/pages/knowledge/Datasets.tsx
git commit -m "style: simplify Datasets page - flat design"
```

---

## Final Verification

### Task 10: Visual QA

**Step 1: Test light mode**

Run: `cd web && pnpm dev`
Navigate through all pages and verify:
- [ ] No gradient backgrounds
- [ ] No glow effects
- [ ] No scale/translate hover animations
- [ ] Cards have 1px border only
- [ ] Primary color only on buttons and active states

**Step 2: Test dark mode**

Toggle dark mode and verify same criteria.

**Step 3: Final commit**

```bash
git add -A
git commit -m "style: complete UI redesign - Alibaba Cloud style"
```

---

## Summary

| Phase | Tasks | Key Changes |
|-------|-------|-------------|
| 1 | 1-3 | CSS variables, Tailwind, Ant Design theme |
| 2 | 4-5 | Card, Button components |
| 3 | 6 | AppLayout (sidebar, header, animations) |
| 4 | 7-8 | Dashboard components |
| 5 | 9-10 | Knowledge page, final QA |

**Estimated commits:** 10
**Key principle:** Remove decorations, keep function
