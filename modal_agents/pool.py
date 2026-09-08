"""Validated, user-editable sandbox configuration."""

from pathlib import Path, PurePosixPath
from runpy import run_path

import modal
from pydantic import BaseModel, ConfigDict, Field, field_validator

POOL_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,39}$"


class Pool(BaseModel):
    """One agent routed to an ephemeral Modal sandbox configuration."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid", frozen=True)

    name: str = Field(pattern=POOL_NAME_PATTERN)
    agent_id: str = Field(pattern=r"^agent_[A-Za-z0-9_-]+$")
    image: modal.Image
    cpu: float = Field(default=2, gt=0)
    memory: int = Field(default=4096, gt=0, strict=True)
    gpu: str | None = None
    timeout: int = Field(default=1800, ge=60, le=86400, strict=True)
    workspace: str = "/workspace"
    worker_secret_names: tuple[str, ...] = ()

    @field_validator("agent_id")
    @classmethod
    def real_agent_id(cls, value: str) -> str:
        if value == "agent_auto":
            raise ValueError("Supply an existing OpenAI agent ID; agent_auto is a placeholder")
        return value

    @field_validator("workspace")
    @classmethod
    def absolute_workspace(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not path.is_absolute() or ".." in path.parts or str(path) != value:
            raise ValueError("workspace must be a normalized absolute POSIX path")
        return value

    @field_validator("worker_secret_names")
    @classmethod
    def separate_worker_secrets(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() or value.startswith("openai-agents-") for value in values):
            raise ValueError(
                "Worker secrets must be nonempty and separate from integration secrets"
            )
        if len(set(values)) != len(values):
            raise ValueError("Worker secrets must be unique")
        return values

    @property
    def app_name(self) -> str:
        return f"openai-agents-{self.name}"

    @property
    def controller_secret(self) -> str:
        return f"{self.app_name}-controller"

    @property
    def executor_secret(self) -> str:
        return f"{self.app_name}-executor"

    @property
    def signing_secret(self) -> str:
        return f"{self.app_name}-signing"

    @property
    def required_secrets(self) -> tuple[str, ...]:
        return (
            self.controller_secret,
            self.executor_secret,
            self.signing_secret,
            *self.worker_secret_names,
        )


def load_pool(path: Path) -> Pool:
    """Load trusted local Python configuration, like `modal deploy` does."""
    value = run_path(str(path)).get("pool")
    if not isinstance(value, Pool):
        raise ValueError(f"{path}: pool must be a Pool instance; define pool = Pool(...)")
    if value.name != path.stem:
        raise ValueError(f"{path}: pool.name must match the filename")
    return value
