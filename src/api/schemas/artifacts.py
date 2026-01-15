"""
Artifact schemas for assistant API.

Artifacts represent outputs from code execution, data analysis,
or other tool invocations (e.g., charts, tables, code files).
"""

from typing import List, Optional

from pydantic import BaseModel


class ArtifactInfo(BaseModel):
    """Artifact metadata."""

    artifact_id: str
    execution_id: str
    type: str  # code, chart, table, file
    format: str  # png, csv, json, etc.
    filename: str
    title: Optional[str] = None
    size_bytes: int
    created_at: Optional[str] = None


class ArtifactListResponse(BaseModel):
    """Response with list of artifacts."""

    artifacts: List[ArtifactInfo]
    total: int
