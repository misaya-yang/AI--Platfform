"""
Unit tests for DocumentParser.

Tests:
- Path resolution from API paths to disk paths
- File type validation
- Document parsing
- Error handling
- Security checks (path traversal prevention)
"""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory structure."""
    with tempfile.TemporaryDirectory() as tmpdir:
        storage_path = Path(tmpdir)
        # Create the uploads directory structure
        uploads_dir = storage_path / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        yield storage_path


@pytest.fixture
def parser(temp_storage):
    """Create a DocumentParser instance with temporary storage."""
    from src.services.assistant.document_parser import DocumentParser

    return DocumentParser(storage_base_path=temp_storage)


@pytest.fixture
def sample_txt_file(temp_storage):
    """Create a sample text file for testing."""
    user_dir = temp_storage / "uploads" / "test_user"
    user_dir.mkdir(parents=True, exist_ok=True)

    file_path = user_dir / "abc123_20240101_120000.txt"
    file_path.write_text("This is a test document.\n\nIt has multiple paragraphs.\n\nEnd of document.")
    return file_path


@pytest.fixture
def sample_md_file(temp_storage):
    """Create a sample markdown file for testing."""
    user_dir = temp_storage / "uploads" / "test_user"
    user_dir.mkdir(parents=True, exist_ok=True)

    file_path = user_dir / "doc456_20240101_120000.md"
    file_path.write_text("# Heading\n\nThis is **bold** text.\n\n## Subheading\n\n- Item 1\n- Item 2")
    return file_path


class TestDocumentParser:
    """Tests for DocumentParser class."""

    def test_init_default_storage_path(self):
        """Test initialization with default storage path."""
        from src.services.assistant.document_parser import DocumentParser, FILE_STORAGE_PATH

        parser = DocumentParser()
        assert parser.storage_base_path == FILE_STORAGE_PATH

    def test_init_custom_storage_path(self, temp_storage):
        """Test initialization with custom storage path."""
        from src.services.assistant.document_parser import DocumentParser

        parser = DocumentParser(storage_base_path=temp_storage)
        assert parser.storage_base_path == temp_storage

    def test_is_supported_valid_types(self, parser):
        """Test is_supported returns True for valid file types."""
        valid_files = [
            "document.pdf",
            "document.docx",
            "document.doc",
            "document.txt",
            "document.md",
            "document.csv",
            "document.xlsx",
            "document.html",
            "document.htm",
        ]
        for file in valid_files:
            assert parser.is_supported(file), f"Expected {file} to be supported"

    def test_is_supported_invalid_types(self, parser):
        """Test is_supported returns False for invalid file types."""
        invalid_files = [
            "image.png",
            "image.jpg",
            "image.gif",
            "archive.zip",
            "executable.exe",
            "script.py",
        ]
        for file in invalid_files:
            assert not parser.is_supported(file), f"Expected {file} to not be supported"

    def test_get_mime_type_valid(self, parser):
        """Test get_mime_type returns correct MIME types."""
        assert parser.get_mime_type("document.pdf") == "application/pdf"
        assert parser.get_mime_type("document.txt") == "text/plain"
        assert parser.get_mime_type("document.html") == "text/html"
        assert parser.get_mime_type("document.csv") == "text/csv"

    def test_get_mime_type_invalid(self, parser):
        """Test get_mime_type returns None for invalid types."""
        assert parser.get_mime_type("image.png") is None
        assert parser.get_mime_type("archive.zip") is None


class TestPathResolution:
    """Tests for path resolution logic."""

    def test_resolve_path_api_format(self, parser, sample_txt_file, temp_storage):
        """Test resolving API path format /uploads/user_id/filename."""
        api_path = "/uploads/test_user/abc123_20240101_120000.txt"
        resolved = parser._resolve_path(api_path)

        assert resolved == sample_txt_file

    def test_resolve_path_without_leading_slash(self, parser, sample_txt_file):
        """Test resolving path without leading slash."""
        api_path = "uploads/test_user/abc123_20240101_120000.txt"
        resolved = parser._resolve_path(api_path)

        assert resolved == sample_txt_file

    def test_resolve_path_file_not_found(self, parser):
        """Test that non-existent file raises DocumentParseError."""
        from src.services.assistant.document_parser import DocumentParseError

        with pytest.raises(DocumentParseError) as exc_info:
            parser._resolve_path("/uploads/test_user/nonexistent.txt")

        assert "File not found" in str(exc_info.value)

    def test_resolve_path_traversal_attack(self, parser, temp_storage):
        """Test that path traversal attempts are blocked."""
        from src.services.assistant.document_parser import DocumentParseError

        # Create a file outside the storage directory
        outside_file = temp_storage.parent / "outside_file.txt"
        outside_file.write_text("Secret content")

        try:
            with pytest.raises(DocumentParseError) as exc_info:
                parser._resolve_path("/uploads/../outside_file.txt")

            assert "Invalid file path" in str(exc_info.value) or "File not found" in str(exc_info.value)
        finally:
            outside_file.unlink(missing_ok=True)

    def test_resolve_path_directory_not_file(self, parser, temp_storage):
        """Test that directory paths raise DocumentParseError."""
        from src.services.assistant.document_parser import DocumentParseError

        # Create a directory where a file would be expected
        dir_path = temp_storage / "uploads" / "test_user" / "not_a_file"
        dir_path.mkdir(parents=True, exist_ok=True)

        with pytest.raises(DocumentParseError) as exc_info:
            parser._resolve_path("/uploads/test_user/not_a_file")

        assert "not a file" in str(exc_info.value).lower()


class TestValidateExtension:
    """Tests for file extension validation."""

    def test_validate_extension_valid(self, parser, sample_txt_file):
        """Test validation passes for supported extensions."""
        ext = parser._validate_extension(sample_txt_file)
        assert ext == ".txt"

    def test_validate_extension_case_insensitive(self, parser, temp_storage):
        """Test validation is case-insensitive."""
        user_dir = temp_storage / "uploads" / "test_user"
        user_dir.mkdir(parents=True, exist_ok=True)

        file_path = user_dir / "document.TXT"
        file_path.write_text("Content")

        ext = parser._validate_extension(file_path)
        assert ext == ".txt"

    def test_validate_extension_invalid(self, parser, temp_storage):
        """Test validation fails for unsupported extensions."""
        from src.services.assistant.document_parser import DocumentParseError

        user_dir = temp_storage / "uploads" / "test_user"
        user_dir.mkdir(parents=True, exist_ok=True)

        file_path = user_dir / "image.png"
        file_path.write_text("Fake image content")

        with pytest.raises(DocumentParseError) as exc_info:
            parser._validate_extension(file_path)

        assert "Unsupported file type" in str(exc_info.value)
        assert ".png" in str(exc_info.value)


class TestParsing:
    """Tests for document parsing functionality."""

    @pytest.mark.asyncio
    async def test_parse_txt_file(self, parser, sample_txt_file):
        """Test parsing a text file."""
        with patch("src.services.assistant.document_parser.partition") as mock_partition:
            # Mock unstructured elements
            mock_element1 = MagicMock()
            mock_element1.text = "This is a test document."
            mock_element2 = MagicMock()
            mock_element2.text = "It has multiple paragraphs."
            mock_element3 = MagicMock()
            mock_element3.text = "End of document."

            mock_partition.return_value = [mock_element1, mock_element2, mock_element3]

            result = await parser.parse("/uploads/test_user/abc123_20240101_120000.txt")

            assert "This is a test document." in result
            assert "It has multiple paragraphs." in result
            assert "End of document." in result
            mock_partition.assert_called_once()

    @pytest.mark.asyncio
    async def test_parse_md_file(self, parser, sample_md_file):
        """Test parsing a markdown file."""
        with patch("src.services.assistant.document_parser.partition") as mock_partition:
            mock_element = MagicMock()
            mock_element.text = "# Heading\nThis is bold text."
            mock_partition.return_value = [mock_element]

            result = await parser.parse("/uploads/test_user/doc456_20240101_120000.md")

            assert "Heading" in result
            mock_partition.assert_called_once()

    @pytest.mark.asyncio
    async def test_parse_empty_document(self, parser, temp_storage):
        """Test parsing an empty document."""
        user_dir = temp_storage / "uploads" / "test_user"
        user_dir.mkdir(parents=True, exist_ok=True)
        empty_file = user_dir / "empty.txt"
        empty_file.write_text("")

        with patch("src.services.assistant.document_parser.partition") as mock_partition:
            mock_partition.return_value = []

            result = await parser.parse("/uploads/test_user/empty.txt")

            assert result == ""

    @pytest.mark.asyncio
    async def test_parse_element_without_text(self, parser, sample_txt_file):
        """Test parsing handles elements without text attribute."""
        with patch("src.services.assistant.document_parser.partition") as mock_partition:
            mock_element1 = MagicMock()
            mock_element1.text = "Has text"
            mock_element2 = MagicMock(spec=[])  # No text attribute
            mock_element3 = MagicMock()
            mock_element3.text = None  # text is None
            mock_element4 = MagicMock()
            mock_element4.text = ""  # Empty text

            mock_partition.return_value = [mock_element1, mock_element2, mock_element3, mock_element4]

            result = await parser.parse("/uploads/test_user/abc123_20240101_120000.txt")

            assert result == "Has text"

    @pytest.mark.asyncio
    async def test_parse_unstructured_import_error(self, parser, sample_txt_file):
        """Test handling when unstructured library is not installed."""
        from src.services.assistant.document_parser import DocumentParseError

        with patch.dict("sys.modules", {"unstructured.partition.auto": None}):
            with patch("src.services.assistant.document_parser.partition", side_effect=ImportError("No module")):
                with pytest.raises(DocumentParseError) as exc_info:
                    await parser.parse("/uploads/test_user/abc123_20240101_120000.txt")

                assert "unstructured" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_parse_generic_error(self, parser, sample_txt_file):
        """Test handling generic parsing errors."""
        from src.services.assistant.document_parser import DocumentParseError

        with patch("src.services.assistant.document_parser.partition") as mock_partition:
            mock_partition.side_effect = Exception("Parsing failed unexpectedly")

            with pytest.raises(DocumentParseError) as exc_info:
                await parser.parse("/uploads/test_user/abc123_20240101_120000.txt")

            assert "Failed to parse document" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_parse_to_text_alias(self, parser, sample_txt_file):
        """Test that parse_to_text is an alias for parse."""
        with patch("src.services.assistant.document_parser.partition") as mock_partition:
            mock_element = MagicMock()
            mock_element.text = "Test content"
            mock_partition.return_value = [mock_element]

            result = await parser.parse_to_text("/uploads/test_user/abc123_20240101_120000.txt")

            assert result == "Test content"


class TestDocumentParseError:
    """Tests for DocumentParseError exception."""

    def test_error_attributes(self):
        """Test DocumentParseError attributes."""
        from src.services.assistant.document_parser import DocumentParseError

        original = ValueError("Original error")
        error = DocumentParseError(
            "Test error message",
            file_path="/uploads/test.pdf",
            original_error=original,
        )

        assert str(error) == "Test error message"
        assert error.file_path == "/uploads/test.pdf"
        assert error.original_error is original

    def test_error_without_original(self):
        """Test DocumentParseError without original error."""
        from src.services.assistant.document_parser import DocumentParseError

        error = DocumentParseError(
            "Test error message",
            file_path="/uploads/test.pdf",
        )

        assert error.original_error is None


class TestConvenienceFunction:
    """Tests for the parse_document convenience function."""

    @pytest.mark.asyncio
    async def test_parse_document_function(self, temp_storage):
        """Test the parse_document convenience function."""
        from src.services.assistant.document_parser import parse_document

        # Create a test file
        user_dir = temp_storage / "uploads" / "test_user"
        user_dir.mkdir(parents=True, exist_ok=True)
        test_file = user_dir / "test.txt"
        test_file.write_text("Convenience function test")

        with patch("src.services.assistant.document_parser.partition") as mock_partition:
            mock_element = MagicMock()
            mock_element.text = "Convenience function test"
            mock_partition.return_value = [mock_element]

            result = await parse_document(
                "/uploads/test_user/test.txt",
                storage_base_path=temp_storage,
            )

            assert result == "Convenience function test"


class TestSupportedTypes:
    """Tests for supported file types constant."""

    def test_all_supported_types(self, parser):
        """Test that all expected types are in SUPPORTED_TYPES."""
        expected_types = [".pdf", ".docx", ".doc", ".txt", ".md", ".csv", ".xlsx", ".html", ".htm"]

        for ext in expected_types:
            assert ext in parser.SUPPORTED_TYPES, f"Expected {ext} to be in SUPPORTED_TYPES"

    def test_supported_types_have_mime(self, parser):
        """Test that all supported types have MIME type mappings."""
        for ext, mime in parser.SUPPORTED_TYPES.items():
            assert mime is not None, f"Expected MIME type for {ext}"
            assert "/" in mime, f"Expected valid MIME type format for {ext}"
