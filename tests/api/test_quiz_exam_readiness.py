from types import SimpleNamespace

import asyncpg
import pytest
from fastapi import FastAPI, HTTPException, Request

from src.api.v1.exams import list_exams
from src.api.v1.quiz import (
    QuizGenerateRequest,
    _get_quiz_service,
    _UnavailableModelRegistry,
    generate_quiz,
    generate_quiz_stream,
    list_quizzes,
)
from src.core.auth.user_resolver import UserContext


def _request_with_state(**state_values) -> Request:
    app = FastAPI()
    for key, value in state_values.items():
        setattr(app.state, key, value)
    return Request(
        {
            "type": "http",
            "app": app,
            "headers": [],
            "method": "GET",
            "path": "/api/v1/test",
        }
    )


def _admin_user() -> UserContext:
    return UserContext(
        user_id="user_1",
        tenant_id="tenant_1",
        is_authenticated=True,
        roles=["admin"],
    )


def _anonymous_user() -> UserContext:
    return UserContext(
        user_id="anon:127.0.0.1",
        tenant_id="public",
        is_authenticated=False,
        roles=["guest"],
    )


class _MissingExamTablesDb:
    async def fetchrow(self, *_args, **_kwargs):
        raise asyncpg.UndefinedTableError('relation "exams" does not exist')


class _EmptyQuizDb:
    async def fetchrow(self, *_args, **_kwargs):
        return {"cnt": 0}

    async def fetch(self, *_args, **_kwargs):
        return []


class _MissingQuizTablesDb:
    async def fetchrow(self, *_args, **_kwargs):
        raise asyncpg.UndefinedTableError('relation "quizzes" does not exist')


@pytest.mark.asyncio
async def test_exam_list_returns_empty_when_exam_tables_are_missing():
    result = await list_exams(
        _request_with_state(database=_MissingExamTablesDb()),
        user=_admin_user(),
    )

    assert result == {"exams": [], "total": 0}


@pytest.mark.asyncio
async def test_quiz_list_does_not_require_model_registry():
    result = await list_quizzes(
        _request_with_state(database=_EmptyQuizDb()),
        user=_admin_user(),
    )

    assert result.quizzes == []
    assert result.total == 0


@pytest.mark.asyncio
async def test_quiz_list_returns_empty_when_quiz_tables_are_missing():
    result = await list_quizzes(
        _request_with_state(database=_MissingQuizTablesDb()),
        user=_admin_user(),
    )

    assert result.quizzes == []
    assert result.total == 0


@pytest.mark.asyncio
async def test_quiz_generation_uses_assistant_adapter_when_gateway_registry_missing():
    with pytest.raises(HTTPException) as exc_info:
        await generate_quiz(
            QuizGenerateRequest(dataset_ids=["dataset_1"]),
            _request_with_state(database=_EmptyQuizDb(), knowledge_service=SimpleNamespace()),
            user=_admin_user(),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail.startswith("No content retrieved")


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", [generate_quiz, generate_quiz_stream])
async def test_quiz_generation_rejects_anonymous_callers_before_model_or_kb_work(handler):
    with pytest.raises(HTTPException) as exc_info:
        await handler(
            QuizGenerateRequest(dataset_ids=["dataset_1"]),
            _request_with_state(),
            user=_anonymous_user(),
        )

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", [generate_quiz, generate_quiz_stream])
async def test_quiz_generation_applies_the_paid_operation_rate_limit(monkeypatch, handler):
    observed: list[tuple[object, UserContext, str]] = []

    async def _rate_limited(request, user, operation):
        observed.append((request, user, operation))
        raise HTTPException(status_code=429, detail="rate limited")

    monkeypatch.setattr("src.api.v1.quiz.enforce_rate_limit", _rate_limited)

    with pytest.raises(HTTPException) as exc_info:
        await handler(
            QuizGenerateRequest(dataset_ids=["dataset_1"]),
            _request_with_state(),
            user=_admin_user(),
        )

    assert exc_info.value.status_code == 429
    assert len(observed) == 1
    assert observed[0][1].user_id == "user_1"
    assert observed[0][2] == "quiz_generate"


def test_quiz_generation_uses_local_fallback_registry_when_enabled(monkeypatch):
    monkeypatch.setenv("QUIZ_DETERMINISTIC_FALLBACK_ENABLED", "1")

    service = _get_quiz_service(
        _request_with_state(database=_EmptyQuizDb()),
        user=_admin_user(),
    )

    assert isinstance(service.generator.model_registry, _UnavailableModelRegistry)
