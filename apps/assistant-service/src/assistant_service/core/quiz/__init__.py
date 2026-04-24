"""Quiz service re-export shim.

Phase 5d moved canonical implementations to ``ai_gateway_core.quiz`` so
gateway routes (quiz / exams / conversation_shares) can import without
a compile-time dep on ``assistant_service``. This shim keeps existing
``from assistant_service.core.quiz import …`` sites (quiz_tool, etc.)
working — delete once every AS caller migrates to the shared module.
"""

from ai_gateway_core.quiz import (
    ExamService,
    QuizGenerator,
    QuizGrader,
    QuizService,
    QuizShareManager,
)

__all__ = [
    "ExamService",
    "QuizGenerator",
    "QuizGrader",
    "QuizService",
    "QuizShareManager",
]
