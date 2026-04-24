# Assistant-Service 文档生成能力 SOTA 升级计划

**版本**：v1.0
**作者**：AI Engineer（Misaya）+ Claude（调研 & 起草）
**日期**：2026-04-21
**适用范围**：`apps/assistant-service/` + `src/services/assistant/tools/`
**目标读者**：Claude Code（实施者）、AI Gateway 团队
**关联文档**：`plans/ADR-001-Assistant-Service-Extraction.md`、`plans/Skills-System-Design.md`

---

## 0. TL;DR（给 Claude Code 的一页纸）

当前 `assistant-service` 的 Word/PDF/PPT 生成是"能跑但不能看"级别：

- **DOCX**：`python-docx` 裸调用，只支持标题+段落+列表；无表格、无图片、无主题、无 TOC、无页眉页脚。
- **PPTX**：`python-pptx` 5 种硬编码模板，4 种配色；无图片、无图表、无 speaker notes、无溢出检测。
- **PDF**：HTML→WeasyPrint，CSS 内联写死；无图表、无矢量布局、无字体兜底。
- **XLSX**：**完全没实现**。
- **验证**：`quality/guardrails.py` 仅做字数阈值校验，不渲染、不看图、不重算公式。
- **Skill 集成**：`/mnt/.claude/skills/{docx,pptx,pdf,xlsx}/` 四份高质量 SKILL.md **完全没接入**。

**行业 SOTA（2026 年 4 月）的共识架构**：

> **LLM 写代码 → 沙箱执行 → 渲染成图 → 视觉 critic 子 agent → 修 bug → 再渲染 → 通过后返回文件**

该范式由 Anthropic 的 Agent Skills 系统开源定义，Microsoft 365 Copilot Agents、Google Gemini in Sheets、PPTAgent（EMNLP 2025）全部是它的变体。OpenAI Code Interpreter 少了"视觉 critic"环节。

**本计划的核心动作**：把 `assistant-service` 的文档模块从"Python 函数直接生成文件"重构为"**Skill 驱动 + 沙箱执行 + 视觉验证**的代理式流水线"，四个分阶段里程碑，预计 6-8 周。

---

## 1. 现状审计（baseline）

### 1.1 代码位置与实现

| 能力 | 文件路径 | 库 | 主要问题 |
|---|---|---|---|
| DOCX 生成 | `src/services/assistant/tools/document_generator_tool.py::MarkdownToDocxConverter` | `python-docx` | 无表格/图片/主题；标题写死居中；List 样式硬编码 |
| PDF 生成 | 同上 `::MarkdownToPdfConverter` | `markdown` + `weasyprint`（**未写入依赖**） | 内联 CSS；无图表；WeasyPrint 未进 pyproject，生产环境大概率崩 |
| PPTX 生成 | `src/services/assistant/tools/pptx_generator_tool.py` | `python-pptx` | 固定 5 种 slide 类型、4 种主题；无图片/图表/notes；无溢出检测 |
| XLSX 生成 | — | — | **不存在** |
| 质量保护 | `src/services/assistant/quality/guardrails.py` | — | 仅字数、段数、banned phrase 阈值 |
| 工具注册 | `register_document_generation_tool()` in `main.py` | — | 单个 flat tool，不分类、不进度流式、不复用 |
| 输出投递 | `ToolCallResult.output_files[]`（base64） | — | 无持久化、无预览、无可视化 |

### 1.2 数据流

```
User prompt
  └─► assistant-service LLM agent
        └─► tool_call("generate_document" | "generate_pptx", {...json...})
              └─► Python 函数直接 build OOXML
                    └─► base64 塞回 tool_result
                          └─► 前端下载
```

**问题**：整条链路没有"规划 → 执行 → 验证"分层，没有失败重试，没有中间预览。

### 1.3 与 Claude Skills 系统的 gap

仓库里已经有 `/mnt/.claude/skills/docx`、`/pptx`、`/pdf`、`/xlsx`（官方 SOTA 实现），但：

- `assistant-service` 的 agent 不知道它们存在；
- 没有 skill-runner、没有 progressive disclosure；
- 目前的 Python 生成逻辑与 Skills 里推荐的 `docx-js` / `pptxgenjs` / OOXML unpack-edit-repack 路线完全不兼容。

---

## 2. 业界 SOTA 速览（决策依据）

### 2.1 Anthropic Agent Skills（黄金参考）

**四步循环**：
1. **Read**：`markitdown` / `pandoc` / `pdfplumber` 读输入；`pdftoppm` 渲染成 JPEG 供 vision 子 agent 看。
2. **Plan**：outline + layout map + 色板选择（SKILL.md 里内置 10 套调色板、8 组字体配对）。
3. **Generate**：
   - DOCX：`docx-js` (Node) 新建；**OOXML unpack → 文本编辑 → pack** 编辑。
   - PPTX：`pptxgenjs` (Node) 新建；OOXML 编辑现有模板。
   - XLSX：`openpyxl`（带公式）+ `pandas`（数据）+ LibreOffice `recalc.py`。
   - PDF：ReportLab（Canvas / Platypus）主路径；`pypdf` 合并拆分；`pypdf` + 坐标 fallback 填表。
4. **Verify**：
   - PPTX/PDF：`soffice --headless --convert-to pdf` → `pdftoppm -jpeg -r 150` → **派发"新鲜 context"的 vision 子 agent**，prompt 明确"假设有问题，去找问题"。
   - XLSX：LibreOffice recalc；出现任何 `#REF!/#DIV/0!/#VALUE!` 一律打回。
   - DOCX：`validate.py` 做 OOXML 校验 + 自动修复（RSID、`xml:space="preserve"` 等）。

**关键创新**：
- **progressive disclosure**：只 YAML frontmatter（~100 tok）预加载；body、次级 md、脚本按需打开。
- **filesystem-backed runtime**：沙箱里 python-docx/pptx/openpyxl/reportlab/pypdfium2 全预装；最大 30 MB。
- **opinionated style**：SKILL 里写死"不要用 accent line under title"、"不要 Unicode bullet"、"PPT 永远别用 `#` 前缀的 hex"，这些是最高 ROI 的内容。

### 2.2 其他家的定位

| 厂商 | 做法 | 值得借鉴 |
|---|---|---|
| **Microsoft 365 Copilot Agents** | 驱动真正的 Word/Excel/PPT 二进制；多轮"中间验证循环"；UI 流式展示 plan + 中间草稿 | 流式中间件；向用户展示 critic 发现的问题 |
| **Google Gemini in Workspace** | Sheets 在 SpreadsheetBench 达 70.48%；Slides 匹配已有主题而不是从零生成 | "继承主题"的增量式生成 |
| **OpenAI Code Interpreter** | `python-docx`/`python-pptx`/`openpyxl` 预装；**无 bash、无 pip install、无网络** | 反例：少了 vision critic 这一环 |
| **PPTAgent (EMNLP 2025)** | 聚类参考幻灯片→抽 schema→5 种编辑 API 迭代修正 | 两阶段 agent；edit/remove/duplicate 显式原语 |
| **Gamma / Beautiful.ai** | 约束式模板引擎；Smart Slides live-layout | 受限但高质量的模板优于自由 OOXML |
| **Typst + Pandoc** | 现代 LaTeX 替代，LLM 友好，可一次性把复杂 LaTeX 模板迁移成 Typst | PDF 高保真长文档备选路径 |

### 2.3 六大架构范式与我们的选择

| 范式 | 代表 | 推荐 |
|---|---|---|
| ① Code-execution agent（LLM 写 Python/JS） | Anthropic / OpenAI / MS Copilot | ✅ **主路径** |
| ② Schema-first JSON IR → deterministic renderer | Beautiful.ai、LangGraph 社区 | ✅ **planner → renderer 之间的合约**（避免让 LLM 直接写 XML） |
| ③ HTML-as-universal-source | 大量开源 AI PPT 工具 | ⚠️ 备选（PDF 走这条；PPT/DOCX 保真度不够） |
| ④ Template + slot-filling（docxtpl / PPT 母版） | 企业内部工具 | ✅ **品牌模板场景优先** |
| ⑤ 多 agent（planner→designer→renderer→critic） | PPTAgent、MS Copilot | ✅ **采用** |
| ⑥ Render-to-image + vision critic | Anthropic SKILL | ✅ **采用，最高 ROI** |

---

## 3. 目标架构

### 3.1 分层总览

```
┌─────────────────────────────────────────────────────────────────┐
│  User prompt                                                     │
└────────────────┬────────────────────────────────────────────────┘
                 ▼
┌─────────────────────────────────────────────────────────────────┐
│  Assistant Agent (LLM, Claude Sonnet 4.6)                        │
│  system_prompt += <available_skills>（progressive disclosure）    │
└────────────────┬────────────────────────────────────────────────┘
                 ▼
┌─────────────────────────────────────────────────────────────────┐
│  SkillRouter    [core/skills/router.py]                          │
│    detects format (docx|pptx|xlsx|pdf) →                         │
│    loads SKILL.md frontmatter → 命中后加载 full body             │
└────────────────┬────────────────────────────────────────────────┘
                 ▼
┌─────────────────────────────────────────────────────────────────┐
│  Planner LLM call → JSON IR                                      │
│   (typed, pydantic-validated; see §4.2)                          │
└────────────────┬────────────────────────────────────────────────┘
                 ▼
┌─────────────────────────────────────────────────────────────────┐
│  Renderer Dispatcher  [core/files/renderers/*]                   │
│    ├── DocxRenderer   (docx-js via Node sidecar OR python-docx)  │
│    ├── PptxRenderer   (pptxgenjs via Node sidecar)               │
│    ├── XlsxRenderer   (openpyxl + pandas)                        │
│    └── PdfRenderer    (Playwright HTML→PDF / ReportLab / Typst)  │
└────────────────┬────────────────────────────────────────────────┘
                 ▼
┌─────────────────────────────────────────────────────────────────┐
│  Sandbox Executor  [core/sandbox/*]                              │
│    Docker-based code exec, 内装 LibreOffice + Node + Python 栈   │
│    （复用 code-interpreter 镜像；参见 sandbox_artifacts_working.png）│
└────────────────┬────────────────────────────────────────────────┘
                 ▼
┌─────────────────────────────────────────────────────────────────┐
│  VerifierPipeline  [core/quality/verifier.py]                    │
│    pptx/pdf → soffice→pdftoppm→vision-subagent                   │
│    xlsx     → soffice --calc recalc → reject on #ERROR           │
│    docx     → OOXML validate + auto-repair                       │
│  失败 → 把 findings 喂回 Renderer 做 targeted fix → 再验证        │
└────────────────┬────────────────────────────────────────────────┘
                 ▼
┌─────────────────────────────────────────────────────────────────┐
│  FileArtifactStore  [storage/artifacts.py]                       │
│    存 S3/OSS/local；签名 URL；每个 session 一个 artifact dir      │
│    向前端 SSE 推送：plan → thumbnails → critic-findings → done   │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 设计原则

- **合约即代码**：Planner 和 Renderer 之间永远走 pydantic JSON IR，禁止 LLM 直接吐 OOXML/XML。
- **Skill 作为 procedural knowledge，不做 tool 用**：SKILL.md 是塞进 system prompt 的知识，不是 MCP tool；节省 token。
- **沙箱是一等公民**：渲染、转 PDF、OCR、recalc 都在沙箱里；业务服务本身不装 `weasyprint` / `libreoffice`。
- **验证 > 生成**：一次生成 + 一次视觉验证 + 一次定点修复，比一次"完美生成"更可靠。
- **可流式**：plan、thumbnail、critic findings 都走 SSE 推给前端。
- **可观测**：每次生成写 `ArtifactTrace`（plan + IR + render log + critic JSON + 最终文件 sha256）入库，便于回放。

---

## 4. 关键模块详细设计

### 4.1 Skill 加载器（core/skills/）

**目标**：把 `/mnt/.claude/skills/{docx,pptx,pdf,xlsx}/SKILL.md` 接入 assistant-service。

**新增文件**：
```
apps/assistant-service/src/assistant_service/core/skills/
  __init__.py
  loader.py          # 读 SKILL.md 的 YAML frontmatter + body
  router.py          # 根据 user intent / tool 参数 route 到对应 skill
  registry.py        # 启动时扫描 skills 目录，构建 SkillIndex
  models.py          # Skill, SkillFrontmatter, SkillResource (pydantic)
  renderer_hints.py  # 从 SKILL body 里抽出 renderer 选型建议
```

**核心类**：
```python
class Skill(BaseModel):
    name: str                    # "docx"
    version: str                 # "1.0"
    description: str             # YAML 里的 description
    frontmatter_tokens: int      # ~100
    body_path: Path              # SKILL.md
    resources: list[SkillResource]  # editing.md, pptxgenjs.md, forms.md, ...
    scripts_dir: Path | None     # unpack.py / pack.py / validate.py / ...

class SkillRouter:
    def select(self, intent: Intent) -> Skill | None: ...
    def expand(self, skill: Skill, context_budget: int) -> str:
        """Progressive disclosure: 先 frontmatter，命中后按需加载 body 和 resources。"""
```

**行为**：
- 冷启动扫描 `~/.claude/skills/` 和 `apps/assistant-service/skills/`（项目自有）。
- Agent system prompt 加一段 `<available_skills>`，只包含 frontmatter。
- 当用户意图或 tool 参数触发某个 skill，下一轮 LLM call 把 full SKILL.md body 注入。
- 敏感的 editing.md / pptxgenjs.md 只在明确用到对应分支时才加载。

### 4.2 文档 IR（core/files/ir/）

planner 的输出、renderer 的输入，**一套 schema 覆盖四种格式**。

```python
# core/files/ir/base.py
class DocumentIR(BaseModel):
    doc_type: Literal["docx","pptx","xlsx","pdf"]
    metadata: DocMetadata         # title, author, locale, page_size
    theme: Theme                  # palette, fonts, layout_variant
    content: DocxContent | PptxContent | XlsxContent | PdfContent

class Theme(BaseModel):
    palette: list[HexColor]       # 最少 5 色
    font_primary: FontSpec
    font_heading: FontSpec
    accent_style: Literal["none","underline","left_bar"]  # 默认 none
```

**四种 content**（示例 pptx）：

```python
class PptxSlide(BaseModel):
    layout: Literal["title","title_content","two_col","quote","stat_callout",
                    "icon_row","2x2_grid","halfbleed_image","chart","blank"]
    title: str | None
    body: list[Block]             # Paragraph | Bullet | Table | Image | Chart | Code
    notes: str | None
    visual: VisualSpec | None     # image path | chart spec | shape group

class VisualSpec(BaseModel):
    kind: Literal["image","chart","icon","shape"]
    source: ImageSource | ChartSpec | IconRef
    alt_text: str                 # 强制必填，a11y
```

**设计要点**：
- `Block` 是递归 union，支持嵌套。
- 所有 image 都必须带 `alt_text`（docx-js / a11y 要求）。
- `Table` 的宽度用 DXA（不用百分比），避免 Google Docs 崩。
- `ChartSpec` 独立建模（type, data, axes, series），渲染端再翻译成 pptx/xlsx/matplotlib。

### 4.3 Renderer 分发（core/files/renderers/）

四个 renderer，都实现同一个接口：

```python
class BaseRenderer(Protocol):
    async def render(self, ir: DocumentIR, out_dir: Path) -> RenderResult: ...
    async def fix(self, ir: DocumentIR, critic_findings: CriticReport, out_dir: Path) -> RenderResult: ...
```

**DocxRenderer**
- **主路径**：Node 侧车跑 `docx-js`，生成 .docx → sandbox `validate.py` 校验 + auto-repair。
- **备路径**：pure-Python `python-docx`（兼容老调用）。
- **模板路径**：若 IR 指定 `template_id`，走 `docxtpl`（jinja）。
- **编辑路径**：OOXML unpack → 文本 Edit → pack。

**PptxRenderer**
- **主路径**：Node 侧车跑 `pptxgenjs`；遵循 SKILL 的所有"不要"（无 `#`、无 8 位 hex、无 option 复用）。
- **编辑路径**：unpack `ppt/presentation.xml` 改 `<p:sldIdLst>`；`scripts/add_slide.py` 做复制。
- **图表**：用 pptxgenjs `addChart`；不预生成 PNG（杀死 Office 里的交互）。

**XlsxRenderer**
- **生成**：`openpyxl` 写公式（**绝不 Python 算后写值**）；`pandas` 转批量数据。
- **风格**：强制"财务模型配色约定"（蓝=硬编码输入，黑=公式，绿=跨表引用，红=外部链接，黄底=关键假设）。
- **recalc**：生成后 `soffice --headless --calc --convert-to xlsx` 触发重算；扫描任何 `#REF!/#DIV/0!/#VALUE!/#N/A/#NAME?` → 报给 VerifierPipeline 打回。

**PdfRenderer**
- **默认**：LLM 写 HTML + CSS，沙箱里 Playwright `page.pdf()`。warm pool 预热 Chromium。
- **文件小/静态**：WeasyPrint fallback（不需要 JS 渲染时 10× 小文件）。
- **学术长文档**：Typst + Pandoc 路径（`--pdf-engine=typst`）。
- **低级精确排版**：ReportLab（发票、证书、表单）。
- **表单填写**：pypdf AcroForm；非 fillable 走坐标法 + bbox 预检。

### 4.4 沙箱执行器（core/sandbox/）

**目标**：和现有 `openclaw` / code-interpreter 能力整合，对外暴露一个统一 `SandboxClient`。

```python
class SandboxClient:
    async def exec_python(self, code: str, *, timeout=120, files_in=[], files_out=[]) -> SandboxResult: ...
    async def exec_node(self,  code: str, ...) -> SandboxResult: ...
    async def exec_bash(self,  cmd:  str, ...) -> SandboxResult: ...
```

**镜像要求**（`docker/sandbox.Dockerfile`）：
```
Ubuntu 24.04
+ Python 3.12: python-docx, python-pptx, openpyxl, xlsxwriter,
               reportlab, weasyprint, pypdf, pdfplumber,
               pypdfium2, pdf2image, tabula-py, pandas, matplotlib,
               pillow, markdown, markitdown, docxtpl
+ Node 20: docx (docx-js), pptxgenjs, pdf-lib, pdfjs-dist
+ apt: libreoffice-core libreoffice-writer libreoffice-impress libreoffice-calc
       poppler-utils (pdftoppm, pdftotext)
       tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-chi-tra
       fonts-noto-cjk fonts-dejavu fonts-liberation
       chromium + playwright（已装 chrome）
       typst
+ skills/office scripts: validate.py / unpack.py / pack.py / recalc.py / thumbnail.py
```

**网络策略**：沿用当前 `plugins/` 白名单 egress；禁止任意外联。

### 4.5 验证管线（core/quality/verifier.py）

```python
class VerifierPipeline:
    async def verify(self, artifact: Path, ir: DocumentIR) -> CriticReport: ...

class CriticReport(BaseModel):
    passed: bool
    issues: list[Issue]       # 按 slide / page / cell 粒度
    rerender_targets: list[int]   # 只重渲这几页
```

**三条线**：

1. **PPTX / PDF 视觉线**：
   - sandbox 跑 `soffice --headless --convert-to pdf input.pptx && pdftoppm -jpeg -r 150 input.pdf slide`。
   - 抽样/全量把 JPEG 喂给 **fresh-context vision 子 agent**，prompt 钉死："Assume there are issues. Find overflow, overlap, alignment, contrast, missing images, placeholder text, AI-tells (centered body, accent lines)."
   - 子 agent 输出结构化 `Issue[]`。
   - 主 agent 调 `renderer.fix(ir, critic_findings)`，只对 `rerender_targets` 重新渲染。
   - 硬规则：**至少一轮 fix-and-verify 才能 declare success**（SKILL 原话）。

2. **XLSX 公式线**：
   - sandbox `libreoffice --headless --calc --convert-to xlsx` 触发重算。
   - `openpyxl.load_workbook(..., data_only=True)` 扫所有 cell，任何 `#REF!/#DIV/0!/#VALUE!/#N/A/#NAME?` 收集成 Issue。
   - `recalc.py` 返回 JSON；Renderer 拿到后定位公式 cell 修正。

3. **DOCX XML 线**：
   - `unpack → lxml schema validate → auto-repair → pack`。
   - Grep 占位符（`xxxx`/`lorem`/`ipsum`/`TODO`）。
   - sanity open：`soffice --headless --convert-to pdf output.docx` 能渲染不崩。

### 4.6 Artifact 存储与投递（storage/artifacts.py）

- 每个 `session_id + turn_id` 给一个 artifact dir：`s3://ag-artifacts/{tenant}/{session}/{turn}/…`
- 入库元数据：`(artifact_id, doc_type, size, sha256, trace_id, critic_score, thumbnail_paths[])`
- 前端通过预签名 URL 下载；同时暴露 `/api/v1/artifacts/{id}/thumbnails/{n}` 做 slide 预览。
- 生命周期：匿名 7 天、登录用户 30 天（follow `GATEWAY_SESSION__*_TTL` 惯例）。

### 4.7 流式 UX（api/routes/chat.py 增量）

SSE 事件新增：
```
event: doc.plan        data: { outline: [...] }
event: doc.ir          data: { ir: {...} }                # 调试用，可隐藏
event: doc.thumbnail   data: { page: 1, url: "..." }
event: doc.critic      data: { issues: [...] }
event: doc.fix         data: { targets: [3,7] }
event: doc.done        data: { artifact_url, thumbnails: [...] }
```

对齐 Microsoft Copilot 的"agent mode plan pane"体验。

---

## 5. 分阶段路线图

### Phase 0：地基（Week 1，~3 人日）

**目标**：把沙箱和 skills 接入跑通，拿到一个 hello world。

- [ ] 建 `docker/sandbox.Dockerfile`，预装 LibreOffice + Node + 关键 Python/Node 库；加到 `docker-compose.yml`。
- [ ] 拷贝 `/mnt/.claude/skills/{docx,pptx,pdf,xlsx}` 四个 SKILL 到 `apps/assistant-service/skills/`（项目内稳定版本，带 pin），保留软链回源。
- [ ] 实现 `core/skills/{loader,router,registry,models}.py`；启动时扫描、构建 SkillIndex。
- [ ] `SandboxClient` 最小实现：`exec_python / exec_node / exec_bash`，能往沙箱投递/取回文件。
- [ ] 验收：Agent 拿到用户"帮我生成一个关于 RAG 的 .docx"请求后，能命中 docx skill、加载 body、在沙箱里跑通 `python -c "import docx; ..."` 返回文件。

**Owner**：Claude Code
**Artifacts**：
- `docker/sandbox.Dockerfile`
- `apps/assistant-service/src/assistant_service/core/skills/*.py`
- `apps/assistant-service/src/assistant_service/core/sandbox/client.py`
- `apps/assistant-service/tests/test_skill_router.py`

### Phase 1：IR + Renderer 骨架（Week 2-3，~5 人日）

**目标**：四种文档都能从 IR 一次性跑出来（不带 critic）。

- [ ] `core/files/ir/{base,docx,pptx,xlsx,pdf}.py`：完整 pydantic schema；每个 format 配 10+ 个 unit test 覆盖嵌套 Block。
- [ ] `DocxRenderer`：主路径走 docx-js Node 侧车；`docxtpl` 模板路径；`python-docx` 兼容路径。
- [ ] `PptxRenderer`：pptxgenjs Node 侧车；实现 §4.2 所有 layout 类型。
- [ ] `XlsxRenderer`：openpyxl + pandas；财务模型配色约定硬编码为 helper。
- [ ] `PdfRenderer`：Playwright HTML-to-PDF 为主；WeasyPrint + ReportLab + Typst 三个备路。
- [ ] 新接口：`POST /api/v1/documents/render` 接受 `DocumentIR`，返回 artifact URL。
- [ ] 验收：四种格式各自 e2e demo 能跑，输出文件能在对应 Office 软件里正确打开。

**Artifacts**：
- `apps/assistant-service/src/assistant_service/core/files/ir/*.py`
- `apps/assistant-service/src/assistant_service/core/files/renderers/{docx,pptx,xlsx,pdf}.py`
- `apps/assistant-service/scripts/node_renderers/{docx_render.js,pptx_render.js}`
- `apps/assistant-service/tests/renderers/test_*_roundtrip.py`

### Phase 2：Planner + Skill 驱动（Week 3-4，~4 人日）

**目标**：把 IR 从 agent 的 tool_call 参数里"挤出来"——用独立 Planner LLM 调用生成。

- [ ] `core/agent/planners/{docx,pptx,xlsx,pdf}_planner.py`：每个 planner 有自己的 system prompt（来自 SKILL.md + §5 内置规则）。
- [ ] 重构现有 `document_generator_tool` / `pptx_generator_tool`：tool schema 精简为 `{intent, title, goal, style_hints?}`；内部 pipeline = Planner → IR → Renderer。
- [ ] 加 `style_guide.py`：10 套色板 + 8 种字体对；"don't" 规则（无 accent line、无 unicode bullet、无 centered body）。
- [ ] SSE 事件 `doc.plan` 推给前端。
- [ ] 验收：同一个 prompt（如 "给我一个 30 页的 AI Agent 架构 PPT"）产出的 IR 稳定、可 diff；更换 style_hints 能让配色改变但结构稳定。

**Artifacts**：
- `apps/assistant-service/src/assistant_service/core/agent/planners/*.py`
- `apps/assistant-service/src/assistant_service/core/files/style_guide.py`
- `apps/assistant-service/tests/planners/*.py`

### Phase 3：Verifier + 自动修复（Week 4-5，~5 人日）⚡最高 ROI

**目标**：上线"渲染成图→vision critic→fix 循环"。

- [ ] `core/quality/verifier.py`：
  - `PptxPdfVisualVerifier`：soffice→pdftoppm→vision 子 agent；子 agent 用独立 Anthropic SDK client，fresh context，prompt 钉死"Assume there are issues"。
  - `XlsxFormulaVerifier`：soffice recalc + openpyxl data_only 扫描。
  - `DocxXmlVerifier`：lxml schema + placeholder grep + sanity open。
- [ ] `core/files/renderers/*` 全部实现 `fix(ir, critic_findings)`：根据 issues 只修改 IR 中对应片段，只重渲受影响页。
- [ ] 硬规则：没跑过至少一轮 fix-verify 禁止返回。
- [ ] SSE 事件 `doc.critic` / `doc.fix` 推给前端。
- [ ] 金标集：收集 30 个"真实用户 prompt"（来自 `AI-Assistant-Interview-TechReport-2026-04-09.md`），建 snapshot 测试。
- [ ] 验收：对金标集，critic 命中率 ≥ 90%；两轮 fix 后通过率 ≥ 95%。

**Artifacts**：
- `apps/assistant-service/src/assistant_service/core/quality/verifier.py`
- `apps/assistant-service/src/assistant_service/core/quality/critic_agent.py`
- `apps/assistant-service/tests/golden/doc_prompts/*.yaml`

### Phase 4：Artifact store + 流式 UX + 模板库（Week 5-6，~4 人日）

**目标**：产品化收尾。

- [ ] `storage/artifacts.py`：S3/OSS/local 三后端，跟随 `FILE_STORAGE_BACKEND` env。
- [ ] 前端 SSE 对接：先展示 plan 大纲 → slide 缩略图滚动出现 → critic 发现的问题（可折叠） → 最终下载链接。复用 `web/` 里现有的流式组件。
- [ ] 品牌模板库 `apps/assistant-service/templates/{docx,pptx}/`：至少 3 套（默认、极简、企业深色）。支持租户自定义上传。
- [ ] `/api/v1/documents/templates` CRUD。
- [ ] 验收：前端全链路跑通；模板切换生效；多租户隔离通过 `test_tenant_isolation`。

**Artifacts**：
- `apps/assistant-service/src/assistant_service/storage/artifacts.py`
- `apps/assistant-service/src/assistant_service/api/routes/documents.py`
- `apps/assistant-service/templates/**`
- `web/src/components/DocumentGenerationStream.tsx`（或等价）

### Phase 5（可选）：编辑 / 表单 / 长文档高级能力（Week 7+）

- [ ] DOCX 编辑：OOXML unpack → 文本 Edit → pack；tracked changes（`<w:ins>` / `<w:del>`）；评论。
- [ ] PDF 表单：AcroForm 自动填；坐标 fallback + bbox 预检。
- [ ] 学术长文档：Typst + Pandoc 路径，支持参考文献、交叉引用。
- [ ] PPT 主题继承：上传一个参考 .pptx → 抽取 palette/master → 新生成内容自动匹配。

---

## 6. 库选型矩阵（决策表）

| 需求 | 首选 | 备选 | 拒绝 |
|---|---|---|---|
| DOCX 从零生成 | `docx-js` (Node) | `python-docx` | 手写 OOXML |
| DOCX 模板填充 | `docxtpl` (jinja) | python-docx-template | — |
| DOCX 精细编辑 | OOXML unpack + 文本 Edit + pack | — | python-docx（丢样式） |
| DOCX 校验 | SKILL `validate.py` (lxml) | — | — |
| PPTX 从零生成 | `pptxgenjs` (Node) | `python-pptx` | HTML→LibreOffice→pptx（作备用） |
| PPTX 编辑现有 | unpack OOXML + `scripts/add_slide.py` | — | python-pptx 直接改（rel 丢失） |
| PPTX 视觉 QA | soffice→pdftoppm→vision-subagent | — | 纯文本 grep |
| XLSX 生成 | `openpyxl` + `pandas` | `xlsxwriter`（写入快，不可编辑） | python 算后写死值 |
| XLSX 重算 | `soffice --headless --calc` | `formulas` / `pycel` | 信任 openpyxl 缓存 |
| PDF（JS/chart 多） | Playwright `page.pdf()` | WeasyPrint | wkhtmltopdf（退场中） |
| PDF（长静态） | WeasyPrint | Typst + Pandoc | — |
| PDF（学术） | Typst + Pandoc | XeLaTeX | — |
| PDF（发票/表单式） | ReportLab Platypus | — | — |
| PDF 合并拆分 | `pypdf` | — | — |
| PDF 表单填写 | `pypdf` AcroForm → 坐标 fallback | `pdfforms` | — |
| PDF 解析 RAG | Marker（默认）/ MinerU（CJK/学术）/ Unstructured（表重） | pdfplumber | pypdf（会丢布局） |
| 图表（pptx/xlsx） | 各自原生 chart API | matplotlib → PNG 插入 | — |
| HTML→PDF 字体 | bundled Noto CJK + DejaVu + Liberation | — | 默认系统字体 |
| OCR | tesseract + `pdf2image` | — | — |

---

## 7. 测试与质量

- **单测**：IR schema round-trip、每个 Renderer 的 minimum viable output。
- **快照测**：30 个 golden prompt 生成的文件 → `diffoscope` 比对（忽略时间戳 / ID）。
- **人类抽检**：每次回归随机抽 5 个 PPT/PDF 给 vision critic 打 0-10 分；指标入库。
- **压测**：并发 20 个文档生成，验证沙箱池和 artifact store 不挂。
- **多语言/CJK 护城河**：固定包含 "東京 π √ 🚀 αβγ 测试" 的 canary prompt，每次跑都渲染一次。
- **a11y**：解析输出 .docx/.pptx → 确认所有 image 都有 alt_text；颜色对比度 ≥ WCAG AA。

---

## 8. 与现有代码的迁移策略

1. **新旧并存期**：先把新 pipeline 挂在新路由 `/api/v1/documents/*`，老的 `generate_document` / `generate_pptx` tool 保持不动。
2. **流量灰度**：加一个 feature flag `DOC_GEN_MODE=v2|v1`，默认 v1。
3. **逐渐替换**：v2 稳定后把老 tool 内部实现转发到新 pipeline；两周观察期后删老代码。
4. **guardrails**：`quality/guardrails.py` 的字数阈值做为新 pipeline 的 **sanity check 兜底**（不是主验证），避免退化。
5. **配置迁移**：`pyproject.toml` 加 `[project.optional-dependencies] documents-v2 = [...]`；生产镜像用 sandbox image。

---

## 9. 风险与应对

| 风险 | 影响 | 应对 |
|---|---|---|
| Node 侧车进程管理复杂 | 故障面大 | 复用沙箱做一次性 exec；不常驻进程 |
| Vision critic 成本高 | $$ | 仅对 PPT/PDF 抽帧（每 3 页 1 帧 + 所有含图页），非全量 |
| LibreOffice headless 不稳定 | recalc/convert 偶发失败 | `tenacity` 重试 3 次；失败降级到 openpyxl data-only |
| Playwright 容器体积大 | 镜像 >2 GB | 分层 build；按需路径，lazy pull |
| Skill 内容漂移（上游更新） | 本地 pin 版本过旧 | CI 每周跑 `skills-lock.json` 更新 PR |
| Chinese/CJK 字体回退 | 中文缺字显示方块 | 强制 bundle Noto CJK；构建期跑 canary 渲染 |
| 多租户 artifact 串流 | 安全 | artifact URL 走租户签名；`test_tenant_isolation.py` 扩测 |
| Critic 误判把好 PPT 打回 | 产品体验 | 加 `max_fix_rounds=2` 上限；critic 置信度 <0.6 时不触发修复 |

---

## 10. 成功指标（ship 后 4 周内观测）

- **一次性生成通过率**（无 critic 时就合格）：DOCX ≥ 80%、XLSX ≥ 75%、PPTX ≥ 55%、PDF ≥ 70%。
- **两轮 fix 后通过率**：≥ 95%（全格式）。
- **金标集 vision critic 评分均值**：≥ 8/10。
- **P95 端到端生成延迟**：30 页 PPT ≤ 60s；20 页 PDF ≤ 30s；1 MB XLSX ≤ 20s；50 页 DOCX ≤ 40s。
- **用户主动下载率**（生成 → 下载的漏斗）：≥ 70%（当前约 40%，来自 dashboard 埋点）。

---

## 11. 给 Claude Code 的执行建议

1. **按 Phase 顺序推进**，每个 Phase 结束都打 tag、跑一次金标集回归。
2. **先写 IR schema，再写 Renderer**。schema 稳定之前不要批量写 prompt，planner 会跟着来回改。
3. **Vision critic 必须用 fresh Anthropic client（独立 API key 或独立 conversation）**，不能复用主 agent 的 context——Anthropic SKILL 原话"fresh eyes"。
4. **所有渲染/转换走沙箱**，业务容器永远不要装 LibreOffice/Playwright。
5. **不允许 LLM 直接写 OOXML**。发现 planner 尝试绕过 IR 就立即 revert。
6. **SKILL.md 内容不要复制进 system prompt**，要通过 SkillRouter 按需注入；否则 token 爆炸。
7. **每一步都写 trace**：IR、render log、critic JSON 全部落盘到 `data/traces/{trace_id}/`，方便事后回放调参。
8. **CJK canary 渲染**加进 CI；没过就 block merge。
9. **把 `Gateway-Optimization-Plan-2026-04-16.md` 里关于沙箱的章节对齐一遍**——两边共用同一个 sandbox image。

---

## 附录 A：关键参考链接

- Anthropic skills 仓库 <https://github.com/anthropics/skills>
  - `docx/SKILL.md`、`pptx/SKILL.md`、`xlsx/SKILL.md`、`pdf/SKILL.md`（本地也有副本）
- Anthropic Agent Skills overview <https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview>
- Anthropic 代码执行工具 <https://platform.claude.com/docs/en/agents-and-tools/tool-use/code-execution-tool>
- Anthropic 工程博客"Equipping agents for the real world with Agent Skills" <https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills>
- Microsoft 365 Copilot Agents 架构 <https://techcommunity.microsoft.com/blog/microsoft365copilotblog/introducing-word-excel-and-powerpoint-agents-in-microsoft-365-copilot/4470604>
- Gemini in Sheets @ SpreadsheetBench <https://blog.google/products-and-platforms/products/workspace/gemini-google-sheets-state-of-the-art/>
- PPTAgent (EMNLP 2025) <https://arxiv.org/abs/2501.03936> | repo <https://github.com/icip-cas/PPTAgent>
- SlideCoder (2025) <https://arxiv.org/abs/2506.07964>
- HTML→PDF 基准（Playwright vs WeasyPrint） <https://pdf4.dev/blog/html-to-pdf-benchmark-2026>
- Typst + Pandoc <https://slhck.info/software/2025/10/25/typst-pdf-generation-xelatex-alternative.html>
- PDF→Markdown 2026 对比（Marker / Docling / MinerU / PyMuPDF4LLM） <https://themenonlab.blog/blog/best-open-source-pdf-to-markdown-tools-2026>
- python-pptx 已知溢出 bug #715 / #969 / #973 <https://github.com/scanny/python-pptx/issues/969>
- docxtpl 文档 <https://docxtpl.readthedocs.io/>

---

## 附录 B：现状文件速查

| 现状文件 | 升级后的归宿 |
|---|---|
| `src/services/assistant/tools/document_generator_tool.py` | 拆为 `core/agent/planners/docx_planner.py` + `core/files/renderers/docx.py`；老 tool 变薄壳转发 |
| `src/services/assistant/tools/pptx_generator_tool.py` | 同上，pptx 分支 |
| `src/services/assistant/quality/guardrails.py` | 降级为兜底 sanity check；主验证权重转给 `core/quality/verifier.py` |
| `apps/assistant-service/src/assistant_service/core/files/__init__.py` | 扩充为 IR + Renderers 主目录 |
| `apps/assistant-service/src/assistant_service/core/skills/__init__.py` | 实装 Skill loader/router/registry |
| `apps/assistant-service/src/assistant_service/core/openclaw/__init__.py` | 与新 SandboxClient 合并或互相调用 |
| `apps/knowledge-service/.../document_processor.py` | **不动**（它是 ingest 侧，读 PDF/DOCX；和生成侧正交） |

---

**END — Ready for Claude Code.**
