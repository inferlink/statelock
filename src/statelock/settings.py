# SPDX-License-Identifier: Apache-2.0
"""Runtime configuration. Every value can be set with a STATELOCK_* environment variable."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from statelock.policy.perception import DEFAULT_RETRIES as DEFAULT_PERCEPTION_RETRIES
from statelock.policy.perception import DEFAULT_TIMEOUT as DEFAULT_PERCEPTION_TIMEOUT


class Settings(BaseSettings):
    """``STATELOCK_*`` environment variables (see the README's configuration table)."""

    model_config = SettingsConfigDict(env_prefix="STATELOCK_", extra="ignore")

    policy_file: Path = Path("/app/policies/default.yaml")
    # More policy files, comma-separated, loaded with policy_file as one bundle (e.g. an
    # example's policy next to the default one).
    extra_policy_files: str = ""
    # Custom rule modules, comma-separated: module names or .py paths (statelock/policy/extensions.py).
    rule_modules: str = ""
    artifact_dir: Path = Path("/app/artifacts")
    # Saved browser sessions (statelock/saved_sessions.py). Off without a key file, which must
    # be outside saved_sessions_dir (default: <artifact_dir>/saved-sessions). Created if missing.
    saved_sessions_dir: Path | None = None
    saved_sessions_key_file: Path | None = None

    # Visual checks (rule visual_assert, statelock/policy/perception.py): a vision-language
    # model through litellm, e.g. "hosted_vllm/Qwen/Qwen2.5-VL-7B-Instruct" or
    # "ollama_chat/qwen2.5vl:7b". Unset: visual_assert fails closed. Needs statelock-ai[vlm].
    perception_model: str | None = None
    perception_api_base: str | None = None
    perception_api_key: SecretStr | None = None
    perception_timeout: float = Field(default=DEFAULT_PERCEPTION_TIMEOUT, gt=0)  # seconds per model call
    perception_retries: int = Field(default=DEFAULT_PERCEPTION_RETRIES, ge=0)  # after invalid output or an error
    # Refuse a model server that is not on this machine or a private network (air-gapped use).
    perception_local_only: bool = False

    # Credential injection (statelock/credentials.py): secrets the agent types as {{secret:name}}.
    secrets_file: Path | None = None

    # Agent authentication: "api_key" (default) needs auth_keys_file; "none" is for
    # local development only (agent IDs are then self-asserted). See statelock/auth.py.
    auth_mode: Literal["api_key", "none"] = "api_key"
    auth_keys_file: Path | None = None

    # Python logging level for Statelock's own setup. Set it to "" to leave logging to the host.
    log_level: str = "INFO"

    # Optional routes.
    debug: bool = False  # also write artifacts/latest.json (the latest action record)
    demo: bool = False  # /demo/finance, /demo/bank, /demo/erp

    # Plugins: "auto" loads every installed statelock.plugins entry point,
    # "none" loads none, otherwise a comma-separated list of entry point names.
    plugins: str = "auto"

    # Chromium.
    chromium_host: str = "127.0.0.1"
    chromium_sandbox: bool = False  # the sandbox needs a non-root user and a permissive seccomp profile

    # Timing (seconds).
    capture_timeout: float = Field(default=2.5, gt=0)
    post_capture_settle: float = Field(default=0.2, ge=0)
    held_response_timeout: float = Field(default=10.0, gt=0)
    violation_close_delay: float = Field(default=0.5, ge=0)
    guard_ready_timeout: float = Field(default=10.0, gt=0)
    guard_command_timeout: float = Field(default=5.0, gt=0)
    attribution_timeout: float = Field(default=1.0, gt=0)
    trusted_submit_window: float = Field(default=3.0, gt=0)
    agent_navigation_window: float = Field(default=10.0, gt=0)
    memory_settle: float = Field(default=0.5, ge=0)  # second capture after a page load

    # A plain element.click() from agent code (frameworks such as Stagehand click this
    # way) is replayed as a governed real click. Off: such clicks are violations.
    replay_script_clicks: bool = True
    agent_script_grace: float = Field(default=0.5, ge=0)
    replay_wait: float = Field(default=10.0, gt=0)

    violation_registry_size: int = Field(default=1000, gt=0)
    session_url_ttl: int = Field(default=300, gt=0)

    # Human review (rules with on_fail: review; see statelock/review).
    review_timeout: float = Field(default=300.0, gt=0)
    review_history_size: int = Field(default=1000, gt=0)

    # Governed uploads (Statelock.uploadFile*, then DOM.setFileInputFiles).
    upload_dir: Path | None = None  # base directory for per-session upload folders; default: system temp
    upload_max_file_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    upload_max_session_bytes: int = Field(default=500 * 1024 * 1024, gt=0)

    # Governed downloads (see statelock/proxy/downloads.py).
    download_dir: Path | None = None  # base directory for per-session download folders; default: system temp
    download_max_file_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    download_max_session_bytes: int = Field(default=500 * 1024 * 1024, gt=0)

    @property
    def policy_files(self) -> list[Path]:
        """policy_file, then extra_policy_files."""
        extra = [Path(part.strip()) for part in self.extra_policy_files.split(",") if part.strip()]
        return [self.policy_file, *extra]
