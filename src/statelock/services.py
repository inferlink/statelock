# SPDX-License-Identifier: Apache-2.0
"""Process-wide services, built once at startup and shared by all sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from statelock.audit.sink import ArtifactSink, LocalJsonSink
from statelock.auth import Authenticator
from statelock.credentials import SecretStore
from statelock.events import Events
from statelock.policy.evaluator import PolicyEvaluator
from statelock.policy.extensions import load_rule_modules
from statelock.policy.perception import LiteLLMPerceptionEvaluator, PerceptionEvaluator
from statelock.proxy.browser import ChromiumCdpLauncher
from statelock.registry import ViolationRegistry
from statelock.review.queue import ReviewQueue
from statelock.saved_sessions import AesGcmCipher, SavedSessionStore, check_key_location
from statelock.settings import Settings
from statelock.tokens import SessionTokens

if TYPE_CHECKING:
    from fastapi import FastAPI


@dataclass
class Services:
    settings: Settings
    evaluator: PolicyEvaluator
    sink: ArtifactSink
    launcher: ChromiumCdpLauncher
    registry: ViolationRegistry
    # Required: an embedder must choose (Authenticator("none") is explicit, never a default).
    authenticator: Authenticator
    saved_sessions: SavedSessionStore
    # Secrets agents type as {{secret:name}} (empty without STATELOCK_SECRETS_FILE).
    secrets: SecretStore = field(default_factory=SecretStore)
    events: Events = field(default_factory=Events)
    # Built from settings and events: paused actions shared by sessions and the review API.
    reviews: ReviewQueue = field(init=False)
    # Session URLs issued and not yet used (statelock.sessions).
    tokens: SessionTokens = field(init=False)
    # Session ids of the sessions running now (a session id is used once).
    active_sessions: set[str] = field(init=False, default_factory=set)

    def __post_init__(self) -> None:
        self.reviews = ReviewQueue(self.events, self.settings.review_history_size)
        self.tokens = SessionTokens()

    @classmethod
    def from_settings(cls, settings: Settings) -> Services:
        """Build default services. A missing policy or keys file fails here, at startup."""
        load_rule_modules(settings.rule_modules)  # custom rules, before the policy files name them
        return cls(
            authenticator=Authenticator.from_settings(settings.auth_mode, settings.auth_keys_file),
            settings=settings,
            evaluator=PolicyEvaluator.from_files(settings.policy_files, perception_evaluator(settings)),
            sink=LocalJsonSink(settings.artifact_dir, write_latest=settings.debug),
            launcher=ChromiumCdpLauncher(host=settings.chromium_host, sandbox=settings.chromium_sandbox),
            registry=ViolationRegistry(settings.violation_registry_size),
            saved_sessions=saved_session_store(settings),
            secrets=SecretStore.load(settings.secrets_file) if settings.secrets_file else SecretStore(),
        )


def perception_evaluator(settings: Settings) -> PerceptionEvaluator | None:
    """The VLM behind visual_assert; None (fail closed) without STATELOCK_PERCEPTION_MODEL."""
    if not settings.perception_model:
        return None
    try:
        return LiteLLMPerceptionEvaluator(
            settings.perception_model,
            api_base=settings.perception_api_base,
            api_key=settings.perception_api_key.get_secret_value() if settings.perception_api_key else None,
            timeout=settings.perception_timeout,
            retries=settings.perception_retries,
            local_only=settings.perception_local_only,
        )
    except ImportError as error:
        raise RuntimeError('STATELOCK_PERCEPTION_MODEL needs litellm: pip install "statelock-ai[vlm]"') from error


def saved_session_store(settings: Settings) -> SavedSessionStore:
    """The local saved-session store; off (no cipher) without STATELOCK_SAVED_SESSIONS_KEY_FILE."""
    root = settings.saved_sessions_dir or settings.artifact_dir / "saved-sessions"
    key_file = settings.saved_sessions_key_file
    if key_file is None:
        return SavedSessionStore(root)
    check_key_location(key_file, root)
    return SavedSessionStore(root, AesGcmCipher.from_key_file(key_file))


def get_services(app: FastAPI) -> Services:
    """The Services of a Statelock app (for routes and plugins)."""
    services = getattr(app.state, "services", None)
    if not isinstance(services, Services):
        raise TypeError("not a Statelock app: create it with statelock.app.create_app")
    return services
