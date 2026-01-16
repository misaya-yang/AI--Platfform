"""
Artifact schemas for assistant API.

Artifacts represent outputs from code execution, document generation,
image generation, or other tool invocations.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class ArtifactInfo(BaseModel):
    """Artifact metadata for API responses."""

    artifact_id: str
    session_id: str
    type: str  # image, document, chart, code, file
    format: str  # png, pdf, docx, md, csv, json, etc.
    title: str
    filename: str
    size_bytes: int
    mime_type: Optional[str] = None
    source: str = "ai"  # ai | user | code_execution
    message_id: Optional[str] = None
    download_url: Optional[str] = None  # Presigned URL for download
    metadata: Optional[Dict[str, Any]] = None
    created_at: Optional[str] = None


class ArtifactListResponse(BaseModel):
    """Response with list of artifacts."""

    artifacts: List[ArtifactInfo]
    total: int


class ArtifactCreateRequest(BaseModel):
    """Request to create an artifact."""

    session_id: str
    type: str
    format: str
    title: str
    filename: str
    content_base64: str  # Base64 encoded content
    source: str = "ai"
    message_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
