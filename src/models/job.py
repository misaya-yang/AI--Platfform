from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
import uuid

class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class JobType(str, Enum):
    IMPORT = "import"      # File import
    INDEX = "index"        # Indexing/embedding
    REINDEX = "reindex"    # Re-indexing
    CLEANUP = "cleanup"    # Removing deleted docs

@dataclass
class JobStage:
    """A distinct stage in a job (e.g., 'Parsing', 'Chunking')"""
    name: str
    status: JobStatus = JobStatus.PENDING
    progress: float = 0.0  # 0.0 to 1.0 (stage local progress)
    message: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    
    # Granular tracking
    total_items: int = 0
    processed_items: int = 0

@dataclass
class Job:
    """
    Represents an asynchronous background task.
    """
    job_id: str
    type: JobType
    dataset_id: str
    
    # Reference to the object being processed (e.g., document_id, batch_id)
    ref_id: str
    
    status: JobStatus = JobStatus.PENDING
    stages: List[JobStage] = field(default_factory=list)
    current_stage_index: int = 0
    
    # Overall progress (0-100) - computed or explicitly set
    total_progress: float = 0.0
    
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    created_by: str = ""

    @classmethod
    def create(cls, dataset_id: str, type: JobType, ref_id: str, stages: List[str], created_by: str = "") -> "Job":
        return cls(
            job_id=str(uuid.uuid4()),
            dataset_id=dataset_id,
            type=type,
            ref_id=ref_id,
            stages=[JobStage(name=s) for s in stages],
            created_by=created_by
        )

    def start_stage(self, stage_name: str):
        """Mark a stage as started"""
        for i, stage in enumerate(self.stages):
            if stage.name == stage_name:
                stage.status = JobStatus.RUNNING
                stage.started_at = datetime.utcnow()
                self.current_stage_index = i
                self.status = JobStatus.RUNNING
                self.updated_at = datetime.utcnow()
                return

    def update_stage_progress(self, stage_name: str, processed: int, total: int):
        """Update items processed in a stage"""
        for stage in self.stages:
            if stage.name == stage_name:
                stage.processed_items = processed
                stage.total_items = total
                if total > 0:
                    stage.progress = min(1.0, processed / total)
                self.updated_at = datetime.utcnow()
                return
                
    def complete_stage(self, stage_name: str):
        """Mark a stage as completed"""
        for stage in self.stages:
            if stage.name == stage_name:
                stage.status = JobStatus.COMPLETED
                stage.progress = 1.0
                stage.completed_at = datetime.utcnow()
                self.updated_at = datetime.utcnow()
                
                # Check if all completed
                if all(s.status == JobStatus.COMPLETED for s in self.stages):
                    self.status = JobStatus.COMPLETED
                    self.completed_at = datetime.utcnow()
                    self.total_progress = 100.0
                return

    def fail(self, error: str):
        """Mark job as failed"""
        self.status = JobStatus.FAILED
        self.error = error
        self.completed_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()
        
        # Mark current stage as failed too
        if 0 <= self.current_stage_index < len(self.stages):
            stage = self.stages[self.current_stage_index]
            stage.status = JobStatus.FAILED
            stage.message = error
