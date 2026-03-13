# Quran / Hadith 微服务交付说明

更新时间：2026-03-11

## 0. 领导版 TL;DR

- Quran 微服务已经做成独立服务，可直接 Swagger 演示和接口测试。
- 默认 Quran 版本已经完整可用，包含：全文、逐节、逐词、三段式、翻译、音频、音频时间轴、一体接口。
- 多翻译、多音色能力已经实现，并已做过真实 live 验证；但“所有翻译 × 所有音色 × 全部 114 章”的全量数据回填仍在进行中。
- Hadith 代码和数据库都已准备好，但还缺 `SUNNAH_API_KEY`，所以暂时不能做真实联调。
- Quran 用户功能不是现在这组 content 凭证自动带的，若要做登录、书签、笔记、进度同步，还需要单独申请 `user-enabled` 权限。

### 0.1 当前可以直接对外演示的内容

- `GET /api/v1/quran/ayahs/{verse_key}/minimal`
- `GET /api/v1/quran/ayahs/{verse_key}`
- `GET /api/v1/quran/chapters/{chapter_id}/triplets`
- `GET /api/v1/quran/chapters/{chapter_id}/audio-text`
- `GET /api/v1/quran/resources/translations`
- `GET /api/v1/quran/resources/recitations`

### 0.2 当前还不能宣称“全部完成”的内容

- Hadith live 数据联调
- Quran user features 正式打通
- 全量多翻译、多音色的全章节最终回填完成确认

## 1. 这次项目要解决什么问题

以前 Quran 主要依赖 PDF / OCR / 人工整理进入知识库，问题是：

- 维护成本高
- 文本和音频难以稳定对齐
- 扩展多翻译、多音色很困难
- 第三方和前端拿到的不是稳定 API，而是偏内部的数据流程

这次我们把它升级成：

```text
上游官方/开源 API
-> Islamic Content 独立微服务
-> PostgreSQL canonical 数据
-> 对外 API / Swagger
-> 第三方、前端、后续 KB 投影
```

当前独立服务路径：

- [apps/islamic-content-service](/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/apps/islamic-content-service)

## 2. 现在已经做完了什么

### 2.1 服务形态

已经完成独立微服务拆分，具备：

- 独立进程
- 独立 Swagger
- 独立数据库 schema
- 独立同步 CLI
- 不再依赖 `ai_gateway` 主运行时

核心入口：

- [main.py](/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/apps/islamic-content-service/src/islamic_content_service/main.py)
- [cli.py](/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/apps/islamic-content-service/src/islamic_content_service/cli.py)

### 2.2 数据库存储

已经完成 PostgreSQL canonical 设计与迁移。

当前 schema 里核心表包括：

- `quran_chapters`
- `quran_ayahs`
- `quran_words`
- `quran_ayah_translations`
- `quran_ayah_audio`
- `quran_chapter_audio_tracks`
- `quran_chapter_audio_timings`
- `quran_triplet_ranges`
- `hadith_collections`
- `hadith_books`
- `hadith_items`
- `hadith_localizations`
- `hadith_grades`
- `source_sync_runs`
- `source_snapshots`

迁移文件：

- [001_init_schema.sql](/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/apps/islamic-content-service/migrations/001_init_schema.sql)

### 2.3 Quran 内容能力

已经完成：

- 全古兰经正文
- ayah 最小粒度
- word 最小粒度
- chapter -> ayah -> word 层级
- 三段式 triplet（每 3 ayah 一组）
- chapter audio
- verse timing
- word segment timing
- 官方翻译
- 多 translation / 多 recitation 选择
- 音频和文本一体返回

核心实现：

- [quran_sync_service.py](/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/apps/islamic-content-service/src/islamic_content_service/services/quran_sync_service.py)
- [quran_query_service.py](/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/apps/islamic-content-service/src/islamic_content_service/services/quran_query_service.py)
- [quran_repository.py](/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/apps/islamic-content-service/src/islamic_content_service/repositories/quran_repository.py)
- [quran.py](/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/apps/islamic-content-service/src/islamic_content_service/api/routes/quran.py)

### 2.4 Quran 用户功能接入骨架

已经完成后端接入骨架：

- OAuth 授权 URL 生成
- code exchange
- refresh token
- userinfo
- user API proxy

对应代码：

- [quran_user_client.py](/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/apps/islamic-content-service/src/islamic_content_service/clients/quran_user_client.py)
- [quran_user_service.py](/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/apps/islamic-content-service/src/islamic_content_service/services/quran_user_service.py)
- [quran_user.py](/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/apps/islamic-content-service/src/islamic_content_service/api/routes/quran_user.py)

注意：这部分代码已经有了，但要真正打通，仍需要 Quran Foundation 单独给 `user-enabled` 权限。

### 2.5 Hadith

Hadith 微服务代码、表结构、路由已完成，但还缺真实 `SUNNAH_API_KEY` 的 live 联调。

关键代码：

- [hadith_sync_service.py](/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/apps/islamic-content-service/src/islamic_content_service/services/hadith_sync_service.py)
- [hadith_repository.py](/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/apps/islamic-content-service/src/islamic_content_service/repositories/hadith_repository.py)
- [hadith.py](/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/apps/islamic-content-service/src/islamic_content_service/api/routes/hadith.py)

## 3. 现在已经能拿到哪些数据

### 3.1 Quran 已确认可拿到

根据实际 live 验证，当前服务已经能拿到：

- 全部 114 章 Quran 结构
- 完整 ayah 结构
- 完整 word 结构
- 官方 translations 资源目录
- 官方 recitations 资源目录
- chapter mp3
- verse 级 timing
- word 级 `segments`
- `audio + text` 组合 payload

已确认上游资源数量：

- translations：145 个
- recitations：12 个

### 3.2 Quran 已真实验证的数据样例

已真实验证：

- 默认版本：`translation_id=20`、`recitation_id=7`
- 非默认版本：`translation_id=84`、`recitation_id=1`

例如已经实际返回成功：

- `GET /api/v1/quran/ayahs/1:1/minimal`
- `GET /api/v1/quran/ayahs/1:1?translation_id=84&recitation_id=1`
- `GET /api/v1/quran/chapters/1/audio-text?translation_id=84&recitation_id=1`

这说明：

- 不是只支持默认翻译
- 不是只支持默认音色
- 已经能返回“非默认翻译 + 非默认音色”的真实组合

### 3.3 Hadith 目前状态

Hadith 设计和代码都已具备，但未完成 live 数据验证。

原因不是代码，而是还没有真实 `SUNNAH_API_KEY`。

## 4. 现在是否符合需求

### 4.1 已符合的部分

对于你们当前最核心的 Quran 需求，已经基本符合：

- `AI Quran`
  - 3 ayah 一组
  - 阿拉伯文
  - transliteration
  - translation
  - 音频
  - 一体接口

- `Sh Wahda`
  - 最小粒度 `1:1`
  - word 级结构
  - verse timing
  - word segment timing
  - 可替代 OCR 路线

- 数据源
  - 已切换到 Quran Foundation 正式内容 API 模型
  - 不再依赖 PDF/OCR 作为主来源

### 4.2 还不能说“100% 完成”的部分

还差两点：

1. 全量 Quran 的“所有 translation × 所有 recitation × 所有 114 章”真实 bootstrap 还在跑
2. Quran user features 需要单独的 `user-enabled` 权限，不是现在这组 content 凭证自动带的

所以准确表达应该是：

- **代码能力已经支持**
- **默认版本已完整可用**
- **非默认版本已做 live 验证**
- **全量多资源全章节正在做最终数据回填**

## 5. 对外 API 现在有哪些

### 5.1 Meta

- `GET /health`
- `GET /health/live`
- `GET /health/ready`
- `GET /api/v1/meta/config`
- `GET /api/v1/meta/manifest`
- `GET /api/v1/meta/canonical-summary`

### 5.2 Quran

- `GET /api/v1/quran/chapters`
- `GET /api/v1/quran/resources/translations`
- `GET /api/v1/quran/resources/recitations`
- `GET /api/v1/quran/chapters/{chapter_id}/ayahs`
- `GET /api/v1/quran/chapters/{chapter_id}/triplets`
- `GET /api/v1/quran/chapters/{chapter_id}/audio`
- `GET /api/v1/quran/chapters/{chapter_id}/audio-text`
- `GET /api/v1/quran/ayahs/{verse_key}/minimal`
- `GET /api/v1/quran/ayahs/{verse_key}`
- `GET /api/v1/quran/ayahs/{verse_key}/translation`
- `GET /api/v1/quran/chapters/{chapter_id}/translations`

这些接口现在支持可选参数：

- `translation_id`
- `recitation_id`

### 5.3 Quran User

- `GET /api/v1/quran/user/auth/config`
- `GET /api/v1/quran/user/auth/authorize-url`
- `POST /api/v1/quran/user/auth/token`
- `POST /api/v1/quran/user/auth/refresh`
- `GET /api/v1/quran/user/userinfo`
- `POST /api/v1/quran/user/request`

### 5.4 Hadith

- `GET /api/v1/hadith/collections`
- `GET /api/v1/hadith/collections/{collection_name}/books`
- `GET /api/v1/hadith/collections/{collection_name}/books/{book_number}/hadiths`
- `GET /api/v1/hadith/collections/{collection_name}/hadiths/{hadith_number}`

## 6. 现在测试和演示怎么做

### 6.1 Swagger

当前服务启动后可直接用 Swagger：

- [http://127.0.0.1:8091/docs](http://127.0.0.1:8091/docs)

### 6.2 脚本测试

测试脚本：

- [test_public_api.py](/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/apps/islamic-content-service/scripts/test_public_api.py)

常用示例：

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/apps/islamic-content-service
python scripts/test_public_api.py --list
python scripts/test_public_api.py --endpoint quran_audio_text --chapter-id 1 --translation-id 84 --recitation-id 1
python scripts/test_public_api.py --endpoint quran_ayah_detail --verse-key 1:1 --translation-id 84 --recitation-id 1
python scripts/test_public_api.py --endpoint quran_user_authorize_url --redirect-uri https://wahda.example/callback --state abc123
```

## 7. 还差哪些事情

### 7.1 必做

- 等这次全量 Quran bootstrap 跑完，确认所有 translation / recitation 对所有章节都 ready
- 拿到 `SUNNAH_API_KEY`，完成 Hadith live bootstrap

### 7.2 如果要启用 Quran 用户功能

还需要 Quran Foundation 单独开通：

- user-enabled access
- Authorization Code + PKCE
- user-related APIs
- redirect URI allowlist

## 8. 需要怎么申请

### 8.1 Quran Content API

这部分你们已经拿到了，可继续使用。

官方文档说明：

- Content APIs 走 `client_credentials`
- scope 是 `content`
- 请求头带 `x-client-id` 和 `x-auth-token`

参考：

- [Quran Foundation Content APIs](https://api-docs.quran.foundation/docs/content_apis_versioned/4.0.0/content-apis/)
- [Quran Foundation Quick Start](https://api-docs.quran.foundation/docs/quickstart/)

### 8.2 Quran User-enabled

这部分需要单独申请，原因是官方明确区分：

- Content APIs：非用户数据
- User APIs：书签、笔记、collections、progress 等用户数据

当前 production content 凭证默认不带 user features。

官方文档说明：

- User APIs 使用 `Authorization Code + PKCE`
- 需要在申请页填写 redirect URI 等 OAuth 信息

参考：

- [Request Access](https://api-docs.quran.foundation/request-access/)
- [Using OAuth 2.0 to Access Quran.Foundation APIs](https://api-docs.quran.foundation/docs/tutorials/oidc/getting-started-with-oauth2/)
- [OAuth2 Scopes](https://api-docs.quran.foundation/docs/user_related_apis_versioned/scopes/)
- [User-related APIs](https://api-docs.quran.foundation/docs/category/user-related-apis)

建议申请时说明：

- 你们已经在用 Content APIs
- 现在要开通 user-enabled access
- 用途是：
  - user login
  - bookmarks
  - notes
  - collections
  - reading progress

### 8.3 Sunnah / Hadith API

Hadith 还要单独申请 `API key`。

官方当前说明：

- 需要 API key
- 申请方式是去他们 GitHub repo 提 issue

参考：

- [Sunnah Developers](https://sunnah.com/developers)
- [Sunnah API Docs](https://sunnah.stoplight.io/docs/api/pe5b9g6mqp16f-sunnah-com-api)

## 9. 给领导的一句话结论

这次已经把 Quran 从“PDF/OCR 知识库导入”升级成“官方结构化 API + 独立微服务 + PostgreSQL canonical 数据”的正式方案了。

当前：

- Quran 默认版本已完整可用
- 多翻译、多音色能力已实现并做了 live 验证
- 全量多资源数据仍在做最终回填
- Hadith 还差 Sunnah key
- Quran 用户功能还差 user-enabled 权限开通
