from types import SimpleNamespace

import asyncpg
import pytest
from fastapi import FastAPI, HTTPException, Request

from src.api.v1.exams import list_exams
from src.api.v1.quiz import QuizGenerateRequest, generate_quiz, list_quizzes
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
async def test_quiz_generation_still_requires_model_registry():
    with pytest.raises(HTTPException) as exc_info:
        await generate_quiz(
            QuizGenerateRequest(dataset_ids=["dataset_1"]),
            _request_with_state(database=_EmptyQuizDb(), knowledge_service=SimpleNamespace()),
            user=_admin_user(),
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Model registry not available"
