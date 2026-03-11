class IslamicContentServiceError(RuntimeError):
    """Base runtime error for the standalone Islamic content service."""


class UpstreamAPIError(IslamicContentServiceError):
    """Raised when an upstream source fails."""


class NotReadyError(IslamicContentServiceError):
    """Raised when requested data is not ready in PostgreSQL yet."""
