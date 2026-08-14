"""Quiz grading primitive.

Generation and orchestration moved to apps/assistant-service/core/quiz/
(product-convergence PC-03). The grader stays here because the gateway
grades anonymous share submissions in-process (see src/api/v1/quiz.py).
"""

from .quiz_grader import QuizGrader

__all__ = ["QuizGrader"]
