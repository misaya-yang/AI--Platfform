"""Quiz domain logic for the assistant runtime.

Moved from packages/ai-gateway-core (product-convergence PC-03): quiz
generation and orchestration are assistant-side domain logic, not shared
platform primitives. Grading stays in ai_gateway_core.quiz.quiz_grader
because the gateway grades anonymous share submissions in-process.
"""

from .quiz_generator import QuizGenerator
from .quiz_service import QuizService

__all__ = ["QuizGenerator", "QuizService"]
