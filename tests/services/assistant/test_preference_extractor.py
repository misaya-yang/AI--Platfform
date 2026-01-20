"""Preference extraction tests."""

from src.services.assistant.memory.preference_extractor import extract_preferences


def test_extract_language_preference():
    prefs = extract_preferences("以后用中文回复")
    assert prefs.get("language") == "zh"
