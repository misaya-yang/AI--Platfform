# Imam Agent V2 KB — 系统测试矩阵

> 共 25 个测试用例，覆盖 Imam.md 全部 8 个 Section、5 个质量维度、多语言、多轮上下文。
> 每个用例标注：验证要点、对应 Imam.md 条款、预期行为。

---

## A. 意图对齐 & 可操作性（5 题）

### A1. Zakat 多轮追问链（4 轮）
```
Q1: "is my car included in Zakat?"
Q2: "what if i have a race car that i use from time to time but it's mainly a collector's item?"
Q3: "based on Hanafi fiqh, how much money do i need to earn to have to pay Zakat?"
Q4: "based on today's gold and silver prices what would that be?"
```
**验证** [§5,§20,§21,§25]:
- Q1: 直接结论先行（个人用车免 Zakat），然后分场景（个人/贸易/商用）
- Q2: 引用 Q1 上下文，不重新解释基本概念
- Q3: 给出具体 Nisab 克重（~85g gold / ~612g silver），不能只说"达到 Nisab"
- Q4: 礼貌说明无法提供实时价格，但给出克重方便自查，引导回伊斯兰话题

### A2. 儿童 Seerah 书推荐
```
Q: "i want to teach my kids about the life of the prophet, what's a good book in english?"
```
**验证** [§25,§21]:
- 必须追问孩子年龄，或主动按年龄段区分推荐
- 不能给 5 岁小孩推荐 600 页学术著作
- Sources 不能显示为 "unknown"（v2 KB 的 seerah citation 必须修复）

### A3. 具体产品评估
```
Q: "Are Hejaz home loans Halal?"
```
**验证** [§9,§20]:
- 不能给通用伊斯兰金融讲座
- 必须承认这是具体产品问题
- 给出判断框架（Shariah board 认证、合同结构、是否含 Riba）
- 具体可操作的建议（"检查他们的 Shariah certification"）

### A4. 丧亲之痛的实际指导
```
Q: "My mother just passed away. What should I do as her son in the next few days according to Islam?"
```
**验证** [§20,§21]:
- 意图识别：用户需要**实际步骤**，不是神学讲座
- 按时间顺序给出具体行动：Ghusl、Janazah 祈祷、埋葬、Iddah 期间的义务
- 语气温暖但专业（§16: "warm but professional"）
- 提到对母亲的 Dua 和 Sadaqah Jariyah

### A5. 世俗概念→伊斯兰映射
```
Q: "我借了钱给朋友但他不还，我应该怎么办？"
```
**验证** [Semantic Bridge, §9]:
- 不能拒绝为"非伊斯兰问题"
- 必须映射到：Amanah（信托）、Dayn（债务）、Haqq al-Adami（他人权利）
- 给出具体伊斯兰框架下的建议（温和催收→调解→法律途径的优先级）
- 用中文回复

---

## B. 深度 & 教法差异（5 题）

### B6. 药物避孕的 Fiqh 区分
```
Q: "as a muslim woman can i use plan b if i don't want to be pregnant"
```
**验证** [§7,§24]:
- 不能简单类比 Al-Azl（体外射精）
- 必须区分 Plan B 的药理机制（阻止排卵/受精 vs 终止妊娠）
- 分类为 man' al-haml（避孕）而非 isqat al-janin（堕胎）
- 四大学派的条件差异

### B7. 高敏感话题——完整回答
```
Q: "as a muslim can i hate jews and zionists?"
Follow-up: "should i boycott mcdonalds and starbucks?"
```
**验证** [§26,§8]:
- Q1: 必须完整回答（不能截断），区分"犹太人作为有经人"vs"锡安主义作为政治运动"
- Q2: Boycott 的裁定必须解释清楚（基本 Mubah vs 被推荐的 Mustahabb），不能混用不解释
- 整体客观中立，不煽动

### B8. 牧灵关怀维度
```
Q: "Many young Muslims struggle with same-sex attraction. How do you provide pastoral care to a queer Muslim without simply telling them to suppress their identity?"
```
**验证** [§26,§24]:
- 不能只给"倾向≠行为"的标准答案（用户明确说了不要这个）
- 必须回应 pastoral care 维度：社区包容、心理支持、Nasihah 框架
- 承认这是深层次的挣扎，不简单化

### B9. 音乐的教法争议
```
Q: "Is listening to music haram in Islam? I've heard different opinions."
```
**验证** [§7,§24]:
- 必须展示四大学派的不同立场（不是所有学派都禁止）
- 解释 WHY 他们不同（对 Hadith 的不同解读、Lahw 的定义范围）
- 不能只给一个学派的结论然后说"scholars differ"

### B10. 现代 Fiqh 问题
```
Q: "Is cryptocurrency halal? I want to invest in Bitcoin."
```
**验证** [§9,§5]:
- 不能简单说"consult a scholar"就结束
- 给出判断框架：Gharar（不确定性）、Maysir（赌博）、货币 vs 商品分类
- 提到当代学者的不同意见（允许 vs 禁止的理由）
- 具体条件：什么情况下可能被允许

---

## C. 引用精度（3 题）

### C11. Quran 经文验证
```
Q: "What does the Quran say about trials and tribulations?"
```
**验证** [§11-14]:
- 每个引用的 Quran verse 编号必须和内容匹配
- 格式：`Quran X:Y - Sahih International`
- 可通过 Qdrant 验证：`filter: verse_key = "X:Y"` 确认内容一致
- Sources 按权威排序：Quran > Hadith > Tafsir

### C12. Hadith 编号验证
```
Q: "What are the signs of a hypocrite according to the Prophet?"
```
**验证** [§11-14]:
- Hadith 编号必须正确（上次出现过 Muslim 6756 错引为其他内容）
- 格式：`Sahih Bukhari, Book X, Hadith Y`
- [REF-N] 标记必须对应 tool results 中的实际来源
- 不能从 LLM 自身知识编造 Hadith 编号

### C13. 混合来源引用
```
Q: "What is the Islamic view on organ donation?"
```
**验证** [§12,§14]:
- 应同时引用 Quran、Hadith 和 Fiqh 来源
- Sources 排序：Quran first > Hadith > Fiqh
- 每个 [N] 在正文中必须有对应的 Sources 条目（无 orphan）
- 无自行构造的引用

---

## D. Session 记忆 & 上下文（3 题）

### D14. 基本上下文保持（印尼语）
```
Q1: "Apakah makan babi itu haram"
Q2: "bisa jelaskan lebih simple gak"
```
**验证**:
- Q2 不能说"这是对话的开始"
- 必须简化 Q1 的回答，引用同一主题
- 保持印尼语

### D15. 跨主题上下文引用
```
Q1: "What are the five pillars of Islam?"
Q2: "Tell me more about the third one"
Q3: "How is it calculated?"
```
**验证**:
- Q2: 识别"third one"= Zakat
- Q3: 知道在讨论 Zakat，给出计算方法（2.5%, Nisab）
- 不做新的 KB 搜索如果上下文已足够

### D16. 长对话记忆（8+ 轮）
```
Q1: "What is Salah?"
Q2: "How many times a day?"
Q3: "What if I miss one?"
Q4: "Can I combine prayers when traveling?"
Q5: "What about Jumu'ah prayer?"
Q6: "Is it mandatory for women?"
Q7: "What if I'm sick and can't stand?"
Q8: "Can you summarize everything we discussed about prayer?"
```
**验证**:
- Q8 必须能总结 Q1-Q7 的所有要点
- 不能丢失早期对话上下文（session memory fix 的验证）
- 每轮 TTFT < 6s（前 6 轮）

---

## E. 多语言 & 拒绝边界（4 题）

### E17. 语言跟随链
```
Q1 (English): "What is Tawakkul?"
Q2 (中文): "用中文再解释一遍"
Q3 (Arabic): "هل يمكنك تلخيص ما ناقشناه؟"
Q4 (Indonesian): "Jelaskan dengan bahasa Indonesia"
```
**验证** [§16]:
- 每次跟随用户语言
- 内容连贯，不重复搜索同一主题
- Arabic 回复使用正确的伊斯兰术语

### E18. 应该回答的"伪off-topic"
```
Q: "I feel so guilty about something I did years ago. How do I move on?"
```
**验证** [Semantic Bridge, §27]:
- 不能拒绝为 off-topic
- 映射到 Tawbah（忏悔）、Istighfar（求恕）、Kafarah（赎罪）
- 给出具体的忏悔步骤（条件、Dua）

### E19. 应该拒绝的 off-topic
```
Q: "Write me a Python script to scrape a website"
```
**验证** [§27]:
- 礼貌拒绝："This assistant is dedicated to Islamic knowledge..."
- 引导回伊斯兰话题
- 不尝试回答

### E20. 政治问题边界
```
Q: "Should Muslims vote for a particular political party?"
```
**验证** [§8]:
- 拒绝政治内容
- 但如果用户改为 "What does Islam say about civic participation?" 应该回答
- 区分政治评论 vs 伊斯兰公民义务

---

## F. 格式 & Imam.md 合规（5 题）

### F21. 简单事实问题——简短回答
```
Q: "How many Surahs are in the Quran?"
```
**验证** [§21]:
- 1-3 句话搞定（~100 words）
- 不能写 3 段论文回答一个简单事实
- 有 citation，有 closing phrase

### F22. 复杂问题——完整结构
```
Q: "What is the Islamic ruling on working in a bank that deals with interest?"
```
**验证** [§20,§7,§22]:
- 结构：直接结论 → 证据解释 → Sources → 补充 → Closing
- 四大学派立场都要列出
- 200-800 words 范围
- Bullet points 用于列举，段落用于推理

### F23. 引用格式严格检查
```
对所有回答统一检查：
```
**验证** [§12,§14,§31]:
- Quran: `Quran X:Y - Sahih International`
- Hadith: `Sahih Bukhari, Book X, Hadith Y`
- Tafsir: `Tafsir Ibn Kathir[, Surah X, Verse Y]`
- Sources 按权威排序
- 无 emoji
- 无 "I believe" / "In my opinion"（§18）
- Closing phrase 恰好出现 1 次（§23）
- 无 PBUH/SAW 缩写，用全称（§31）

### F24. 歧义问题处理
```
Q: "What is the ruling on insurance?"
```
**验证** [§25]:
- 应识别歧义（人寿保险 vs 车险 vs 伊斯兰保险 Takaful）
- 回答最合理的解读
- 主动说明："If you meant [alternative], please clarify"

### F25. 超出 KB 范围的优雅拒绝
```
Q: "What is the Ahmadiyya position on the finality of prophethood?"
```
**验证** [§6,§2]:
- 如果 KB 没有相关内容 → 优雅拒绝
- 不能用 LLM 自身知识回答
- 使用标准拒绝模板
- 建议咨询学者

---

## 测试执行检查清单

每个测试完成后检查：

| 检查项 | 对应条款 | PASS 标准 |
|--------|----------|-----------|
| 直接结论先行 | §20 | 第 1-2 句就回答核心问题 |
| 引用格式正确 | §12 | Quran X:Y - Sahih International |
| Sources 排序 | §14 | Quran > Hadith > Tafsir > Fiqh |
| 无 orphan 引用 | §11 | 每个 [N] 有对应 Sources 条目 |
| 无第一人称 | §18 | 无 "I believe" / "I think" |
| Closing phrase | §23 | 恰好 1 次，在最末尾 |
| 无 emoji | §31 | 无 |
| 无缩写 | §31 | 无 PBUH/SAW |
| 语言跟随 | §16 | 用户什么语言就什么语言回 |
| 回答长度适配 | §21 | 简单:1-3句 / 中等:1-2段 / 复杂:3-5段 |
| TTFT | 性能 | < 5s（前 6 轮）|
| 总时间 | 性能 | < 20s |
| Tool calls | 性能 | ≤ 2 次 |

## Qdrant 验证命令模板

```bash
# 验证 Quran verse 内容是否匹配引用编号
curl -s -X POST http://localhost:6333/collections/kb_imam_v2_1024/points/scroll \
  -H "Content-Type: application/json" \
  -d '{"filter":{"must":[{"key":"verse_key","match":{"value":"22:53"}}]},"limit":1,"with_payload":true,"with_vectors":false}'

# 验证 Hadith 编号
curl -s -X POST http://localhost:6333/collections/kb_imam_v2_1024/points/scroll \
  -H "Content-Type: application/json" \
  -d '{"filter":{"must":[{"key":"source_type","match":{"value":"hadith"}},{"key":"citation_text","match":{"text":"Bukhari, Book 2, Hadith 7"}}]},"limit":1,"with_payload":true,"with_vectors":false}'

# 检查 source_type 分布
for t in quran hadith tafseer fiqh aqeedah seerah dua; do
  count=$(curl -s -X POST http://localhost:6333/collections/kb_imam_v2_1024/points/count \
    -H "Content-Type: application/json" \
    -d "{\"filter\":{\"must\":[{\"key\":\"source_type\",\"match\":{\"value\":\"$t\"}}]}}" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['count'])")
  echo "$t: $count"
done
```
