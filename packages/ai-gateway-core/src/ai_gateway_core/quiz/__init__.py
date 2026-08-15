"""Shared quiz grading and persistence primitives.

Generation stays in apps/assistant-service/core/quiz/ (product-convergence
PC-03). Gateway-facing read/submit/delete operations live here so the gateway
never imports the assistant-service application package.
"""

from .quiz_access_service import QuizAccessService
from .quiz_grader import QuizGrader

__all__ = ["QuizAccessService", "QuizGrader"]
