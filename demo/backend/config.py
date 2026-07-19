"""Runtime configuration for the demo backend.

Every knob is an environment variable with a sane default so the same app drives
LM Studio, Ollama, or an mlx server without edits. Validation happens here, at the
process boundary: a bad port or a missing repo root fails loudly at startup rather
than mid-request.
"""

from __future__ import annotations

import pathlib

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven settings for the ephemeral demo server."""

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    model_name: str = Field(default="Qwen3.6-27B-4bit", alias="AIVFS_MODEL")
    openai_base_url: str = Field(default="http://localhost:11434/v1", alias="OPENAI_BASE_URL")
    openai_api_key: str = Field(default="omlx", alias="OPENAI_API_KEY")
    api_style: str = Field(default="chat", alias="AIVFS_API_STYLE")  # "chat" | "responses"
    # "files" | "codemode" | "bash" | "python" | "all" (comma-ok); "all" == "codemode,bash".
    tool_sets: str = Field(default="all", alias="AIVFS_TOOLS")

    # History compaction. The local model's context window is not discoverable, so it is
    # configured; `compact_fraction` of it is the budget past which older messages are
    # summarized (see history.py).
    context_window_tokens: int = Field(default=32_768, gt=0, alias="AIVFS_CONTEXT_TOKENS")
    compact_fraction: float = Field(default=0.6, alias="AIVFS_COMPACT_FRACTION")
    host: str = Field(default="127.0.0.1", alias="AIVFS_HOST")
    port: int = Field(default=7171, alias="AIVFS_PORT")
    repo_root: pathlib.Path | None = Field(default=None, alias="AIVFS_REPO_ROOT")

    # Ephemeral MLflow tracing. Best-effort: if the server can't start, chat still
    # runs — tracing is simply skipped.
    mlflow_enabled: bool = Field(default=True, alias="AIVFS_MLFLOW")
    mlflow_port: int = Field(default=5555, alias="AIVFS_MLFLOW_PORT")
    mlflow_experiment: str = Field(default="ai-vfs-demo", alias="AIVFS_MLFLOW_EXPERIMENT")

    @field_validator("api_style")
    @classmethod
    def _valid_style(cls, v: str) -> str:
        if v not in ("chat", "responses"):
            raise ValueError(f"AIVFS_API_STYLE must be 'chat' or 'responses', got {v!r}")
        return v

    @field_validator("compact_fraction")
    @classmethod
    def _valid_fraction(cls, v: float) -> float:
        if not 0.0 < v <= 1.0:
            raise ValueError(f"AIVFS_COMPACT_FRACTION must be in (0, 1], got {v}")
        return v

    @property
    def enabled_sets(self) -> set[str]:
        """Normalize AIVFS_TOOLS to a set of tool-surface flags.

        Flags: `files` (native file tools), `codemode` (`run_code`), `bash` (`run_bash`),
        `python` (deprecated `run_python`). `all` expands to `{codemode, bash}` and may be
        combined (e.g. `all,python`). `files` + `codemode` together is allowed but the file
        tools are exposed only through `run_code` (see `build_agent`).
        """
        valid = {"files", "codemode", "bash", "python"}
        sets = {s.strip() for s in self.tool_sets.split(",") if s.strip()}
        base = {"codemode", "bash"} if "all" in sets else set()
        rest = sets - {"all"}
        unknown = rest - valid
        if unknown:
            raise ValueError(f"AIVFS_TOOLS entries must be one of {sorted(valid)} or all, got {sorted(unknown)}")
        return base | rest

    def resolve_repo_root(self) -> pathlib.Path:
        """Locate the repo root that holds `.specs/` — explicit override, else search upward."""
        if self.repo_root is not None:
            root = self.repo_root
        else:
            root = next(
                (p for p in [pathlib.Path.cwd(), *pathlib.Path.cwd().parents] if (p / ".specs").is_dir()),
                None,
            )
        if root is None or not (root / ".specs").is_dir():
            raise RuntimeError("Could not locate .specs/; set AIVFS_REPO_ROOT to the repo root.")
        return root
