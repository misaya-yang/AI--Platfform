"""Fixtures for knowledge tests."""

from pathlib import Path

import pytest


@pytest.fixture
def pdf_path():
    """Default PDF path for testing."""
    # Try to find a test PDF
    test_paths = [
        "/Users/misaya.yanghejazfs.com.au/Downloads/Fiqh of Marriage.pdf",
        "tests/data/test.pdf",
        "test.pdf",
    ]
    for path in test_paths:
        if Path(path).exists():
            return path
    # Return default even if not exists - tests should handle missing file
    return test_paths[0]
