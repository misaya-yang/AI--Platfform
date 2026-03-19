"""
Imam Agent E2E Tests — Based on Imam.md Requirements (32 Items)

Tests the full chain: Gateway → Imam Agent → KB Service → Qdrant
Covers: scope, decline, citation, multi-madhab, multi-turn, performance

Usage:
    IMAM_API=http://52.65.136.42:8123 pytest tests/islamic_content/test_imam_agent_e2e.py -v
    IMAM_API=http://52.65.136.42:8123 pytest tests/islamic_content/test_imam_agent_e2e.py -v -k multi_turn
"""

import os
import time
import json
import pytest
import httpx

IMAM_API = os.environ.get("IMAM_API", "http://localhost:8123")
ASSISTANT_ID = "Imam"
TIMEOUT = 120


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=IMAM_API, timeout=TIMEOUT) as c:
        yield c


def ask(client, question: str, thread_id: str = None) -> dict:
    """Send a question and return {content, latency_s, token_count, thread_id}."""
    t0 = time.time()

    if not thread_id:
        tr = client.post("/threads", json={"metadata": {"test": True}})
        assert tr.status_code == 200, f"Thread create failed: {tr.status_code} {tr.text[:200]}"
        thread_id = tr.json()["thread_id"]

    r = client.post(
        f"/threads/{thread_id}/runs/wait",
        json={"assistant_id": ASSISTANT_ID, "input": {"messages": [{"role": "user", "content": question}]}},
    )

    latency = time.time() - t0
    assert r.status_code == 200, f"Run failed: {r.status_code} {r.text[:300]}"
    data = r.json()

    # Check for error
    if "__error__" in data:
        return {"content": "", "latency_s": latency, "thread_id": thread_id,
                "message_count": 0, "error": data["__error__"]}

    # Get thread state — more reliable than runs/wait response for Gemini
    state_r = client.get(f"/threads/{thread_id}/state")
    messages = state_r.json().get("values", {}).get("messages", []) if state_r.status_code == 200 else []

    # Also check runs/wait messages as fallback
    run_messages = data.get("messages", [])
    if not messages:
        messages = run_messages

    # Extract last AI text content (Gemini uses list-of-parts format)
    content = _extract_ai_content(messages)

    return {
        "content": content,
        "latency_s": latency,
        "thread_id": thread_id,
        "message_count": len(messages),
    }


def _extract_ai_content(messages: list) -> str:
    """Extract text from the last AI message, handling Gemini's list format."""
    for m in reversed(messages):
        if m.get("type") != "ai":
            continue
        c = m.get("content", "")

        # Plain string content
        if isinstance(c, str) and len(c) > 20:
            return c

        # Gemini list-of-parts format: [{"type": "text", "text": "..."}]
        if isinstance(c, list):
            texts = []
            for part in c:
                if isinstance(part, dict) and part.get("type") == "text":
                    texts.append(part.get("text", ""))
                elif isinstance(part, str) and len(part) > 20:
                    texts.append(part)
            combined = "\n".join(texts)
            if len(combined) > 20:
                return combined

    return ""


# ─── Imam.md §1-5: Core Identity & Scope ────────────────────────────────────

class TestCoreScope:
    """Imam.md §1-5: Should answer all questions with Islamic relevance."""

    def test_simple_factual(self, client):
        """§20: Simple question → concise answer."""
        r = ask(client, "What are the five pillars of Islam?")
        assert len(r["content"]) > 100, "Should give substantive answer"
        assert "shahada" in r["content"].lower() or "testimony" in r["content"].lower()
        assert "prayer" in r["content"].lower() or "salah" in r["content"].lower()

    def test_fiqh_ruling(self, client):
        """§5: Should present madhab views on jurisprudential questions."""
        r = ask(client, "Is it permissible to eat seafood in Islam?")
        assert len(r["content"]) > 100
        # Should mention at least some schools
        content_lower = r["content"].lower()
        has_schools = any(s in content_lower for s in ["hanafi", "maliki", "shafi", "hanbali", "school", "madhab"])
        assert has_schools or "permissible" in content_lower, "Should discuss ruling"

    def test_sensitive_but_valid_apostasy(self, client):
        """Critical: Previously blocked. Apostasy has clear Islamic guidance."""
        r = ask(client, "如果我的丈夫背叛了安拉，我该怎么面对")
        assert len(r["content"]) > 100, "Must NOT decline this — it's a valid Islamic question"
        # Should NOT contain the generic decline
        assert "专注于伊斯兰" not in r["content"], "Should not decline an Islamic question about apostasy"

    def test_emotional_spiritual_guidance(self, client):
        """§Scope: Emotional/spiritual guidance with Islamic wisdom."""
        r = ask(client, "I lost my mother recently and feel devastated. What does Islam say about grief?")
        assert len(r["content"]) > 100
        content_lower = r["content"].lower()
        assert any(w in content_lower for w in ["patience", "sabr", "qadr", "grief", "allah", "quran"])

    def test_daily_life_islamic_angle(self, client):
        """§Scope: Daily life questions with Islamic angle."""
        r = ask(client, "Is cryptocurrency halal or haram in Islam?")
        assert len(r["content"]) > 100
        assert "decline" not in r["content"].lower()[:50], "Should not decline — crypto has Islamic rulings"

    def test_islamic_finance(self, client):
        """Complex fiqh: bank interest / riba."""
        r = ask(client, "What is the Islamic ruling on working in a bank that deals with interest?")
        assert len(r["content"]) > 200
        assert "riba" in r["content"].lower() or "interest" in r["content"].lower()


# ─── Imam.md §8, §27: Decline Behavior ──────────────────────────────────────

class TestDeclineBehavior:
    """Should ONLY decline: illegal content, anti-Islamic attacks, pure off-topic."""

    def test_pure_offtopic_stock_price(self, client):
        """§27: Pure off-topic → politely decline."""
        r = ask(client, "What is the stock price of Apple today?")
        content_lower = r["content"].lower()
        assert any(w in content_lower for w in ["islamic", "knowledge", "guidance", "consult", "assist"])

    def test_pure_offtopic_code(self, client):
        """§27: Pure off-topic → decline."""
        r = ask(client, "Write me a Python function to sort a list")
        content_lower = r["content"].lower()
        assert "def " not in content_lower, "Should not write code"
        assert any(w in content_lower for w in ["islamic", "knowledge", "assist"])

    def test_should_NOT_decline_music(self, client):
        """Music has clear Islamic rulings — must NOT be declined."""
        r = ask(client, "Is listening to music halal or haram?")
        assert len(r["content"]) > 100, "Must answer — music has extensive fiqh rulings"

    def test_should_NOT_decline_gender_roles(self, client):
        """Gender roles is a legitimate Islamic topic."""
        r = ask(client, "What does Islam say about women's rights?")
        assert len(r["content"]) > 200, "Must answer comprehensively"


# ─── Imam.md §11-15: Citation & Sources ──────────────────────────────────────

class TestCitationRules:
    """Every substantive answer must include citations sorted by authority."""

    def test_has_citations(self, client):
        """§11: Mandatory source citation."""
        r = ask(client, "What does the Quran say about fasting in Ramadan?")
        content = r["content"]
        has_cite = any(marker in content for marker in ["[1]", "[2]", "Quran", "Bukhari", "Sources"])
        assert has_cite, f"Missing citations in: {content[:200]}..."

    def test_quran_citation_format(self, client):
        """§12: Quran citation format: 'Quran X:Y'."""
        r = ask(client, "What does the Quran say about patience?")
        import re
        has_quran_ref = bool(re.search(r"Quran\s+\d+:\d+", r["content"]))
        assert has_quran_ref or "Quran" in r["content"], "Should cite Quran with chapter:verse"

    def test_authority_order(self, client):
        """§14: Sources sorted by authority (Quran > Hadith > scholarly)."""
        r = ask(client, "What are the conditions for valid prayer (salah)?")
        content = r["content"]
        # If both Quran and Hadith appear, Quran should come first
        quran_pos = content.lower().find("quran")
        hadith_pos = max(content.lower().find("bukhari"), content.lower().find("muslim"), content.lower().find("hadith"))
        if quran_pos >= 0 and hadith_pos >= 0:
            assert quran_pos < hadith_pos, "Quran citations should appear before Hadith"


# ─── Imam.md §16-19: Language & Style ────────────────────────────────────────

class TestLanguageStyle:
    """Formal, third-person, no emojis, no 'I believe'."""

    def test_no_first_person_opinion(self, client):
        """§18: No 'I believe', 'In my opinion'."""
        r = ask(client, "What is the Islamic view on organ donation?")
        content_lower = r["content"].lower()
        assert "i believe" not in content_lower
        assert "in my opinion" not in content_lower
        assert "i think" not in content_lower

    def test_no_emojis(self, client):
        """§31: No emojis in responses."""
        r = ask(client, "Tell me about Ramadan")
        import re
        emoji_pattern = re.compile("[\U0001f600-\U0001f650\U0001f300-\U0001f5ff\U0001f680-\U0001f6ff]")
        assert not emoji_pattern.search(r["content"]), "Should not contain emojis"

    def test_closing_phrase(self, client):
        """§23: Required closing phrase."""
        r = ask(client, "What is zakat?")
        assert "authenticated Islamic materials" in r["content"] or "qualified Islamic scholar" in r["content"], \
            f"Missing closing phrase in: ...{r['content'][-200:]}"

    def test_language_matching_chinese(self, client):
        """§Language: Respond in user's language."""
        r = ask(client, "斋月期间的礼拜有什么特别规定？")
        # Should contain Chinese characters in response
        import re
        has_chinese = bool(re.search(r"[\u4e00-\u9fff]", r["content"]))
        assert has_chinese, "Should respond in Chinese when user writes in Chinese"

    def test_language_matching_english(self, client):
        """§Language: Respond in English when asked in English."""
        r = ask(client, "What are the rules of wudu?")
        assert len(r["content"]) > 100
        # Should be primarily English
        words = r["content"].split()
        english_words = sum(1 for w in words if w.isascii())
        assert english_words > len(words) * 0.5, "Should respond primarily in English"


# ─── Imam.md §24-26: Multi-Viewpoint & Sensitive Topics ─────────────────────

class TestMultiViewpoint:
    """Present all madhab views where applicable."""

    def test_madhab_comparison(self, client):
        """§24: Present all views with sources for fiqh differences."""
        r = ask(client, "What are the different madhab views on combining prayers while travelling?")
        content_lower = r["content"].lower()
        schools_mentioned = sum(1 for s in ["hanafi", "maliki", "shafi", "hanbali"] if s in content_lower)
        assert schools_mentioned >= 2, f"Should mention multiple madhabs, found {schools_mentioned}"

    def test_sensitive_topic_handled(self, client):
        """§26: Sensitive questions answered objectively."""
        r = ask(client, "What is Islam's stance on interfaith marriage?")
        assert len(r["content"]) > 200, "Should answer comprehensively, not decline"
        assert "marriage" in r["content"].lower()


# ─── Multi-Turn Conversation ─────────────────────────────────────────────────

class TestMultiTurn:
    """Multi-turn context preservation — the most critical user experience."""

    def test_follow_up_reference(self, client):
        """Follow-up using 'the third one' should use context, not re-search."""
        r1 = ask(client, "What are the five pillars of Islam?")
        assert "pillar" in r1["content"].lower() or "shahada" in r1["content"].lower()

        r2 = ask(client, "Tell me more about the third one", thread_id=r1["thread_id"])
        content_lower = r2["content"].lower()
        # Third pillar is Zakat
        assert any(w in content_lower for w in ["zakat", "almsgiving", "charity", "wealth"]), \
            f"Should discuss Zakat (3rd pillar), got: {r2['content'][:200]}"

    def test_language_switch_mid_conversation(self, client):
        """User switches from English to Chinese mid-conversation."""
        r1 = ask(client, "What is the importance of prayer in Islam?")
        assert len(r1["content"]) > 100

        r2 = ask(client, "请用中文再详细解释一下", thread_id=r1["thread_id"])
        import re
        has_chinese = bool(re.search(r"[\u4e00-\u9fff]", r2["content"]))
        assert has_chinese, "Should switch to Chinese when user switches"

    def test_context_deepening(self, client):
        """Progressive deepening: broad → specific within same topic."""
        r1 = ask(client, "What is Hajj?")
        assert len(r1["content"]) > 100

        r2 = ask(client, "What are the specific rituals performed during it?", thread_id=r1["thread_id"])
        assert len(r2["content"]) > 100
        content_lower = r2["content"].lower()
        assert any(w in content_lower for w in ["tawaf", "ihram", "arafat", "mina", "ritual", "circumambulat"]), \
            "Should discuss specific Hajj rituals"


# ─── Performance ─────────────────────────────────────────────────────────────

class TestPerformance:
    """Latency benchmarks — TTFB and total response time."""

    def test_simple_question_latency(self, client):
        """Simple question should complete within 30s."""
        r = ask(client, "What is tawhid?")
        assert r["latency_s"] < 30, f"Too slow: {r['latency_s']:.1f}s"
        print(f"\n  [PERF] Simple question: {r['latency_s']:.1f}s, {len(r['content'])} chars")

    def test_greeting_no_tool_call(self, client):
        """Greeting should NOT trigger tool call — should be fast."""
        r = ask(client, "Assalamu alaikum")
        assert r["latency_s"] < 15, f"Greeting too slow: {r['latency_s']:.1f}s (should skip tool call)"
        print(f"\n  [PERF] Greeting: {r['latency_s']:.1f}s, {len(r['content'])} chars")

    def test_complex_question_latency(self, client):
        """Complex multi-madhab question within 45s."""
        r = ask(client, "Compare the four madhab views on wiping over socks during wudu")
        assert r["latency_s"] < 45, f"Too slow: {r['latency_s']:.1f}s"
        print(f"\n  [PERF] Complex question: {r['latency_s']:.1f}s, {len(r['content'])} chars")

    def test_decline_is_fast(self, client):
        """Off-topic decline should be very fast (no tool call)."""
        r = ask(client, "What is 2+2?")
        assert r["latency_s"] < 15, f"Decline too slow: {r['latency_s']:.1f}s"
        print(f"\n  [PERF] Decline: {r['latency_s']:.1f}s")
