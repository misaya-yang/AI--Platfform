# Islamic Content 提测说明

更新时间：2026-03-11

## 1. 提测范围

本轮建议测试：

- Quran
- Meta / Health

本轮暂不做通过结论：

- Hadith live 数据
- Quran user features 正式登录闭环

## 2. 测试前提

### 2.1 服务已启动

测试目标是独立微服务：

- [apps/islamic-content-service](/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/apps/islamic-content-service)

Swagger 使用方式：

- 打开服务基础地址下的 `/docs`
- 不在文档里写死 host/port，按当前部署环境访问

### 2.2 基础数据前提

测试前应确认：

- Quran 已完成基础 bootstrap
- 默认 translation / recitation 已可用
- 如果要测非默认组合，对应资源已完成同步

## 3. 推荐测试顺序

### 3.1 基础健康检查

- `GET /health`
- `GET /health/live`
- `GET /health/ready`
- `GET /api/v1/meta/config`
- `GET /api/v1/meta/canonical-summary`

预期：

- 返回 200
- `ready` 不报数据库或缓存错误

### 3.2 Quran 资源目录

- `GET /api/v1/quran/resources/translations`
- `GET /api/v1/quran/resources/recitations`

预期：

- 能看到 translation / recitation 资源目录
- 能看到当前已同步资源 ID

### 3.3 Quran 最小三段式

- `GET /api/v1/quran/ayahs/1:1/minimal`

预期字段：

- `arabic_text`
- `transliteration_text`
- `translation_text`
- `verse_key`
- `surah_number`
- `ayah_number`

说明：

- 这是给第三方最容易接的最小接口
- 不应返回 `chapter_audio`、`words`、`timing`

### 3.4 Quran 最小详情

- `GET /api/v1/quran/ayahs/1:1`

预期字段：

- `ayah`
- `ayah.words`
- `ayah.timing`
- `ayah.audio`
- `chapter_audio`

说明：

- 这是详情型接口，不是极简接口
- 会返回逐词和整章时间轴

### 3.5 Quran 三段式分组

- `GET /api/v1/quran/chapters/1/triplets`

预期字段：

- `blocks`
- 每个 block 含：
  - `arabic_text`
  - `transliteration_text`
  - `translation_text`
  - `verse_keys`
  - `children`

说明：

- 这是 `AI Quran` 主要接口

### 3.6 Quran 音频文本一体接口

- `GET /api/v1/quran/chapters/1/audio-text`

预期字段：

- `chapter_audio`
- `ayahs`
- `ayahs[].timing`
- `ayahs[].words[].segment`

说明：

- 这是“音频和文本一起返回”的组合接口
- 适合前端做播放器、逐句高亮、逐词高亮

## 4. 非默认翻译 / 音色测试

如果要验证多资源能力，可测试：

- `GET /api/v1/quran/ayahs/1:1/minimal?translation_id=84&recitation_id=1`
- `GET /api/v1/quran/chapters/1/audio-text?translation_id=84&recitation_id=1`

预期：

- 返回 200
- 文本或音频资源切换成功

如果返回 503，通常表示：

- 对应 translation 或 recitation 尚未完成同步

## 5. 当前已知限制

- Hadith 没有真实 `SUNNAH_API_KEY` 时，不做 live 通过结论
- Quran user features 没有 user-enabled 权限时，不做登录闭环通过结论
- 全量多翻译、多音色在同步期间，部分资源可能返回 503，这是预期保护行为，不是假成功

## 6. 测试脚本

可直接使用脚本查看参数和调用接口：

- [test_public_api.py](/Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/apps/islamic-content-service/scripts/test_public_api.py)

建议命令：

```bash
cd /Users/misaya.yanghejazfs.com.au/hejaz_projects/ai_gateway/ai-gateway/apps/islamic-content-service
python scripts/test_public_api.py --list
python scripts/test_public_api.py --endpoint quran_ayah_minimal --verse-key 1:1
python scripts/test_public_api.py --endpoint quran_triplets --chapter-id 1
python scripts/test_public_api.py --endpoint quran_audio_text --chapter-id 1
```

## 7. 提测结论模板

建议测试同学按下面口径反馈：

- Quran 核心接口：通过 / 不通过
- 最小三段式：通过 / 不通过
- 音频文本一体接口：通过 / 不通过
- 多翻译、多音色：通过 / 不通过 / 部分资源未同步
- Hadith：待 `SUNNAH_API_KEY`
- Quran user：待 user-enabled 权限
