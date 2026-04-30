# Islamic Content 数据最终审查记录 - 2026-04-30

## 目标

本轮审查目标是确认本地数据库和 API 输出同时满足：

- 内容不乱码、不含不可见 bidi 控制符、无 U+FFFD 解码失败字符。
- Quran / Dua / Hadith 的层级和计数一致。
- Hadith 的 Collection -> Book -> Chapter -> Hadith 层级不再暴露空 chapter、默认 title、错误 book 归属。
- API 给 App/开发的默认响应不需要客户端猜测或自行补标题。
- 真实上游缺口作为 source gap 库存暴露，不用默认值伪造正文或翻译。

## 本轮新增审查项

### DB 硬不变量

- H1 同时检查 `collection_name` 和 `book_number`，防止 chapter_ref_id 跨 collection/book 串线。
- H17 检查 source-backed chapter title，禁止 `Chapter:`, `باب`, `Additional hadiths (not grouped by sunnah.com)`, `Introduction (Unmapped preamble hadiths)` 这类占位/默认标题。
- H19 检查 collection/book title 非空、非 `Book N` / `Chapter N` / `unknown` / `default`。
- H20 检查每条 Hadith 都有非空 Arabic localization。
- H21 单独列出 English/source translation gap，不用默认值补。

### API 深度审计

`audit_full_api.py --deep-lists` 会：

- 遍历全部 collections 和 books。
- 遍历全部 chapter endpoints。
- 按 `limit=200` 拉取所有 book item pages，扫描每条 summary。
- 校验每本书的 chapter `hadith_count` 总和等于 book `number_of_hadith`。
- 校验每条 summary 的 `chapter_title` / `section_title` / `title` 不含默认占位标题。
- 校验每条 summary 的 Arabic preview 非空。
- 将 English preview 缺失列为 source-gap warning，而不是静默通过或伪造翻译。

## 本轮修复

- 用 HuggingFace + AhmedBaset 元数据拆分/修正 source-backed chapter title。
- 修复 `Additional hadiths (not grouped by sunnah.com)` 被当作真实 source-backed title 的问题。
- 补齐 6 条可溯源 Arabic localization：
  - Ibn Majah 1928, 1929: AhmedBaset 英文正文唯一匹配后补 Arabic。
  - Tirmidhi 2787, 2855, 3011, 3015: fawaz Arabic edition 按 hadith number 补 Arabic。
- Muslim 2525a English 在 sunnah.com / AhmedBaset / fawaz 源均为空，保留为 source gap，不做默认值填充。
- Bukhari Book 0 剩余条目已确认来自 fawaz `reference.book=0` 源异常库存，不再混入 source-backed 章节判断。
- 新增非 Book 0 Hadith 来源交叉审计：
  - 全量读取生产库 33,400 条非 Book 0 Hadith。
  - 联网比对 fawazahmed0 hadith-api、HuggingFace `meeAtif/hadith_datasets`、AhmedBaset/hadith-json，并对疑点补查 sunnah.com live 页面。
  - 修复 52 条 Arabic 正文错配：
    - 48 条由 fawaz + HuggingFace Arabic 双源一致确认后更新。
    - 1 条 Nasai 3722 在 0.98 阈值下确认首位传述人不同，fawaz + HuggingFace + sunnah.com live 一致后更新。
    - 2 条 Tirmidhi 1424/1425 因 HuggingFace 行内容偏移，采用 fawaz + sunnah.com live 一致文本更新。
    - 1 条 Nasai 135 因 fawaz 源含 U+FFFD，采用 HuggingFace + sunnah.com live 一致文本更新。
  - Gemini 复核用于 suspicious 候选/分层样本；生产 key 在部分批次返回 429，因此 AI 结果只作为辅助证据，最终落库只依赖外部来源证据。

## 新增来源/AI 审计脚本

```bash
# 非 Book 0 Hadith 全量来源交叉审计
docker exec islamic-content-service python /tmp/audit_hadith_sources_ai.py \
  --timeout 120 \
  --out /tmp/hadith_source_audit_noai_20260430_v7.json \
  --markdown-out /tmp/hadith_source_audit_noai_20260430_v7.md

# AI 辅助复核（Gemini 可能因配额返回 429；不能替代来源证据）
docker exec islamic-content-service python /tmp/audit_hadith_sources_ai.py \
  --ai-review --timeout 120 --ai-max-candidates 10 --ai-sample-per-collection 2
```

最新结果：

- hard: 0
- warn: 1 (`muslim/44/2525a` English source gap)
- info: HuggingFace 与主源的标题/译文 variant，仅作来源差异库存，不作为 DB 错误。

## 当前服务器审计结果

### DB

命令：

```bash
docker cp /opt/deploy/ai-gateway/apps/islamic-content-service/scripts/audit_full_data.sql ai-gateway-pg:/tmp/audit_full_data.sql
docker exec ai-gateway-pg psql -U postgres -d gateway -f /tmp/audit_full_data.sql
```

关键结果：

- Quran: Q1-Q14 全部 PASS / 0 violation。
- Dua: D1-D10 全部 PASS / 0 violation。
- Hadith: H1-H17、H19-H20 全部 0 violation。
- Format scans: F1-F4 全部 0。
- H18 source-gap inventory:
  - bukhari: 87 synthetic chapters / 306 hadiths
  - ibnmajah: 1 synthetic chapter / 266 hadiths
  - nawawi: 1 synthetic chapter / 42 hadiths
- H21 source translation gap:
  - en: 1 row (`muslim/44/2525a`)

### API

命令：

```bash
cd /opt/deploy/ai-gateway/apps/islamic-content-service
python3 scripts/audit_full_api.py --base-url http://127.0.0.1:8091/api/v1 --timeout 30 --concurrency 30 --deep-lists
```

结果：

```text
ALL GREEN - 0 violations across Quran, Dua, and Hadith
WARN: hadith_summary_empty_translation_source_gap = muslim/44/2525a
```

## 回归用例设计

### P0 必跑

- DB 全量不变量：`audit_full_data.sql`
- API 深度遍历：`audit_full_api.py --deep-lists`
- 非 Book 0 Hadith 来源交叉审计：`audit_hadith_sources_ai.py`
- Bukhari Book 1: 只能返回 1 个 chapter，`hadith_count=7`。
- Bukhari Book 0: 作为 source gap inventory 存在，不得混入 source-backed title 检查。
- Muslim 2525a: Arabic 非空，English 为空只能作为 warning/source gap。
- Hadith Arabic localization: H20 必须为 0。

### P1 定期跑

- `repair_hadith_chapters_from_hf.py --dry-run`: 应无需要修的 source-backed placeholder。
- `repair_hadith_localization_gaps.py --dry-run`: 应只剩已知 source gap。
- `repair_hadith_book_references.py --collection bukhari --dry-run`: 当前应为 `movable_items: 0`。

### P2 抽样人工复核

- Bukhari 1, 2, 65, 97 chapter list。
- Ibn Majah 9 hadith 1928/1929 detail。
- Tirmidhi 43/47 补 Arabic 的 4 条 detail。
- Nawawi 1 flat collection chapter title。

## 审查结论

当前 hard violations 为 0。唯一剩余是可解释、可复现的 source gap：`muslim/44/2525a` 上游没有 English translation。该项不应由后端默认值填充。
