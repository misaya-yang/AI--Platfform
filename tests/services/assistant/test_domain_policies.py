from __future__ import annotations

from assistant_service.core.domain_policies import ImamPolicy


def test_imam_policy_flags_duplicate_consultation_reminder() -> None:
    policy = ImamPolicy()
    answer = (
        "This is an answer paragraph with citation [1].\n\n"
        "Important Note: This answer addresses the general Islamic principle. "
        "Please consult with a qualified Islamic scholar before making a final decision.\n\n"
        "Sources:\n[1] Quran 2:173\n\n"
        "All information provided is sourced from authenticated Islamic materials. "
        "For matters requiring personal guidance based on your specific circumstances, "
        "please consult with a qualified Islamic scholar."
    )

    issues = policy.validate_answer(answer)
    assert "duplicate_consultation_reminder" in issues


def test_imam_policy_sanitize_answer_keeps_single_closing_phrase() -> None:
    policy = ImamPolicy()
    answer = (
        "Ruling paragraph with citation [1].\n\n"
        "Important Note: This answer addresses the general Islamic principle. "
        "Your specific situation may have unique circumstances. "
        "Please consult with a qualified Islamic scholar who understands your local context.\n\n"
        "Sources:\n[1] Quran 5:3\n\n"
        "All information provided is sourced from authenticated Islamic materials. "
        "For matters requiring personal guidance based on your specific circumstances, "
        "please consult with a qualified Islamic scholar."
    )

    sanitized = policy.sanitize_answer(answer)

    # Closing phrase must appear once and remain at the end.
    assert sanitized.count(policy.config.closing_phrase) == 1
    assert sanitized.strip().endswith(policy.config.closing_phrase)

    # Redundant advisory block should be removed.
    assert "Important Note:" not in sanitized
