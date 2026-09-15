from .local import LocalSandbox  # noqa: F401
from .judge import (  # noqa: F401
    COMPARISON_MODES,
    DEFAULT_COMPARISON,
    Sandbox,
    compare_output,
    judge_program,
)
from .docker_sandbox import DockerSandbox, docker_available  # noqa: F401


def make_sandbox(backend: str, limits):
    """Factory: auto-detect Docker, fall back to local subprocess sandbox."""
    from ..config import SandboxLimits

    limits = limits or SandboxLimits()
    if backend == "docker":
        return DockerSandbox(limits)
    if backend == "local":
        return LocalSandbox(limits)
    # auto
    if docker_available():
        try:
            return DockerSandbox(limits)
        except Exception:
            pass
    return LocalSandbox(limits)
