# Islamic Content Service — 完整 API 清单 + 缺口审计

**更新时间**：2026-04-23
**对应代码版本**：`dev` 分支（commit `1a7b4a0`，已部署生产）
**基础地址**：`http://52.65.136.42:8091/api/v1`
**Swagger**：`http://52.65.136.42:8091/docs`
**部署状态**：✅ 生产已上线，18 个新端点全部 200 响应

---

## -1. 数据完整度（生产 DB 实测 · 2026-04-23 06:34 UTC）

数据层**全部满配**，所有新端点都能返回真实数据，不会触发 `NotReadyError`。

### Quran —— canonical 完整

| 项 | 数量 | 说明 |
|---|---|---|
| Chapters | **114** | 全 canonical |
| Ayahs | **6,236** | 全 canonical |
| 逐词 words | 77,429 | word-by-word 完整 |
| 同步翻译 | **145 个** | en/ar-tafsir/zh/ur/tr/ms/id 全覆盖 |
| 同步诵读人 | **12 个** | Mishary / Sudais / Minshawi 等 |
| 节级音频 URL | 74,832 | = 6,236 × 12 |
| 章级音频 track | 1,368 | = 114 × 12 |
| 字级时间轴 | 74,831 | 逐字对齐（word-level segments） |
| Triplet 块（AI Quran） | 2,115 | 3 句一组 |

### Hadith —— 3 级层次结构 100% 覆盖（2026-04-22 刚完成重构）

| Collection | Items | Books | Chapters | Orphans |
|---|---|---|---|---|
| bukhari | 7,563 | 98 | 3,738 | **0** |
| muslim | 7,205 | 56 | 1,334 | **0** |
| abudawud | 5,274 | 43 | 1,884 | **0** |
| nasai | 5,758 | 52 | 2,531 | **0** |
| tirmidhi | 3,956 | 49 | 2,224 | **0** |
| ibnmajah | 4,341 | 38 | 1,494 | **0** |
| nawawi（40 Hadith） | 42 | 1 | 1 | **0** |
| **合计** | **34,139** | **338** | **13,206** | **0** |

- **67,937 条 localizations**（en + ar 双语全覆盖） — `/hadith/search` 的数据基础
- **73,460 条 grades**（sahih / hasan / daif 评级） — 为未来 grade-filter 端点就绪
- **0 orphan** —— 每条 hadith 都挂到 book + chapter

### Dua —— 满配

| 项 | 数量 |
|---|---|
| Categories | 31 |
| Duas | 72 |

全部带 `category` / `occasion` / `authenticity` / `source` —— `/dua/search` 和 `/dua/by-occasion` 新端点全可用。

### 同步健康度

- `source_sync_runs` = **76 条历史记录**（最近四周持续增量 sync，数据不冷）
- Redis DB 1 部署后已 FLUSHDB，前端拿到的是最新响应（不会命中旧缓存）

**一句话**：三大数据都已存好、更新好，新端点接上即可，客户端/第三方直接调用就行。

---

## 0. 本轮新增（2026-04-23）—— Juz + P0 + P1 全部完成 ✅

**代码侧新增 18 个端点，单元测试全部通过（23 passed / 1 skipped）。**

### P0（阻断型 — 不做 app 跑不起来）

| # | 方法 | 路径 | 说明 |
|---|---|---|---|
| 1 | GET | `/api/v1/quran/juzs` | 30 个 Juz 列表 |
| 2 | GET | `/api/v1/quran/juzs/{n}` | 单 Juz 摘要 |
| 3 | GET | `/api/v1/quran/juzs/{n}/ayahs` | 整个 Juz 的所有 ayah（Continue reading 用） |
| 4 | GET | `/api/v1/quran/search?q=&translation_id=` | Quran 全文搜索 |
| 5 | GET | `/api/v1/hadith/search?q=&lang=&collection=` | Hadith 全文搜索 |
| 6 | GET | `/api/v1/dua/search?q=` | Dua 全文搜索 |
| 7 | GET | `/api/v1/dua/occasions` | 场景清单（做 filter chip 用） |
| 8 | GET | `/api/v1/dua/by-occasion/{occasion}` | 按场景过滤 |

### P1（常用体验增强）

| # | 方法 | 路径 | 说明 |
|---|---|---|---|
| 9 | GET | `/api/v1/quran/chapters/{id}` | 单 Surah 元信息（不含 ayahs） |
| 10 | GET | `/api/v1/quran/ayahs/random` | Ayah of the Day |
| 11 | GET | `/api/v1/quran/ayahs/range?from=&to=` | 批量取经文（跨章节） |
| 12 | GET | `/api/v1/quran/pages/{n}` | Mushaf 第 N 页（1..604） |
| 13 | GET | `/api/v1/quran/sajdahs` | 15 处 Sajdah 节点（硬编码） |
| 14 | GET | `/api/v1/quran/hizbs` | 60 个 Hizb（半-Juz） |
| 15 | GET | `/api/v1/hadith/random?collection=` | Hadith of the Day |
| 16 | GET | `/api/v1/hadith/collections/{name}` | 单 collection 元信息 |
| 17 | GET | `/api/v1/hadith/collections/{name}/hadiths/{n}/context` | 上一条 / 下一条 |
| 18 | GET | `/api/v1/dua/random` | Dua of the Day |

---

## 0.1 原 Juz 列表设计（保留）

### Quran — Juz 列表 & 详情

截图里那种 “Juz 1 · Start at Al-Fatihah 1” 的列表，以前没有对应 API（`juz_number` 只是 ayah 表上的一列），客户端必须自己聚合。现在加了：

- `GET /api/v1/quran/juzs` — 返回 30 个 Juz 的摘要
- `GET /api/v1/quran/juzs/{juz_number}` — 单个 Juz 详情

**字段映射（截图 → 返回）：**

| 截图显示 | 取 JSON 字段 |
|---|---|
| `Juz 1` | `juz_number` |
| 大字标题（阿语开篇词） | `name_arabic` / `name_transliteration` |
| `Start at Al-Fatihah 1` | `start_chapter_name_simple` + `start_ayah_number` |
| 起止（内部用） | `first_verse_key` / `last_verse_key` |
| 某 Juz 内每章节覆盖范围 | `verse_mapping`（例：`{"1":"1-7","2":"1-141"}`） |

**响应示例：**

```json
GET /api/v1/quran/juzs
{
  "generated_at": "2026-04-23T03:00:00+00:00",
  "screen": "quran_juzs",
  "source_api": "quran.foundation+internal",
  "juzs": [
    {
      "juz_number": 1,
      "name_arabic": "الم",
      "name_simple": "Alif Lam Mim",
      "name_transliteration": "Alif Lām Mīm",
      "first_verse_key": "1:1",
      "last_verse_key": "2:141",
      "start_chapter_id": 1,
      "start_chapter_name_simple": "Al-Fatihah",
      "start_chapter_name_arabic": "ٱلْفَاتِحَة",
      "start_ayah_number": 1,
      "verses_count": 148,
      "verse_mapping": {"1": "1-7", "2": "1-141"}
    }
  ]
}
```

**实现要点（和其它端点不同）：**

- 不加表、不加迁移，`juz_number` 是 `quran_ayahs` 原有列，`list_juz_summaries()` 用一个 CTE 聚合（MIN/MAX/COUNT + 按 chapter 分 mapping）。
- 30 个 Juz 的阿语开篇词 **upstream Quran Foundation 不返回**，是全球穆斯林固定常量，存在 `domain/juz_data.py` 硬编码。
- 缓存走 `cache_settings.meta_ttl_seconds`（因为 30 条数据永不变化，没必要用短 TTL）。
- 失败态返回 503 `NotReadyError`，和其它 Quran 端点一致。

**单元测试**：`tests/api/test_quran_api.py::test_quran_endpoints`（覆盖列表 + 单条）。

---

## 1. 全量 API 清单（14 Quran + 5 Hadith + 4 Dua + Meta + Wahda + Quran User）

### 1.1 Quran（`/api/v1/quran/*`）—— 14 个

| Method | Path | 用途 |
|---|---|---|
| GET | `/quran/chapters` | 114 个 Surah 列表 |
| GET | `/quran/resources/translations` | 已同步的翻译资源列表 |
| GET | `/quran/resources/recitations` | 已同步的诵经人列表 |
| GET | **`/quran/juzs`** | **🆕 30 个 Juz 列表 + 名称** |
| GET | **`/quran/juzs/{juz_number}`** | **🆕 单个 Juz 详情** |
| GET | `/quran/chapters/{chapter_id}/ayahs` | 整章经文 + 翻译 + 音频 |
| GET | `/quran/chapters/{chapter_id}/audio-text` | 整章经文 + 音频对齐（带例子） |
| GET | `/quran/chapters/{chapter_id}/triplets` | AI Quran 用的 3 句一组 |
| GET | `/quran/chapters/{chapter_id}/translations` | 只要翻译 |
| GET | `/quran/chapters/{chapter_id}/audio` | 整章音频 track + verse/word 时间轴 |
| GET | `/quran/ayahs/{verse_key}` | 单 ayah 完整 |
| GET | `/quran/ayahs/{verse_key}/minimal` | 单 ayah 极简文本 |
| GET | `/quran/ayahs/{verse_key}/translation` | 单 ayah 翻译文本 |

**Quran User（OAuth，依赖 `user-enabled` 权限，未打通）：**

| Method | Path |
|---|---|
| GET | `/quran/user/auth/*` |
| GET | `/quran/user/userinfo` |
| GET | `/quran/user/request` |

### 1.2 Hadith（`/api/v1/hadith/*`）—— 5 个

| Method | Path | 用途 |
|---|---|---|
| GET | `/hadith/collections` | 7 个 collection |
| GET | `/hadith/collections/{name}/books` | 某 collection 的所有 book |
| GET | `/hadith/collections/{name}/books/{book_number}/chapters` | 某 book 的章节层（2026-04-22 新增） |
| GET | `/hadith/collections/{name}/books/{book_number}/hadiths` | 分页 hadith 列表 |
| GET | `/hadith/collections/{name}/hadiths/{hadith_number}` | 单条 hadith 详情（含 grade） |

### 1.3 Dua（`/api/v1/dua/*`）—— 4 个

| Method | Path | 用途 |
|---|---|---|
| GET | `/dua/categories` | 31 个 category |
| GET | `/dua/categories/{category}` | 某 category 下的所有 dua |
| GET | `/dua/items` | 72 条 dua 全量 |
| GET | `/dua/{dua_id}` | 单条 dua 详情 |

### 1.4 Meta & Wahda

- `/meta/canonical-summary` · `/meta/config` · `/meta/manifest`
- `/wahda/*`（Hejaz-only 内部端点，不对第三方）

---

## 2. 缺口审计 —— 三大数据各缺什么

> 下面 P0 四条**全部已实现**，表格里保留是为了对照；剩余 P1/P2 仍是下一轮候选。

---

> 维度：一个普通 Islamic app 上架能跑起来 **还差什么 API**。
> **P0** = 不加不能做；**P1** = 有就 SOTA；**P2** = 需要上游/同步新数据。

### 2.1 Quran（缺 10 个）

| 优先级 | 端点 | 为什么缺 / 数据是否已有 |
|---|---|---|
| **P0 ✅** | `GET /quran/juzs/{n}/ayahs` | 截图里"Continue where you left off Juz 27 Verse 31"直接要这个数据。现在客户端必须先 `/juzs` 拿到 `verse_mapping`，再对每个章节调 `/chapters/{id}/ayahs` 自己裁。✅ 数据完全在 `quran_ayahs`（有 `juz_number` 列），直接 WHERE juz_number=X 即可。|
| **P0 ✅** | `GET /quran/search?q=&translation_id=` | app 搜索栏必备。DB 已有 `arabic_text` / `transliteration_text` / `quran_ayah_translations.translation_text`，ILIKE 或后续加 BM25 都可以。|
| **P1 ✅** | `GET /quran/chapters/{id}` | 只有列表，没有"仅取 Al-Fatihah 元信息"。现在详情页只能调 ayahs 全量。|
| **P1 ✅** | `GET /quran/ayahs/random` | 首屏 Ayah of the Day。一句 `ORDER BY random() LIMIT 1`。|
| **P1 ✅** | `GET /quran/pages/{page_number}` | Mushaf 604 页浏览。`page_number` 列已有。|
| **P1 ✅** | `GET /quran/ayahs/range?from=1:1&to=2:5` | 批量取多节（笔记、喜欢列表、分享卡片）。|
| **P1 ✅** | `GET /quran/sajdahs` | 14 处 sajdah 节点。**DB 没这个数据**，需硬编码（类似 juz 名称做法）。|
| **P1 ✅** | `GET /quran/chapters/{id}/hizbs` 或 `GET /quran/hizbs` | 60 个 Hizb / 半 Juz。`hizb_number` 列已有，可直接聚合。|
| **P2** | Quran User bookmarks / notes / progress | 路由占位已有（`/quran/user/*`），但 Quran Foundation 要 `user-enabled` 额外权限，目前没打通。|
| **P2** | Ruku 切分 / Manzil 切分 | 南亚 mushaf 常用。上游 Quran Foundation 未同步。|

### 2.2 Hadith（缺 6 个）

| 优先级 | 端点 | 为什么缺 / 数据是否已有 |
|---|---|---|
| **P0 ✅** | `GET /hadith/search?q=&lang=&collection=` | 没有搜索。DB 有 `hadith_localizations.body_text`，可以 ILIKE + pagination。|
| **P1 ✅** | `GET /hadith/random` | Hadith of the Day。|
| **P1 ✅** | `GET /hadith/collections/{name}` | 没有"单个 collection 元信息"端点。列表包含但要拉 7 条都是浪费。|
| **P1 ✅** | `GET /hadith/collections/{name}/hadiths/{n}/context` | 上一条 / 下一条，阅读流用。|
| **P1** | `GET /hadith/filter?grade=sahih&topic=fasting` | `hadith_grades` 表已经有 grade，但没有按 grade 过滤的路由。|
| **P2** | 叙事链 / narrator metadata | `isnad` / 传述人链条 —— 我们现在完全没爬。大工程，需先决定数据源（sunnah.com 没有机器可读版本）。|

### 2.3 Dua（缺 5 个）

| 优先级 | 端点 | 为什么缺 / 数据是否已有 |
|---|---|---|
| **P0 ✅** | `GET /dua/search?q=` | 没搜索。`dua_items.title` / `english_meaning` / `arabic_text` 都可搜。|
| **P0 ✅** | `GET /dua/by-occasion/{occasion}` + `GET /dua/occasions` | DB 有 `occasion` 列（morning / evening / travel / eating 等），只能按 category 过滤，按场景过滤做不了。|
| **P1 ✅** | `GET /dua/random` | 首屏 Dua of the Day。|
| **P1** | category 元信息（icon / 排序 / 描述） | 目前 `dua_categories` 只有 `category` + `dua_count`，前端做分类图标只能硬编码。|
| **P2** | 多语种 meanings | 表结构固定死了 `english_meaning` / `urdu_meaning` 两列，加印尼 / 马来 / 土耳其得改表结构 + 补数据。|

### 2.4 跨模块（3 条观察，非必做）

1. **没有统一 `/search?q=`** 跨 Quran/Hadith/Dua 一把搜 —— 不过 KB 向量搜索已经干这个活儿，重复。
2. **Bookmarks / Favorites 是割裂的** —— Quran 有 user 路由占位，Hadith / Dua 完全没有用户侧。如果要做"跨内容收藏夹"需要单独设计 `user_bookmarks` 表。
3. **I18n 不统一** —— Quran 靠 `translation_id` 参数化；Hadith 用 `hadith_localizations.language`；Dua 是硬编码列。三套玩法对前端不友好，将来想收口建议走 Hadith 的 localizations 模式。

---

## 3. 下一步建议（按投入/回报排序）

| 事项 | 工作量 | 回报 |
|---|---|---|
| 加三个 `/search` 端点（Quran/Hadith/Dua） | 各 ~2h，都是 ILIKE + paging | 🔥 最高，app 一定用 |
| 加 `/quran/juzs/{n}/ayahs` | ~30min，纯 WHERE 过滤 | 🔥 高，配合今天的 Juz 列表闭环 |
| 加 `/dua/by-occasion/{o}` | ~30min | 高，DB 列现成 |
| 三个 `/random` 端点 | 各 ~15min | 中，首屏 UX |
| 加 `/quran/chapters/{id}` 详情 | ~15min | 中 |
| `/quran/sajdahs` 硬编码 | ~30min，参照 juz_data.py | 中，诵读页必备 |
| `/quran/pages/{n}` | ~20min，`page_number` 列已有 | 中 |
| Hadith `/random` + grade-filter | ~1h | 中 |

**短期推荐闭环（4 个 P0）**：
1. `/quran/juzs/{n}/ayahs`（补今天 Juz 列表的缺半）
2. `/quran/search`
3. `/hadith/search`
4. `/dua/search` + `/dua/by-occasion`

写完这批，前端做一个"类 Quran.com + Muslim Pro" 级别的 app 就没有后端阻塞了。

---

## 4. 运维 / 踩坑回顾（不变部分，和 `reference_islamic_content_service` memory 一致）

- **docker compose 服务名是 `islamic-content`（不是 `islamic-content-service`）**，build/up 拼错会静默 no-op
- **Redis DB 是 1（不是 0）**，flush 缓存要 `redis-cli -n 1 FLUSHDB`
- **Schema 是 `islamic_content`（不是 `public`）**，psql 查询要带 schema 前缀或 `SET search_path`
- **代码改完必须 rebuild**，`docker compose restart` 对 rebuild-mode 服务没用
- **Muslim 用的是 sunnah.com 现代编号**（11a/11b），其它 6 个 collection 是 fawazahmed0 经典编号
