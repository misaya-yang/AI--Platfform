"""Sandbox execution layer.

Two backends:

* :class:`LocalSubprocessSandbox` — for dev/unit-test: runs in a chroot-like
  temp dir with no inherited env vars and a hard timeout.
* :class:`DockerSandbox` — production: delegates to a prebuilt image
  (see ``docker/sandbox.Dockerfile``).

Both implement :class:`SandboxClient`.
"""

from .client import (
    SandboxClient,
    SandboxResult,
    SandboxError,
    SandboxTimeout,
)
from .local import LocalSubprocessSandbox
from .docker_backend import DockerSandbox

__all__ = [
    "SandboxClient",
    "SandboxResult",
    "SandboxError",
    "SandboxTimeout",
    "LocalSubprocessSandbox",
    "DockerSandbox",
]
