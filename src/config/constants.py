"""Centralized constants for the AI Gateway.

Replaces magic numbers scattered across the codebase with named constants,
grouped by category. All values are importable and overridable via settings
where applicable.
"""

from __future__ import annotations

# =============================================================================
# Timeouts (seconds)
# =============================================================================

TIMEOUT_SHORT = 2.0
TIMEOUT_DEFAULT = 5.0
TIMEOUT_MEDIUM = 10.0
TIMEOUT_LONG = 30.0
TIMEOUT_REQUEST = 60.0
TIMEOUT_UPLOAD = 90.0
TIMEOUT_PROCESSING = 120.0
TIMEOUT_HEAVY = 300.0

# =============================================================================
# Batch Sizes
# =============================================================================

BATCH_SIZE_SMALL = 8
BATCH_SIZE_DEFAULT = 16
BATCH_SIZE_MEDIUM = 24
BATCH_SIZE_LARGE = 32
BATCH_SIZE_XLARGE = 50

# =============================================================================
# Embedding Dimensions
# =============================================================================

EMBEDDING_DIM_SMALL = 512
EMBEDDING_DIM_MEDIUM = 768
EMBEDDING_DIM_LARGE = 1024

# =============================================================================
# Cache Limits
# =============================================================================

CACHE_SIZE_SMALL = 200
CACHE_SIZE_MEDIUM = 500
CACHE_SIZE_LARGE = 1000
CACHE_SIZE_XLARGE = 10000

# =============================================================================
# Rate Limits
# =============================================================================

RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_ANONYMOUS = 10
RATE_LIMIT_NORMAL = 60
RATE_LIMIT_PREMIUM = 300
RATE_LIMIT_ENTERPRISE = 1000
RATE_LIMIT_GLOBAL = 10000

# =============================================================================
# Chunk Sizes (tokens/characters)
# =============================================================================

CHUNK_SIZE_SMALL = 500
CHUNK_SIZE_MEDIUM = 800
CHUNK_SIZE_DEFAULT = 1000
CHUNK_SIZE_LARGE = 2000
CHUNK_OVERLAP_SMALL = 50
CHUNK_OVERLAP_DEFAULT = 200
CHUNK_OVERLAP_LARGE = 300
