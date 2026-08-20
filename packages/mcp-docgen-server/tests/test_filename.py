from docgen.renderers.filename import safe_document_stem


def test_safe_document_stem_preserves_readable_unicode() -> None:
    assert safe_document_stem("通用 Agent 回归确认", fallback="document") == "通用_Agent_回归确认"


def test_safe_document_stem_blocks_path_segments_and_collapses_noise() -> None:
    assert safe_document_stem("../季度/复盘\\最终版", fallback="document") == "季度_复盘_最终版"


def test_safe_document_stem_uses_fallback_for_punctuation_only() -> None:
    assert safe_document_stem("../...", fallback="presentation") == "presentation"


def test_safe_document_stem_rewrites_windows_reserved_device_names() -> None:
    # Reserved regardless of case or extension ("CON.txt" is still the
    # console device on Windows), so only the head before the first dot
    # must change.
    assert safe_document_stem("CON", fallback="document") == "CO_"
    assert safe_document_stem("con", fallback="document") == "co_"
    assert safe_document_stem("NUL.v2", fallback="document") == "NU_.v2"
    assert safe_document_stem("COM1", fallback="document") == "COM_"
    assert safe_document_stem("lpt9", fallback="document") == "lpt_"


def test_safe_document_stem_keeps_names_that_only_look_reserved() -> None:
    assert safe_document_stem("CONSOLE", fallback="document") == "CONSOLE"
    assert safe_document_stem("connect", fallback="document") == "connect"
    assert safe_document_stem("AUXILIARY notes", fallback="document") == "AUXILIARY_notes"
    # An underscore after the device name breaks the reservation.
    assert safe_document_stem("COM1 report", fallback="document") == "COM1_report"


def test_safe_document_stem_reserved_name_from_truncation() -> None:
    # Truncation itself may create a reserved head; the check runs on the
    # final stem.
    assert safe_document_stem("CONSOLE output", fallback="document", max_length=3) == "CO_"
