# 知识库管理模块优化计划

## 概述
本计划用于指导 codex 执行知识库管理模块的 UI 优化和功能完善。主要涉及文件：
- `web/src/pages/knowledge/DatasetDetail.tsx` - 知识库详情页
- `web/src/types/knowledge.ts` - 类型定义
- 可能需要新增组件文件

---

## 问题一：上传文档对话框 UI 优化

### 现状问题
点击"上传数据"按钮后弹出的对话框中，文件上传区域占据了大量空间，而解析设置被压缩在下方，布局不合理。

### 修改方案

**文件**: `web/src/pages/knowledge/DatasetDetail.tsx`

1. **重新设计对话框布局**：采用左右分栏或上下紧凑布局
   - 左侧/上方：文件上传区（紧凑型，高度限制在 150-200px）
   - 右侧/下方：解析设置（主要配置区域）

2. **文件上传区优化**：
   ```tsx
   // 将文件上传区改为紧凑型
   <div className="border-2 border-dashed border-gray-300 rounded-lg p-4 text-center h-[150px] flex flex-col items-center justify-center">
     <Upload className="h-8 w-8 text-gray-400 mb-2" />
     <p className="text-sm text-gray-600">拖拽文件到此处或点击上传</p>
     <p className="text-xs text-gray-400 mt-1">支持 PDF、Word、TXT、Markdown</p>
   </div>
   ```

3. **已上传文件列表**：使用紧凑的标签式展示
   ```tsx
   <div className="flex flex-wrap gap-2 mt-3">
     {files.map(file => (
       <Badge key={file.name} variant="secondary" className="flex items-center gap-1">
         <FileText className="h-3 w-3" />
         {file.name}
         <button onClick={() => removeFile(file)}><X className="h-3 w-3" /></button>
       </Badge>
     ))}
   </div>
   ```

---

## 问题二：分块策略解耦与完善

### 现状问题
1. 必须先选择"自定义"才能配置分块策略
2. 所有分块模式共用相同的参数配置
3. 层级切分（hierarchical）缺少父块/子块大小配置
4. 正则切分（regex）没有正则表达式输入框

### 修改方案

**文件**: `web/src/pages/knowledge/DatasetDetail.tsx`

1. **移除"自定义"前置条件**：直接展示所有分块模式选项

2. **为每种分块模式创建独立的参数配置组件**：

```tsx
// 分块模式配置 - 根据选择的模式显示不同参数
const ChunkingModeConfig = ({ mode, config, onChange }) => {
  switch (mode) {
    case "fixed_size":
      return (
        <div className="space-y-4">
          <div>
            <Label>块大小 (字符数)</Label>
            <Slider value={[config.chunk_size || 500]} min={100} max={2000} step={50}
              onValueChange={([v]) => onChange({ ...config, chunk_size: v })} />
            <span className="text-sm text-gray-500">{config.chunk_size || 500} 字符</span>
          </div>
          <div>
            <Label>重叠大小 (字符数)</Label>
            <Slider value={[config.overlap || 50]} min={0} max={500} step={10}
              onValueChange={([v]) => onChange({ ...config, overlap: v })} />
            <span className="text-sm text-gray-500">{config.overlap || 50} 字符</span>
          </div>
        </div>
      );

    case "paragraph":
      return (
        <div className="space-y-4">
          <div>
            <Label>最小段落长度</Label>
            <Input type="number" value={config.min_length || 50}
              onChange={(e) => onChange({ ...config, min_length: parseInt(e.target.value) })} />
          </div>
          <div>
            <Label>合并短段落</Label>
            <Switch checked={config.merge_short || false}
              onCheckedChange={(v) => onChange({ ...config, merge_short: v })} />
          </div>
        </div>
      );

    case "heading":
      return (
        <div className="space-y-4">
          <div>
            <Label>标题级别</Label>
            <Select value={config.heading_level || "h2"}
              onValueChange={(v) => onChange({ ...config, heading_level: v })}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="h1">H1 - 一级标题</SelectItem>
                <SelectItem value="h2">H2 - 二级标题</SelectItem>
                <SelectItem value="h3">H3 - 三级标题</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
      );

    case "hierarchical":
      return (
        <div className="space-y-4">
          <div className="p-3 bg-blue-50 rounded-lg">
            <p className="text-sm text-blue-700">
              层级切分会生成父块和子块，父块用于提供上下文，子块用于精确检索
            </p>
          </div>
          <div>
            <Label>父块大小 (字符数)</Label>
            <Slider value={[config.parent_chunk_size || 1500]} min={500} max={4000} step={100}
              onValueChange={([v]) => onChange({ ...config, parent_chunk_size: v })} />
            <span className="text-sm text-gray-500">{config.parent_chunk_size || 1500} 字符</span>
          </div>
          <div>
            <Label>子块大小 (字符数)</Label>
            <Slider value={[config.child_chunk_size || 300]} min={100} max={1000} step={50}
              onValueChange={([v]) => onChange({ ...config, child_chunk_size: v })} />
            <span className="text-sm text-gray-500">{config.child_chunk_size || 300} 字符</span>
          </div>
          <div>
            <Label>子块重叠 (字符数)</Label>
            <Slider value={[config.child_overlap || 50]} min={0} max={200} step={10}
              onValueChange={([v]) => onChange({ ...config, child_overlap: v })} />
            <span className="text-sm text-gray-500">{config.child_overlap || 50} 字符</span>
          </div>
        </div>
      );

    case "separator":
      return (
        <div className="space-y-4">
          <div>
            <Label>分隔符</Label>
            <Input value={config.separator || "\\n\\n"}
              onChange={(e) => onChange({ ...config, separator: e.target.value })}
              placeholder="例如: \\n\\n 或 ---" />
            <p className="text-xs text-gray-500 mt-1">支持转义字符：\n(换行) \t(制表符)</p>
          </div>
          <div>
            <Label>保留分隔符</Label>
            <Switch checked={config.keep_separator || false}
              onCheckedChange={(v) => onChange({ ...config, keep_separator: v })} />
          </div>
        </div>
      );

    case "regex":
      return (
        <div className="space-y-4">
          <div>
            <Label>正则表达式模式</Label>
            <Input value={config.pattern || ""}
              onChange={(e) => onChange({ ...config, pattern: e.target.value })}
              placeholder="例如: (?=第[一二三四五六七八九十]+章)" />
            <p className="text-xs text-gray-500 mt-1">
              使用正向前瞻 (?=...) 保留匹配内容，使用普通模式则删除匹配内容
            </p>
          </div>
          <div>
            <Label>预设模式</Label>
            <Select onValueChange={(v) => onChange({ ...config, pattern: v })}>
              <SelectTrigger><SelectValue placeholder="选择预设模式" /></SelectTrigger>
              <SelectContent>
                <SelectItem value="(?=第[一二三四五六七八九十]+章)">按"第X章"切分</SelectItem>
                <SelectItem value="(?=\\d+\\.)">按数字编号切分</SelectItem>
                <SelectItem value="(?=#{1,3}\\s)">按 Markdown 标题切分</SelectItem>
                <SelectItem value="\n\n+">按空行切分</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
      );

    case "qa":
      return (
        <div className="space-y-4">
          <div className="p-3 bg-amber-50 rounded-lg">
            <p className="text-sm text-amber-700">
              QA切分会使用 AI 将文档转换为问答对格式，适合FAQ类文档
            </p>
          </div>
          <div>
            <Label>问题标识符</Label>
            <Input value={config.question_prefix || "Q:"}
              onChange={(e) => onChange({ ...config, question_prefix: e.target.value })} />
          </div>
          <div>
            <Label>答案标识符</Label>
            <Input value={config.answer_prefix || "A:"}
              onChange={(e) => onChange({ ...config, answer_prefix: e.target.value })} />
          </div>
        </div>
      );

    case "automatic":
    default:
      return (
        <div className="p-4 bg-gray-50 rounded-lg">
          <p className="text-sm text-gray-600">
            自动模式会根据文档类型智能选择最佳切分策略，无需额外配置
          </p>
        </div>
      );
  }
};
```

3. **更新类型定义**：

**文件**: `web/src/types/knowledge.ts`

```typescript
interface ChunkingConfig {
  mode: "automatic" | "fixed_size" | "paragraph" | "heading" | "hierarchical" | "separator" | "regex" | "qa";

  // fixed_size 模式
  chunk_size?: number;
  overlap?: number;

  // paragraph 模式
  min_length?: number;
  merge_short?: boolean;

  // heading 模式
  heading_level?: "h1" | "h2" | "h3";

  // hierarchical 模式
  parent_chunk_size?: number;
  child_chunk_size?: number;
  child_overlap?: number;

  // separator 模式
  separator?: string;
  keep_separator?: boolean;

  // regex 模式
  pattern?: string;

  // qa 模式
  question_prefix?: string;
  answer_prefix?: string;
}
```

---

## 问题三：元数据增强配置

### 现状问题
缺少元数据增强功能的配置界面，用户无法配置是否启用以及如何增强元数据。

### 修改方案

**文件**: `web/src/pages/knowledge/DatasetDetail.tsx`

1. **添加元数据增强配置区域**：

```tsx
// 元数据增强配置
<div className="space-y-4">
  <div className="flex items-center justify-between">
    <div>
      <Label className="text-base font-medium">元数据增强</Label>
      <p className="text-sm text-gray-500">自动提取和丰富文档元数据</p>
    </div>
    <Switch checked={config.metadata_enhancement?.enabled || false}
      onCheckedChange={(v) => updateConfig({
        metadata_enhancement: { ...config.metadata_enhancement, enabled: v }
      })} />
  </div>

  {config.metadata_enhancement?.enabled && (
    <div className="pl-4 border-l-2 border-indigo-200 space-y-4">
      <div className="flex items-center gap-2">
        <Checkbox checked={config.metadata_enhancement.extract_title}
          onCheckedChange={(v) => updateConfig({
            metadata_enhancement: { ...config.metadata_enhancement, extract_title: v }
          })} />
        <Label>自动提取文档标题</Label>
      </div>
      <div className="flex items-center gap-2">
        <Checkbox checked={config.metadata_enhancement.extract_summary}
          onCheckedChange={(v) => updateConfig({
            metadata_enhancement: { ...config.metadata_enhancement, extract_summary: v }
          })} />
        <Label>自动生成摘要</Label>
      </div>
      <div className="flex items-center gap-2">
        <Checkbox checked={config.metadata_enhancement.extract_keywords}
          onCheckedChange={(v) => updateConfig({
            metadata_enhancement: { ...config.metadata_enhancement, extract_keywords: v }
          })} />
        <Label>自动提取关键词</Label>
      </div>
      <div className="flex items-center gap-2">
        <Checkbox checked={config.metadata_enhancement.extract_entities}
          onCheckedChange={(v) => updateConfig({
            metadata_enhancement: { ...config.metadata_enhancement, extract_entities: v }
          })} />
        <Label>自动识别命名实体</Label>
      </div>
      <div className="flex items-center gap-2">
        <Checkbox checked={config.metadata_enhancement.detect_language}
          onCheckedChange={(v) => updateConfig({
            metadata_enhancement: { ...config.metadata_enhancement, detect_language: v }
          })} />
        <Label>自动检测语言</Label>
      </div>
    </div>
  )}
</div>
```

2. **更新类型定义**：

**文件**: `web/src/types/knowledge.ts`

```typescript
interface MetadataEnhancementConfig {
  enabled: boolean;
  extract_title?: boolean;
  extract_summary?: boolean;
  extract_keywords?: boolean;
  extract_entities?: boolean;
  detect_language?: boolean;
}
```

---

## 问题四：表格处理增强配置

### 现状问题
缺少表格处理的配置界面，用户无法配置表格的解析和处理方式。

### 修改方案

**文件**: `web/src/pages/knowledge/DatasetDetail.tsx`

1. **添加表格处理配置区域**：

```tsx
// 表格处理配置
<div className="space-y-4">
  <div className="flex items-center justify-between">
    <div>
      <Label className="text-base font-medium">表格处理增强</Label>
      <p className="text-sm text-gray-500">优化表格内容的解析和检索</p>
    </div>
    <Switch checked={config.table_processing?.enabled || false}
      onCheckedChange={(v) => updateConfig({
        table_processing: { ...config.table_processing, enabled: v }
      })} />
  </div>

  {config.table_processing?.enabled && (
    <div className="pl-4 border-l-2 border-indigo-200 space-y-4">
      <div>
        <Label>表格处理模式</Label>
        <Select value={config.table_processing.mode || "markdown"}
          onValueChange={(v) => updateConfig({
            table_processing: { ...config.table_processing, mode: v }
          })}>
          <SelectTrigger><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="markdown">转换为 Markdown 表格</SelectItem>
            <SelectItem value="row_based">按行拆分（每行一个块）</SelectItem>
            <SelectItem value="cell_based">按单元格拆分</SelectItem>
            <SelectItem value="structured">结构化JSON</SelectItem>
            <SelectItem value="natural_language">转换为自然语言描述</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <div className="flex items-center gap-2">
        <Checkbox checked={config.table_processing.include_headers}
          onCheckedChange={(v) => updateConfig({
            table_processing: { ...config.table_processing, include_headers: v }
          })} />
        <Label>每行包含表头信息</Label>
      </div>

      <div className="flex items-center gap-2">
        <Checkbox checked={config.table_processing.generate_summary}
          onCheckedChange={(v) => updateConfig({
            table_processing: { ...config.table_processing, generate_summary: v }
          })} />
        <Label>生成表格摘要</Label>
      </div>

      <div>
        <Label>最大表格大小限制</Label>
        <Select value={String(config.table_processing.max_rows || 100)}
          onValueChange={(v) => updateConfig({
            table_processing: { ...config.table_processing, max_rows: parseInt(v) }
          })}>
          <SelectTrigger><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="50">50 行</SelectItem>
            <SelectItem value="100">100 行</SelectItem>
            <SelectItem value="200">200 行</SelectItem>
            <SelectItem value="500">500 行</SelectItem>
            <SelectItem value="-1">不限制</SelectItem>
          </SelectContent>
        </Select>
      </div>
    </div>
  )}
</div>
```

2. **更新类型定义**：

**文件**: `web/src/types/knowledge.ts`

```typescript
interface TableProcessingConfig {
  enabled: boolean;
  mode?: "markdown" | "row_based" | "cell_based" | "structured" | "natural_language";
  include_headers?: boolean;
  generate_summary?: boolean;
  max_rows?: number;
}
```

---

## 实施步骤

### 第一步：更新类型定义
1. 打开 `web/src/types/knowledge.ts`
2. 添加或更新 `ChunkingConfig`、`MetadataEnhancementConfig`、`TableProcessingConfig` 类型
3. 更新 `DocumentConfig` 接口包含这些新类型

### 第二步：重构上传对话框
1. 在 `DatasetDetail.tsx` 中找到上传文档的 Dialog 组件
2. 重新设计布局，压缩文件上传区域
3. 将解析设置作为主要配置区域

### 第三步：实现分块策略解耦
1. 创建 `ChunkingModeConfig` 组件
2. 为每种分块模式实现独立的参数配置
3. 移除"自定义"前置条件，直接展示所有模式

### 第四步：添加元数据增强配置
1. 在解析设置区域添加元数据增强配置组件
2. 实现各个增强选项的开关

### 第五步：添加表格处理配置
1. 在解析设置区域添加表格处理配置组件
2. 实现表格处理模式选择和相关选项

### 第六步：测试验证
1. 测试所有分块模式的配置是否正确保存
2. 测试元数据增强和表格处理配置
3. 验证 API 调用参数正确传递

---

## 注意事项

1. **后端兼容性**：确保前端发送的配置参数与后端 API 期望的格式一致
2. **默认值处理**：为所有配置项设置合理的默认值
3. **表单验证**：为正则表达式等输入添加格式验证
4. **用户体验**：添加配置说明和提示信息
5. **响应式设计**：确保配置界面在不同屏幕尺寸下正常显示

---

## 参考资料

- 阿里云百炼知识库配置界面设计
- 当前后端支持的 chunking 策略：automatic, fixed_size, paragraph, heading, hierarchical, recursive, separator, regex, qa
- 后端文件：`src/services/knowledge/chunking.py`
