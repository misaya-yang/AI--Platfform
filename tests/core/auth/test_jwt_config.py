"""Tests for JWT configuration helpers."""

import src.core.auth.jwt_config as jwt_config_mod
from src.core.auth.jwt_config import (
    DEFAULT_JWT_ALGORITHM,
    DEFAULT_JWT_SECRET,
    get_jwt_algorithms,
    get_jwt_secret,
)


class TestGetJwtSecret:
    def setup_method(self):
        # Reset global warning flag between tests
        jwt_config_mod._warned_default_secret = False

    def test_configured_secret_returned(self):
        assert get_jwt_secret("my-strong-secret") == "my-strong-secret"

    def test_none_returns_default(self):
        assert get_jwt_secret(None) == DEFAULT_JWT_SECRET

    def test_empty_returns_default(self):
        assert get_jwt_secret("") == DEFAULT_JWT_SECRET

    def test_warning_logged_once(self, caplog):
        jwt_config_mod._warned_default_secret = False
        get_jwt_secret(None)
        assert "default JWT secret" in caplog.text
        caplog.clear()
        get_jwt_secret(None)
        assert caplog.text == ""  # No second warning


class TestGetJwtAlgorithms:
    def test_configured_algorithms(self):
        assert get_jwt_algorithms(["RS256", "HS384"]) == ["RS256", "HS384"]

    def test_none_returns_default(self):
        assert get_jwt_algorithms(None) == [DEFAULT_JWT_ALGORITHM]

    def test_empty_list_returns_default(self):
        assert get_jwt_algorithms([]) == [DEFAULT_JWT_ALGORITHM]
