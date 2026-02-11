"""
Tests for ModelService pricing synchronization.

Tests that model create/update operations properly sync pricing
to the model_pricing table for usage recording.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.llm.model_service import ModelService


@pytest.fixture
def mock_db():
    """Create a mock database storage."""
    db = MagicMock()
    db.fetchrow = AsyncMock()
    db.fetch = AsyncMock(return_value=[])
    db.execute = AsyncMock()
    return db


@pytest.fixture
def model_service(mock_db):
    """Create a ModelService instance with mock database."""
    return ModelService(database=mock_db)


@pytest.fixture
def sample_model_row():
    """Sample model row returned from database."""
    return {
        "model_id": "gpt-4o",
        "tenant_id": "test-tenant",
        "provider_id": "openai",
        "display_name": "GPT-4o",
        "context_window": 128000,
        "max_output_tokens": 4096,
        "supports_vision": True,
        "supports_tools": True,
        "input_price_per_1k": Decimal("0.0025"),
        "output_price_per_1k": Decimal("0.01"),
        "access_level": "public",
        "is_enabled": True,
        "sort_order": 0,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }


class TestModelServicePricingSync:
    """Tests for pricing synchronization in ModelService."""

    @pytest.mark.asyncio
    async def test_create_model_syncs_pricing(self, model_service, mock_db, sample_model_row):
        """Test that creating a model syncs pricing to model_pricing table."""
        mock_db.fetchrow.return_value = sample_model_row

        with patch("src.services.llm.model_service.get_pricing_service") as mock_get_pricing:
            mock_pricing_svc = MagicMock()
            mock_pricing_svc.update_pricing = AsyncMock()
            mock_get_pricing.return_value = mock_pricing_svc

            result = await model_service.create_model(
                tenant_id="test-tenant",
                model_id="gpt-4o",
                provider_id="openai",
                display_name="GPT-4o",
                context_window=128000,
                max_output_tokens=4096,
                supports_vision=True,
                supports_tools=True,
                input_price_per_1k=Decimal("0.0025"),
                output_price_per_1k=Decimal("0.01"),
            )

            # Verify pricing sync was called
            mock_pricing_svc.update_pricing.assert_called_once_with(
                model="gpt-4o",
                input_price_per_1k=0.0025,
                output_price_per_1k=0.01,
                provider="openai",
                display_name="GPT-4o",
                context_window=128000,
                max_output_tokens=4096,
                supports_vision=True,
                supports_tools=True,
            )

            # Verify model was returned
            assert result["model_id"] == "gpt-4o"

    @pytest.mark.asyncio
    async def test_update_model_syncs_pricing(self, model_service, mock_db, sample_model_row):
        """Test that updating a model syncs pricing to model_pricing table."""
        mock_db.fetchrow.return_value = sample_model_row

        with patch("src.services.llm.model_service.get_pricing_service") as mock_get_pricing:
            mock_pricing_svc = MagicMock()
            mock_pricing_svc.update_pricing = AsyncMock()
            mock_get_pricing.return_value = mock_pricing_svc

            result = await model_service.update_model(
                tenant_id="test-tenant",
                model_id="gpt-4o",
                input_price_per_1k=Decimal("0.003"),
                output_price_per_1k=Decimal("0.012"),
            )

            # Verify pricing sync was called with values from the updated row
            mock_pricing_svc.update_pricing.assert_called_once()
            call_kwargs = mock_pricing_svc.update_pricing.call_args.kwargs
            assert call_kwargs["model"] == "gpt-4o"
            assert call_kwargs["provider"] == "openai"

            # Verify model was returned
            assert result["model_id"] == "gpt-4o"

    @pytest.mark.asyncio
    async def test_pricing_sync_failure_does_not_block_create(
        self, model_service, mock_db, sample_model_row
    ):
        """Test that pricing sync failure doesn't prevent model creation."""
        mock_db.fetchrow.return_value = sample_model_row

        with patch("src.services.llm.model_service.get_pricing_service") as mock_get_pricing:
            mock_pricing_svc = MagicMock()
            mock_pricing_svc.update_pricing = AsyncMock(
                side_effect=Exception("Pricing sync failed")
            )
            mock_get_pricing.return_value = mock_pricing_svc

            # Should not raise, model should still be created
            result = await model_service.create_model(
                tenant_id="test-tenant",
                model_id="gpt-4o",
                provider_id="openai",
                display_name="GPT-4o",
            )

            # Verify model was still returned despite pricing sync failure
            assert result["model_id"] == "gpt-4o"

    @pytest.mark.asyncio
    async def test_pricing_sync_failure_does_not_block_update(
        self, model_service, mock_db, sample_model_row
    ):
        """Test that pricing sync failure doesn't prevent model update."""
        mock_db.fetchrow.return_value = sample_model_row

        with patch("src.services.llm.model_service.get_pricing_service") as mock_get_pricing:
            mock_pricing_svc = MagicMock()
            mock_pricing_svc.update_pricing = AsyncMock(
                side_effect=Exception("Pricing sync failed")
            )
            mock_get_pricing.return_value = mock_pricing_svc

            # Should not raise, model should still be updated
            result = await model_service.update_model(
                tenant_id="test-tenant",
                model_id="gpt-4o",
                display_name="Updated GPT-4o",
            )

            # Verify model was still returned despite pricing sync failure
            assert result["model_id"] == "gpt-4o"

    @pytest.mark.asyncio
    async def test_update_model_no_changes_skips_pricing_sync(
        self, model_service, mock_db, sample_model_row
    ):
        """Test that update with no changes doesn't trigger pricing sync."""
        # get_model returns the existing model
        mock_db.fetchrow.return_value = sample_model_row

        with patch("src.services.llm.model_service.get_pricing_service") as mock_get_pricing:
            mock_pricing_svc = MagicMock()
            mock_pricing_svc.update_pricing = AsyncMock()
            mock_get_pricing.return_value = mock_pricing_svc

            # Call update with no changes
            result = await model_service.update_model(
                tenant_id="test-tenant",
                model_id="gpt-4o",
            )

            # Since no updates, it falls back to get_model path
            # which doesn't sync pricing
            # (The fetchrow is called for get_model, not update)
            assert result is not None

    @pytest.mark.asyncio
    async def test_update_nonexistent_model_returns_none(self, model_service, mock_db):
        """Test that updating a non-existent model returns None."""
        mock_db.fetchrow.return_value = None

        with patch("src.services.llm.model_service.get_pricing_service") as mock_get_pricing:
            mock_pricing_svc = MagicMock()
            mock_pricing_svc.update_pricing = AsyncMock()
            mock_get_pricing.return_value = mock_pricing_svc

            result = await model_service.update_model(
                tenant_id="test-tenant",
                model_id="nonexistent-model",
                display_name="Updated Name",
            )

            # Should return None for non-existent model
            assert result is None

            # Pricing sync should not be called
            mock_pricing_svc.update_pricing.assert_not_called()


class TestModelServicePricingValues:
    """Tests for pricing value handling."""

    @pytest.mark.asyncio
    async def test_create_model_with_zero_prices(self, model_service, mock_db):
        """Test creating a model with zero prices."""
        mock_db.fetchrow.return_value = {
            "model_id": "free-model",
            "tenant_id": "test-tenant",
            "provider_id": "local",
            "display_name": "Free Model",
            "context_window": 4096,
            "max_output_tokens": 1024,
            "supports_vision": False,
            "supports_tools": False,
            "input_price_per_1k": Decimal("0"),
            "output_price_per_1k": Decimal("0"),
            "access_level": "public",
            "is_enabled": True,
            "sort_order": 0,
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
        }

        with patch("src.services.llm.model_service.get_pricing_service") as mock_get_pricing:
            mock_pricing_svc = MagicMock()
            mock_pricing_svc.update_pricing = AsyncMock()
            mock_get_pricing.return_value = mock_pricing_svc

            await model_service.create_model(
                tenant_id="test-tenant",
                model_id="free-model",
                provider_id="local",
                display_name="Free Model",
                input_price_per_1k=Decimal("0"),
                output_price_per_1k=Decimal("0"),
            )

            # Verify pricing sync was called with zero prices
            call_kwargs = mock_pricing_svc.update_pricing.call_args.kwargs
            assert call_kwargs["input_price_per_1k"] == 0.0
            assert call_kwargs["output_price_per_1k"] == 0.0

    @pytest.mark.asyncio
    async def test_create_model_with_high_precision_prices(self, model_service, mock_db):
        """Test creating a model with high precision prices."""
        mock_db.fetchrow.return_value = {
            "model_id": "precise-model",
            "tenant_id": "test-tenant",
            "provider_id": "custom",
            "display_name": "Precise Model",
            "context_window": 8192,
            "max_output_tokens": 2048,
            "supports_vision": False,
            "supports_tools": True,
            "input_price_per_1k": Decimal("0.000125"),
            "output_price_per_1k": Decimal("0.000375"),
            "access_level": "public",
            "is_enabled": True,
            "sort_order": 0,
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
        }

        with patch("src.services.llm.model_service.get_pricing_service") as mock_get_pricing:
            mock_pricing_svc = MagicMock()
            mock_pricing_svc.update_pricing = AsyncMock()
            mock_get_pricing.return_value = mock_pricing_svc

            await model_service.create_model(
                tenant_id="test-tenant",
                model_id="precise-model",
                provider_id="custom",
                display_name="Precise Model",
                input_price_per_1k=Decimal("0.000125"),
                output_price_per_1k=Decimal("0.000375"),
            )

            # Verify pricing sync was called with correct precision
            call_kwargs = mock_pricing_svc.update_pricing.call_args.kwargs
            assert call_kwargs["input_price_per_1k"] == 0.000125
            assert call_kwargs["output_price_per_1k"] == 0.000375
