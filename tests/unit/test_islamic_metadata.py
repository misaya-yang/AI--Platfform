from src.services.knowledge.islamic_metadata import IslamicMetadataExtractor
from src.services.knowledge.knowledge_service import KnowledgeService


def test_extract_quran_reference_valid_range():
    extractor = IslamicMetadataExtractor()
    ref = extractor._extract_quran_reference(
        "Allah has permitted trade and forbidden riba (2:275-280).",
        doc_title="Quran - Saheeh International",
    )
    assert ref.get("surah") == 2
    assert ref.get("verse_start") == 275
    assert ref.get("verse_end") == 280


def test_extract_quran_reference_rejects_invalid_verse_range():
    extractor = IslamicMetadataExtractor()
    ref = extractor._extract_quran_reference(
        "Incorrect OCR sample: Quran 3:275-280.",
        doc_title="Quran - Saheeh International",
    )
    assert ref.get("surah") is None
    assert ref.get("verse_start") is None


def test_extract_quran_reference_avoids_generic_number_in_non_quran_doc():
    extractor = IslamicMetadataExtractor()
    ref = extractor._extract_quran_reference(
        "Revenue snapshot 3:275 indicates growth YoY.",
        doc_title="Quarterly Finance Report",
    )
    assert ref.get("surah") is None


def test_extract_quran_reference_supports_arabic_indic_digits():
    extractor = IslamicMetadataExtractor()
    ref = extractor._extract_quran_reference(
        "﴿٢:٢٥٥﴾",
        doc_title="Quran - Saheeh International",
    )
    assert ref.get("surah") == 2
    assert ref.get("verse_start") == 255


def test_islamic_defaults_do_not_infer_from_dataset_name():
    index_config = {}
    updated = KnowledgeService._apply_islamic_dataset_defaults("imam_v2", dict(index_config))
    assert updated == {}


def test_islamic_defaults_align_strict_traceability_when_explicit():
    index_config = {
        "chunking": {"mode": "islamic"},
        "retrieval": {"islamic": {"strict_section_traceability": True}},
    }
    updated = KnowledgeService._apply_islamic_dataset_defaults("any_name", dict(index_config))
    assert updated["chunking"]["strict_section_traceability"] is True


def test_islamic_defaults_apply_profile_defaults_when_explicitly_opted_in():
    index_config = {
        "chunking": {"mode": "islamic"},
        "retrieval": {},
    }
    defaults = {
        "retrieval": {
            "top_k": 8,
            "score_threshold": 0.3,
            "rerank": {
                "enabled": True,
                "provider": "bge",
                "model": "bge-reranker-v2-m3",
            },
            "islamic": {
                "multi_query": True,
                "citation_format": True,
                "authority_sort": True,
                "strict_section_traceability": True,
                "max_expanded_queries": 3,
            },
        }
    }
    updated = KnowledgeService._apply_islamic_dataset_defaults("any_name", dict(index_config), defaults)
    retrieval = updated["retrieval"]
    assert retrieval["top_k"] == 8
    assert retrieval["score_threshold"] == 0.3
    assert retrieval["rerank"]["provider"] == "bge"
    assert retrieval["islamic"]["citation_format"] is True
    assert updated["chunking"]["strict_section_traceability"] is True
