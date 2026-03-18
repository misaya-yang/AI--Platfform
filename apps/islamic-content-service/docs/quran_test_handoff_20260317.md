# Islamic Content Service 测试交接文档

更新时间：2026-03-17
验证人：Claude（自动化全链路校验通过）

---

## 1. 测试范围

本轮测试覆盖 **Quran + Dua** 两个模块。

| 模块 | 状态 | 接口数 | 落库 |
|------|------|--------|------|
| Quran | **可测试** | 11 | 6,236 ayahs + 77,429 words + 2,115 triplets |
| Dua | **可测试** | 4 | 72 duas / 31 categories |
| Hadith | **等待凭证** | 4 | 0（无 SUNNAH_API_KEY） |
| Quran User OAuth | **未启用** | 0 | — |

### 1.1 可测试的接口（共 18 个）

**基础 & 元数据（3 个）**

| 序号 | 接口 | 说明 |
|------|------|------|
| 1 | `GET /health` | 基本存活检查 |
| 2 | `GET /health/ready` | 就绪检查（含模块状态） |
| 3 | `GET /api/v1/meta/canonical-summary` | 落库数据统计 |

**Quran 接口（11 个）**

| 序号 | 接口 | 说明 |
|------|------|------|
| 4 | `GET /api/v1/quran/chapters` | 114 章列表 |
| 5 | `GET /api/v1/quran/resources/translations` | 可用翻译列表（145 个） |
| 6 | `GET /api/v1/quran/resources/recitations` | 可用诵读者列表（12 个） |
| 7 | `GET /api/v1/quran/ayahs/{verse_key}/minimal` | 极简三段式 |
| 8 | `GET /api/v1/quran/ayahs/{verse_key}` | 完整 ayah 详情 |
| 9 | `GET /api/v1/quran/ayahs/{verse_key}/translation` | 翻译文本 |
| 10 | `GET /api/v1/quran/chapters/{chapter_id}/ayahs` | 整章所有 ayah |
| 11 | `GET /api/v1/quran/chapters/{chapter_id}/triplets` | 三段式分组 |
| 12 | `GET /api/v1/quran/chapters/{chapter_id}/audio-text` | 音频 + 文本 + 时间轴一体 |
| 13 | `GET /api/v1/quran/chapters/{chapter_id}/audio` | 章级音频 + verse timing |

**Dua 接口（4 个）**

| 序号 | 接口 | 说明 |
|------|------|------|
| 14 | `GET /api/v1/dua/categories` | 所有分类列表 |
| 15 | `GET /api/v1/dua/categories/{category}` | 某分类下所有 Dua |
| 16 | `GET /api/v1/dua/items` | 全部 72 条 Dua |
| 17 | `GET /api/v1/dua/{dua_id}` | 单条 Dua 详情 |

**Hadith 接口（4 个，待凭证后测试）**

| 序号 | 接口 | 说明 |
|------|------|------|
| 18 | `GET /api/v1/hadith/collections` | 集合列表 |
| 19 | `GET /api/v1/hadith/collections/{name}/books` | 书列表 |
| 20 | `GET /api/v1/hadith/collections/{name}/books/{num}/hadiths` | 列表 |
| 21 | `GET /api/v1/hadith/collections/{name}/hadiths/{num}` | 详情 |

---

## 2. 测试前提

### 2.1 服务启动

```bash
cd apps/islamic-content-service
conda run -n ai_gateway python -m islamic_content_service.main
```

默认端口：**8091**
Swagger 文档：`http://{host}:8091/docs`

### 2.2 数据就绪确认

访问 `GET /health/ready`，确认：

```json
{
  "status": "ready",
  "modules": {
    "quran":  { "status": "ready" },
    "dua":    { "status": "ready" },
    "hadith": { "status": "missing_credentials" }
  },
  "backends": {
    "database": "ready",
    "cache": "ready"
  }
}
```

- `status` = `"ready"`
- `modules.quran.status` = `"ready"`
- `modules.dua.status` = `"ready"`
- `modules.hadith.status` = `"missing_credentials"`（预期，不是 bug）

### 2.3 基线数据量

| 数据项 | 预期数量 |
|--------|----------|
| **Quran** | |
| quran_chapters | 114 |
| quran_ayahs | 6,236 |
| quran_words | 77,429 |
| quran_translations（资源数） | 145 |
| quran_recitations（诵读者数） | 12 |
| quran_ayah_translations（翻译变体） | 904,220 |
| quran_ayah_audio（音频变体） | 74,832 |
| quran_chapter_audio_tracks | 1,368 |
| quran_audio_timings | 74,831 |
| quran_triplet_ranges | 2,115 |
| **Dua** | |
| dua_categories | 31 |
| dua_items | 72 |
| **Hadith** | |
| hadith_collections | 0 |
| hadith_items | 0 |

### 2.4 默认资源

- 默认翻译：`translation_id = 20`（Saheeh International）
- 默认诵读：`recitation_id = 7`（Mishari Al-Afasy, Murattal）

---

## 3. 快速冒烟

```bash
cd apps/islamic-content-service

# 列出所有接口及参数
python scripts/test_public_api.py --list

# Quran 一键冒烟
python scripts/test_public_api.py --smoke

# Dua 手动冒烟（30 秒完成）
curl -s http://localhost:8091/api/v1/dua/categories | python3 -m json.tool
curl -s http://localhost:8091/api/v1/dua/categories/Sleep | python3 -m json.tool
curl -s http://localhost:8091/api/v1/dua/DUA-0001 | python3 -m json.tool
curl -s http://localhost:8091/api/v1/dua/items | python3 -m json.tool
```

---

## 4. Quran 接口测试明细

### 4.1 `GET /health`

```
GET http://{host}:8091/health
```

预期：`200`
```json
{ "status": "healthy", "service": "islamic-content-service" }
```

---

### 4.2 `GET /health/ready`

```
GET http://{host}:8091/health/ready
```

预期：`200`

验证点：
- [ ] `status` = `"ready"`
- [ ] `modules.quran.status` = `"ready"`，`counts.primary` = `6236`
- [ ] `modules.dua.status` = `"ready"`，`counts.primary` = `72`
- [ ] `modules.hadith.status` = `"missing_credentials"`（预期）
- [ ] `backends.database` = `"ready"`
- [ ] `backends.cache` = `"ready"`

---

### 4.3 `GET /api/v1/meta/canonical-summary`

```
GET http://{host}:8091/api/v1/meta/canonical-summary
```

预期：`200`

验证点：
- [ ] 所有 `quran_*` 计数 > 0
- [ ] `dua_categories` = 31，`dua_items` = 72
- [ ] 所有 `hadith_*` 计数 = 0（预期）

---

### 4.4 `GET /api/v1/quran/chapters`

预期：`200`，`chapters` 数组长度 = `114`

验证点：
- [ ] 第 1 章 `name_simple` = `"Al-Fatihah"`，`verses_count` = 7
- [ ] 第 114 章 `name_simple` = `"An-Nas"`，`verses_count` = 6
- [ ] 每条含 `chapter_id`、`name_simple`、`name_arabic`、`verses_count`

---

### 4.5 `GET /api/v1/quran/resources/translations`

预期：`200`，`translations` 数组长度 = `145`

验证点：
- [ ] 包含 `id=20`（Saheeh International）
- [ ] 每条含 `id`、`name`、`author_name`、`language_name`

---

### 4.6 `GET /api/v1/quran/resources/recitations`

预期：`200`，`recitations` 数组长度 = `12`

验证点：
- [ ] 包含 `id=7`（Mishari al-Afasy）
- [ ] 每条含 `id`、`reciter_name`、`translated_name`

---

### 4.7 `GET /api/v1/quran/ayahs/{verse_key}/minimal`（极简三段式）

> 给第三方最容易接入的极简接口

```
GET http://{host}:8091/api/v1/quran/ayahs/1:1/minimal
```

可选参数：`?translation_id=X&recitation_id=Y`

预期响应：
```json
{
  "verse_key": "1:1",
  "surah_number": 1,
  "ayah_number": 1,
  "translation_id": 20,
  "recitation_id": 7,
  "arabic_text": "بِسْمِ ٱللَّهِ ٱلرَّحْمَـٰنِ ٱلرَّحِيمِ",
  "transliteration_text": "bis'mi l-lahi l-raḥmāni l-raḥīmi",
  "translation_text": "In the name of Allāh, the Entirely Merciful, the Especially Merciful."
}
```

验证点：
- [ ] 仅返回三段文本：`arabic_text`、`transliteration_text`、`translation_text`
- [ ] **不应**出现 `words`、`timing`、`chapter_audio`
- [ ] `arabic_text` 非空、`translation_text` 无 HTML / 无脚注编号
- [ ] `?translation_id=19` 后 `translation_text` 确实不同

必测 verse_key：`1:1`、`2:255`、`18:1`、`114:1`、`114:6`、`999:1`（应 404）

---

### 4.8 `GET /api/v1/quran/ayahs/{verse_key}`（完整详情）

```
GET http://{host}:8091/api/v1/quran/ayahs/1:1
```

验证点：
- [ ] 包含 `chapter_audio`（章级 MP3 URL）
- [ ] 包含 `ayah.words[]`：逐词数组，`char_type` 全为 `"word"`
- [ ] `words` 按 `position` 升序，从 1 开始
- [ ] 包含 `ayah.timing`（segments 中 `word_index` 对应 `words[].position`）
- [ ] 包含 `ayah.audio`（verse 级 MP3 URL）
- [ ] 所有 URL 以 `https://` 开头

---

### 4.9 `GET /api/v1/quran/ayahs/{verse_key}/translation`

验证点：
- [ ] `item.translation_text` 非空、无 HTML
- [ ] `?translation_id=19` 后文本不同

---

### 4.10 `GET /api/v1/quran/chapters/{chapter_id}/ayahs`

验证点：
- [ ] `ayahs` 长度 = 该章 `verses_count`
- [ ] 建议测试：chapter 1（7）、2（286）、18（110）、114（6）

---

### 4.11 `GET /api/v1/quran/chapters/{chapter_id}/triplets`（三段式分组）

> AI Quran 主要接口

验证点：
- [ ] `blocks` 非空，每 block 含 `verse_keys`（最多 3 个）
- [ ] `group_size` = `verse_keys` 长度
- [ ] `arabic_text` 用 `\n` 分隔
- [ ] `audio_urls` 和 `children` 长度 = `verse_keys` 长度
- [ ] 预期 block 数：chapter 1 = 3，chapter 2 = 96，chapter 114 = 2

---

### 4.12 `GET /api/v1/quran/chapters/{chapter_id}/audio-text`（音频文本一体）

> 最复杂的接口——音频、文本、verse timing、word segment 全在一个 payload

验证点：
- [ ] `chapter_audio.audio_url` 可浏览器播放
- [ ] `chapter_audio.timings` 长度 = 该章 ayah 数
- [ ] 首条 timing `timestamp_from_ms` = 0，相邻无间隙
- [ ] `words[].segment` 含 `word_index`/`start_ms`/`end_ms`
- [ ] `?recitation_id=1` 后 `audio_url` 不同
- [ ] 建议测试：chapter 1、2、18、114

---

### 4.13 `GET /api/v1/quran/chapters/{chapter_id}/audio`

验证点：
- [ ] `audio_url` 可浏览器打开
- [ ] `timings` 长度 = 该章 ayah 数，每条含 `segments`

---

## 5. Dua 接口测试明细

### 5.1 `GET /api/v1/dua/categories`（分类列表）

```
GET http://{host}:8091/api/v1/dua/categories
```

预期：`200`

预期响应：
```json
{
  "screen": "dua_categories",
  "source": "kaggle/islamic-dua-adhkar",
  "total_categories": 31,
  "total_duas": 72,
  "categories": [
    { "category": "Congratulations", "dua_count": 1 },
    { "category": "Death & Condolence", "dua_count": 3 },
    { "category": "Evening Adhkar", "dua_count": 2 },
    { "category": "Family & Parents", "dua_count": 4 },
    { "category": "Fasting", "dua_count": 3 }
  ]
}
```

验证点：
- [ ] `total_categories` = 31
- [ ] `total_duas` = 72
- [ ] `categories` 长度 = 31
- [ ] 每条含 `category`（字符串）和 `dua_count`（正整数）
- [ ] 所有 `dua_count` 之和 = 72

**完整分类列表**

| 分类 | 数量 |
|------|------|
| Congratulations | 1 |
| Death & Condolence | 3 |
| Evening Adhkar | 2 |
| Family & Parents | 4 |
| Fasting | 3 |
| Food & Drink | 4 |
| Forgiveness | 2 |
| Friday (Jumu'ah) | 2 |
| General | 6 |
| Gratitude | 2 |
| Hardship & Anxiety | 3 |
| Health & Illness | 2 |
| Home | 2 |
| Knowledge & Study | 2 |
| Laylatul Qadr | 1 |
| Marketplace | 1 |
| Marriage | 2 |
| Morning Adhkar | 3 |
| Morning Dhikr | 1 |
| Protection | 3 |
| Quran Recitation | 1 |
| Rain & Weather | 3 |
| Repentance | 1 |
| Salah | 4 |
| Seeking Guidance | 1 |
| Seeking Paradise | 2 |
| Sleep | 3 |
| Toilet | 2 |
| Travel | 3 |
| Wealth & Sustenance | 2 |
| Wudu | 1 |

---

### 5.2 `GET /api/v1/dua/categories/{category}`（分类下所有 Dua）

```
GET http://{host}:8091/api/v1/dua/categories/Sleep
```

预期：`200`

预期响应：
```json
{
  "screen": "dua_category_items",
  "source": "kaggle/islamic-dua-adhkar",
  "category": "Sleep",
  "total": 3,
  "items": [
    {
      "dua_id": "DUA-0006",
      "category": "Sleep",
      "title": "Before Sleeping",
      "arabic_text": "بِاسْمِكَ اللَّهُمَّ أَمُوتُ وَأَحْيَا",
      "transliteration": "Bismika Allahumma amutu wa ahya",
      "english_meaning": "In Your name O Allah, I die and I live.",
      "urdu_meaning": "اے اللہ تیرے نام پر مرتا اور جیتا ہوں۔",
      "source": "Sahih Bukhari",
      "reference": "6324",
      "authenticity": "Sahih",
      "occasion": "Before sleeping"
    }
  ]
}
```

验证点：
- [ ] `category` = 请求中的分类名
- [ ] `total` = 该分类 dua 数量
- [ ] `items` 长度 = `total`
- [ ] 每条含完整字段：`dua_id`、`arabic_text`、`transliteration`、`english_meaning`、`source`、`reference`

必测分类：
| 分类 | 预期数量 |
|------|----------|
| Sleep | 3 |
| Morning Adhkar | 3 |
| Salah | 4 |
| General | 6 |
| NotExist | 503 |

---

### 5.3 `GET /api/v1/dua/items`（全部 Dua）

```
GET http://{host}:8091/api/v1/dua/items
```

预期：`200`

验证点：
- [ ] `total` = 72
- [ ] `items` 长度 = 72
- [ ] `dua_id` 从 `DUA-0001` 到 `DUA-0072` 排列
- [ ] 每条至少含 `arabic_text`、`transliteration`、`english_meaning`

---

### 5.4 `GET /api/v1/dua/{dua_id}`（Dua 详情）

```
GET http://{host}:8091/api/v1/dua/DUA-0001
```

预期：`200`

预期响应：
```json
{
  "screen": "dua_detail",
  "source": "kaggle/islamic-dua-adhkar",
  "dua": {
    "dua_id": "DUA-0001",
    "category": "Morning Adhkar",
    "title": "Waking Up",
    "arabic_text": "الْحَمْدُ لِلَّهِ الَّذِي أَحْيَانَا بَعْدَ مَا أَمَاتَنَا وَإِلَيْهِ النُّشُورُ",
    "transliteration": "Alhamdu lillahil-ladhi ahyana ba'da ma amatana wa ilayhin-nushur",
    "english_meaning": "All praise is for Allah who gave us life after having taken it from us and unto Him is the resurrection.",
    "urdu_meaning": "تمام تعریف اللہ کے لیے جس نے ہمیں موت کے بعد زندگی دی اور اسی کی طرف اٹھنا ہے۔",
    "source": "Sahih Bukhari",
    "reference": "6312",
    "authenticity": "Sahih",
    "occasion": "Upon waking up",
    "data_source": "Manual (Hisnul Muslim + Quran)",
    "verification_status": "✅ VERIFIED | ..."
  }
}
```

验证点：
- [ ] `dua.dua_id` = 请求中的 ID
- [ ] `arabic_text` 非空，为阿拉伯文
- [ ] `transliteration` 非空，为拉丁音译
- [ ] `english_meaning` 非空，为英文
- [ ] `source` 非空（如 Sahih Bukhari、Sahih Muslim、Quran 等）
- [ ] `reference` 非空
- [ ] `authenticity` 为 `Sahih` 或 `Hasan`

必测 dua_id：
| dua_id | 预期 |
|--------|------|
| DUA-0001 | 正常返回 |
| DUA-0072 | 最后一条，正常返回 |
| DUA-9999 | 503（不存在） |

---

## 6. 多翻译 / 多诵读测试（Quran）

以下接口支持 `?translation_id=X&recitation_id=Y`：

| translation_id | 名称 | recitation_id | 诵读者 |
|----------------|------|---------------|--------|
| 20（默认） | Saheeh International | 7（默认） | Mishari al-Afasy |
| 19 | Pickthall | 1 | AbdulBaset Mujawwad |
| 22 | Yusuf Ali | 2 | AbdulBaset Murattal |

验证点：
- [ ] 切换 `translation_id` 后 `translation_text` 不同
- [ ] 切换 `recitation_id` 后 `audio_url` 不同
- [ ] 未同步的资源返回 `503`（不是 500）

---

## 7. 边界和异常测试

| 测试场景 | 请求 | 预期 |
|----------|------|------|
| 不存在的章 | `GET /api/v1/quran/chapters/999/ayahs` | 404 |
| 不存在的 verse_key | `GET /api/v1/quran/ayahs/999:1/minimal` | 404 |
| 非法 verse_key | `GET /api/v1/quran/ayahs/abc/minimal` | 400 或 422 |
| 不存在的 dua_id | `GET /api/v1/dua/DUA-9999` | 503 |
| 不存在的分类 | `GET /api/v1/dua/categories/NotExist` | 503 |
| 未同步的 translation | `GET /api/v1/quran/ayahs/1:1/minimal?translation_id=99999` | 503 |

---

## 8. 音频可访问性（Quran）

以下 URL 应可在浏览器直接播放：

| 类型 | 示例 URL |
|------|----------|
| 章级 MP3 | `https://download.quranicaudio.com/qdc/mishari_al_afasy/murattal/1.mp3` |
| Verse MP3 | `https://verses.quran.foundation/Alafasy/mp3/001001.mp3` |
| Word MP3 | `https://verses.quran.foundation/wbw/001_001_001.mp3` |

---

## 9. 测试结论模板

### Quran

| 测试项 | 状态 | 备注 |
|--------|------|------|
| 健康检查 | 通过 / 不通过 | |
| 章目录（114 章） | 通过 / 不通过 | |
| 翻译资源列表（145 个） | 通过 / 不通过 | |
| 诵读者列表（12 个） | 通过 / 不通过 | |
| 极简三段式 (minimal) | 通过 / 不通过 | |
| Ayah 完整详情 | 通过 / 不通过 | |
| 翻译接口 | 通过 / 不通过 | |
| 整章 ayahs | 通过 / 不通过 | |
| 三段式分组 (triplets) | 通过 / 不通过 | |
| 音频文本一体 (audio-text) | 通过 / 不通过 | |
| 章级音频 (audio) | 通过 / 不通过 | |
| 多翻译切换 | 通过 / 不通过 | |
| 多诵读切换 | 通过 / 不通过 | |
| 音频 URL 可播放 | 通过 / 不通过 | |

### Dua

| 测试项 | 状态 | 备注 |
|--------|------|------|
| 分类列表（31 个分类） | 通过 / 不通过 | |
| 分类下 Dua 列表 | 通过 / 不通过 | |
| 全部 Dua 列表（72 条） | 通过 / 不通过 | |
| Dua 详情 | 通过 / 不通过 | |
| 阿拉伯文正确 | 通过 / 不通过 | |
| 英文翻译完整 | 通过 / 不通过 | |
| 出处和引用 | 通过 / 不通过 | |

### 异常 & 其他

| 测试项 | 状态 | 备注 |
|--------|------|------|
| 异常输入处理 | 通过 / 不通过 | |
| Hadith | **待凭证** | 拿到 SUNNAH_API_KEY 后再测 |

---

## 10. Dua 数据来源说明

- 数据集：[Islamic Dua and Adhkar (Kaggle)](https://www.kaggle.com/datasets/ahsanneural/islamic-dua-and-adhkar-72-verified-duas)
- 许可证：CC BY 4.0
- 总量：72 条经验证的 Dua，31 个生活场景分类
- 认证率：98.6%（Sahih / Hasan）
- 来源：Sahih Bukhari、Sahih Muslim、Sunan Abu Dawud、Sunan Ibn Majah、Sunan At-Tirmidhi、Holy Quran、Hisnul Muslim
- 语言：阿拉伯文（带完整标音）+ 拉丁音译 + 英文翻译 + 乌尔都语翻译

---

## 11. 已知限制

1. Quran 约 0.32% word（245/77429）缺 segment timing——上游数据问题
2. Hadith 无数据（无 SUNNAH_API_KEY），所有 Hadith 接口返回 503
3. Quran User OAuth 未启用，相关接口返回 503
4. Quran 同步凭证未配置（`configured: false`），但不影响已落库数据读取
5. Dua 数据为静态数据集，不支持在线增量更新

---

## 12. Swagger 在线文档

服务启动后访问 `http://{host}:8091/docs` 查看完整 OpenAPI 文档。

所有接口均可在 Swagger 页面直接试用。

---

## 13. 接下来等什么

| 待办项 | 阻塞条件 | 预期工作量 |
|--------|----------|-----------|
| Hadith 上线 | 拿到 `SUNNAH_API_KEY` | 拿到 key 后执行 `sync hadith` 即可 |
| Quran User OAuth | 拿到 user-enabled 权限 | 配置完成后即可用 |
