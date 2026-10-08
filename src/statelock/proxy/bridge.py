# SPDX-License-Identifier: Apache-2.0
"""One governed agent session: admission, governed Chromium, and both message pumps."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import websockets
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import ClientConnection

from statelock import events as event_names
from statelock.auth import AUTHORIZATION_HEADER, AuthError, Identity
from statelock.core.actions import FILE_UPLOAD_METHOD, CdpAction, classify_cdp_action, parse_cdp_message
from statelock.core.enums import SystemRule
from statelock.core.jsonutil import as_dict, as_str
from statelock.credentials import SecretScrubber
from statelock.policy.fields import SessionMemory
from statelock.proxy.attribution import tag_agent_code
from statelock.proxy.commands import AGENT_NAVIGATION_METHODS, refused_command
from statelock.proxy.connection import CdpConnection, CdpError
from statelock.proxy.cookies import COOKIE_METHODS, CookieReads
from statelock.proxy.downloads import AGENT_BEHAVIOR_METHODS, SessionDownloads
from statelock.proxy.downloads import COMMANDS as DOWNLOAD_COMMANDS
from statelock.proxy.files import LocalCommandError
from statelock.proxy.governor import ActionGovernor
from statelock.proxy.guard import GuardTimings, PageGuard
from statelock.proxy.inspector import Inspector
from statelock.proxy.network_filter import filter_network_message
from statelock.proxy.pages import PageSessions
from statelock.proxy.reporter import ViolationReporter
from statelock.proxy.requests import GovernedRequests
from statelock.proxy.targets import TargetRegistry
from statelock.proxy.uploads import COMMANDS as UPLOAD_COMMANDS
from statelock.proxy.uploads import SessionUploads, UploadError
from statelock.proxy.worlds import StatelockContexts
from statelock.saved_sessions import SavedSessionError, SavedSessionKey
from statelock.services import Services
from statelock.wire import (
    AGENT_ID_HEADER,
    AGENT_ID_QUERY_PARAM,
    CLOSE_CODE_INVALID_SESSION_ID,
    CLOSE_CODE_UNREGISTERED_AGENT,
    REQUEST_COMMAND,
    SESSION_COMMAND,
    SESSION_ID_HEADER,
    STATELOCK_METHOD_PREFIX,
    cdp_response,
    statelock_error,
    truncate_utf8,
)

logger = logging.getLogger(__name__)

# The answer to a message that is not a JSON object (Chromium answers id 0 too). It is not forwarded.
NOT_JSON_RESPONSE = json.dumps(
    {"id": 0, "error": {"code": -32700, "message": "Statelock: a CDP message must be a JSON object"}}
)


@dataclass
class Admission:
    session_id: str
    agent_id: str
    tenant_id: str | None = None
    saved_session_name: str | None = None
    save_session: bool = False


def resolve_agent_id(client_ws: WebSocket) -> str | None:
    agent_id = client_ws.headers.get(AGENT_ID_HEADER) or client_ws.query_params.get(AGENT_ID_QUERY_PARAM)
    agent_id = (agent_id or "").strip()
    return agent_id or None


def resolve_session_id(client_ws: WebSocket) -> tuple[str | None, str | None]:
    """Return (session_id, error). A client may supply a UUID so it can look up violations."""
    requested = (client_ws.headers.get(SESSION_ID_HEADER) or "").strip()
    if not requested:
        return str(uuid.uuid4()), None
    try:
        return str(uuid.UUID(requested)), None
    except ValueError:
        return None, f"Statelock session id is not a UUID: {requested}"


@dataclass(frozen=True)
class _Identified:
    identity: Identity
    session_id: str | None
    session_id_error: str | None = None
    saved_session_name: str | None = None
    save_session: bool = False


def _identify(client_ws: WebSocket, services: Services, token: str | None) -> _Identified:
    """The agent and requested session ID: from a session-URL token, or from the headers."""
    if token is not None:
        pending = services.tokens.consume(token)
        if pending is None:
            raise AuthError("unknown, used or expired session URL")
        return _Identified(
            pending.identity,
            pending.session_id,
            saved_session_name=pending.saved_session_name,
            save_session=pending.save_session,
        )
    identity = services.authenticator.authenticate(
        resolve_agent_id(client_ws), client_ws.headers.get(AUTHORIZATION_HEADER)
    )
    session_id, error = resolve_session_id(client_ws)
    return _Identified(identity, session_id, error)


async def admit(client_ws: WebSocket, services: Services, token: str | None = None) -> Admission | None:
    """Accept the WebSocket, authenticate the agent, and validate the session ID. Closes on failure.

    ``token`` is a session URL's token (statelock.sessions); otherwise the headers identify the agent.
    """
    await client_ws.accept()
    try:
        identified = _identify(client_ws, services, token)
    except AuthError as auth_error:
        # The reason stays generic: the client learns nothing about which check failed.
        logger.warning("Rejected Statelock session: authentication failed: %s", auth_error)
        await client_ws.close(code=CLOSE_CODE_UNREGISTERED_AGENT, reason="Statelock authentication failed")
        return None

    session_id, error = identified.session_id, identified.session_id_error
    if session_id is not None and (
        session_id in services.active_sessions
        or services.sink.session_exists(session_id)
        or services.registry.get(session_id) is not None
    ):
        error = f"Statelock session id already used: {session_id}"
    if error is not None or session_id is None:
        logger.warning("Rejected Statelock session: %s", error)
        await client_ws.close(code=CLOSE_CODE_INVALID_SESSION_ID, reason=truncate_utf8(error or "invalid session id"))
        return None

    agent_id = identified.identity.agent_id
    if not services.evaluator.is_registered(agent_id):
        logger.warning("Rejected Statelock session %s: agent_id=%r is not registered", session_id, agent_id)
        await client_ws.close(
            code=CLOSE_CODE_UNREGISTERED_AGENT,
            reason=truncate_utf8(f"Statelock agent not registered: {agent_id}"),
        )
        return None
    # Reserved before any await, so a second connection with this id is refused.
    services.active_sessions.add(session_id)
    return Admission(
        session_id=session_id,
        agent_id=agent_id,
        tenant_id=identified.identity.tenant,
        saved_session_name=identified.saved_session_name,
        save_session=identified.save_session,
    )


@dataclass
class _Session:
    """Per-session components shared by the two message pumps."""

    client_ws: WebSocket
    browser_ws: ClientConnection
    governor: ActionGovernor
    targets: TargetRegistry
    reporter: ViolationReporter
    guard: PageGuard | None
    uploads: SessionUploads
    commands: LocalCommands
    cookies: CookieReads
    requests: GovernedRequests
    contexts: StatelockContexts
    scrubber: SecretScrubber

    async def respond(self, request: dict[str, Any], body: dict[str, Any]) -> None:
        """Answer an agent command in-band (only commands with an id get an answer).
        Secrets the session injected never reach the agent."""
        if isinstance(request.get("id"), int) and not self.reporter.client_closed:
            text = json.dumps(cdp_response(request, body))
            await self.client_ws.send_text(self.scrubber.text(text) if self.scrubber else text)


class LocalCommands:
    """Commands the proxy answers itself; they are never forwarded to Chromium."""

    def __init__(self, uploads: SessionUploads, downloads: SessionDownloads, identity: dict[str, Any]) -> None:
        self.uploads = uploads
        self.downloads = downloads
        # Answer to Statelock.session: lets an SDK tell a Statelock browser from a plain one.
        self.identity = identity

    async def answer(self, payload: dict[str, Any], session: _Session) -> bool:
        """Answer the command if it is local. Returns True if it was handled."""
        method = str(payload.get("method") or "")
        params = as_dict(payload.get("params"))
        if method == REQUEST_COMMAND:
            return False  # governed and recorded like an action (_route_command)
        if not method.startswith(STATELOCK_METHOD_PREFIX) and method not in AGENT_BEHAVIOR_METHODS:
            return False
        try:
            result = await self._run(method, params)
        except (LocalCommandError, CdpError) as error:
            await session.respond(payload, statelock_error(str(error)))
        else:
            await session.respond(payload, {"result": result})
        return True

    async def _run(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == SESSION_COMMAND:
            return dict(self.identity)
        if method in UPLOAD_COMMANDS:
            return await asyncio.to_thread(self.uploads.handle, method, params)
        if method in DOWNLOAD_COMMANDS:
            return await self.downloads.handle(method, params)
        if method in AGENT_BEHAVIOR_METHODS:
            # Statelock keeps its own download folder and events; deny is honoured.
            await self.downloads.apply_agent_request(method, params)
            return {}
        raise LocalCommandError(f"unknown Statelock command: {method}")


class CdpBridge:
    """Runs one session. Components are created per session; services are shared."""

    def __init__(self, services: Services) -> None:
        self.services = services

    async def handle(self, client_ws: WebSocket, token: str | None = None) -> None:
        admission = await admit(client_ws, self.services, token)
        if admission is None:
            return
        logger.info(
            "Accepted Statelock session %s agent_id=%s tenant=%s",
            admission.session_id,
            admission.agent_id,
            admission.tenant_id,
        )
        settings = self.services.settings
        reporter = ViolationReporter(
            client_ws, self.services.registry, self.services.events, close_delay=settings.violation_close_delay
        )
        failed = False
        try:
            # Callbacks run in reverse order: event producers (guard, downloads, targets)
            # stop before the governor they call, and Chromium is always closed last.
            async with contextlib.AsyncExitStack() as stack:
                await self._run_session(stack, client_ws, admission, reporter)
        except Exception:  # logged once here; the agent sees close code 1011
            failed = True
            logger.exception("Statelock session %s failed", admission.session_id)
            await reporter.close(1011, "Statelock session failed (see the proxy log)")
        finally:
            self.services.active_sessions.discard(admission.session_id)
            logger.info("Closed Statelock session %s", admission.session_id)
            await self.services.events.emit(
                event_names.SESSION_CLOSED,
                {
                    "session_id": admission.session_id,
                    "agent_id": admission.agent_id,
                    "tenant_id": admission.tenant_id,
                    "violation": reporter.terminating,
                    "error": failed,
                },
            )

    async def _run_session(
        self,
        stack: contextlib.AsyncExitStack,
        client_ws: WebSocket,
        admission: Admission,
        reporter: ViolationReporter,
    ) -> None:
        settings = self.services.settings
        browser = await self.services.launcher.launch()
        stack.push_async_callback(browser.close)
        connection = CdpConnection(browser.cdp_ws_url, command_timeout=settings.guard_command_timeout)
        await connection.open()
        stack.push_async_callback(connection.close)
        # The agent's own connection to the browser; nothing flows on it until the pumps start.
        browser_ws = await stack.enter_async_context(websockets.connect(browser.cdp_ws_url, max_size=None))

        pages = PageSessions(connection)
        saved_session_key = self._saved_session_key(admission)
        if saved_session_key is not None:
            try:
                await self.services.saved_sessions.restore(connection, saved_session_key)
            except (SavedSessionError, CdpError):
                logger.exception(
                    "Could not restore saved browser session %s for session %s",
                    saved_session_key.name,
                    admission.session_id,
                )
                raise
            if admission.save_session:
                stack.push_async_exit(self._saver(connection, pages, saved_session_key, reporter))
        fields = self.services.evaluator.field_specs(admission.agent_id)
        inspector = Inspector(pages, timeout=settings.capture_timeout, fields=fields)
        targets = TargetRegistry(ready_timeout=settings.guard_ready_timeout)
        governor = ActionGovernor(
            session_id=admission.session_id,
            agent_id=admission.agent_id,
            tenant_id=admission.tenant_id,
            settings=settings,
            evaluator=self.services.evaluator,
            sink=self.services.sink,
            events=self.services.events,
            inspector=inspector,
            targets=targets,
            reporter=reporter,
            reviews=self.services.reviews,
            browser_ws=browser_ws,
            client_ws=client_ws,
            memory=SessionMemory(fields),
            secrets=self.services.secrets,
        )
        stack.push_async_callback(governor.close)
        uploads = SessionUploads(settings.upload_dir, settings.upload_max_file_bytes, settings.upload_max_session_bytes)
        stack.callback(uploads.close)
        downloads = SessionDownloads(
            send=connection.send,
            policy=governor,
            base_dir=settings.download_dir,
            max_file_bytes=settings.download_max_file_bytes,
            max_session_bytes=settings.download_max_session_bytes,
        )
        stack.push_async_callback(downloads.close)
        guard = self._build_guard(pages, governor, admission.agent_id)
        if guard is not None:
            stack.push_async_callback(guard.close)
        stack.push_async_callback(targets.close)

        # Wiring, before any message flows.
        connection.add_listener(governor.field_watcher.on_browser_event)
        connection.add_listener(downloads.on_event)
        targets.add_setup(governor.field_watcher.setup_target)
        targets.add_setup(lambda _target_id, info: downloads.context_ready(as_str(info.get("browserContextId"))))
        if guard is not None:
            targets.add_setup(guard.setup_target)
        contexts = StatelockContexts(pages)
        await downloads.start()

        commands = LocalCommands(
            uploads, downloads, {"session_id": admission.session_id, "agent_id": admission.agent_id}
        )
        cookies = CookieReads(pages, targets, self.services.evaluator.cookie_access(admission.agent_id))
        requests = GovernedRequests(
            pages,
            targets,
            self.services.evaluator.request_access(admission.agent_id),
            agent_id=admission.agent_id,
            secrets=self.services.secrets,
            scrubber=governor.scrubber,
        )
        # Pushed last, so in-flight requests stop first, before the governor that records them.
        stack.push_async_callback(requests.close)
        await self._run_pumps(
            _Session(
                client_ws,
                browser_ws,
                governor,
                targets,
                reporter,
                guard,
                uploads,
                commands,
                cookies,
                requests,
                contexts,
                governor.scrubber,
            )
        )

    def _saved_session_key(self, admission: Admission) -> SavedSessionKey | None:
        if admission.saved_session_name is None:
            return None
        return SavedSessionKey(admission.tenant_id, admission.agent_id, admission.saved_session_name)

    def _saver(
        self, connection: CdpConnection, pages: PageSessions, key: SavedSessionKey, reporter: ViolationReporter
    ) -> Callable[..., Awaitable[bool]]:
        """Exit callback: save the browser state only when the session ended cleanly
        (no violation, no error), so a blocked action's state is never kept."""

        async def save(exc_type: type[BaseException] | None, *_: object) -> bool:
            if exc_type is not None or reporter.terminating:
                logger.info("Not saving browser session %s: the session did not end cleanly", key.name)
                return False
            try:
                await self.services.saved_sessions.save_from_browser(connection, pages, key)
            except (SavedSessionError, CdpError, OSError):
                logger.exception("Could not save browser session %s", key.name)
            return False

        return save

    def _build_guard(self, pages: PageSessions, governor: ActionGovernor, agent_id: str) -> PageGuard | None:
        patterns = self.services.evaluator.guard_url_patterns(agent_id)
        if patterns is not None and not patterns:
            return None  # every policy for this agent allows synthetic events
        settings = self.services.settings
        return PageGuard(
            pages,
            patterns,
            governor.on_guard_violation,
            governor.on_request_record,
            timings=GuardTimings(
                attribution_timeout=settings.attribution_timeout,
                trusted_submit_window=settings.trusted_submit_window,
                agent_navigation_window=settings.agent_navigation_window,
            ),
            on_script_click=governor.replay_script_click if settings.replay_script_clicks else None,
            agent_script_active=governor.scripts.active,
        )

    async def _run_pumps(self, session: _Session) -> None:
        pumps = {
            asyncio.create_task(self._client_to_browser(session)),
            asyncio.create_task(self._browser_to_client(session)),
        }
        done, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()

    async def _client_to_browser(self, session: _Session) -> None:
        try:
            while True:
                raw_message = await session.client_ws.receive_text()
                if not await self._handle_client_message(session, raw_message):
                    return
        except WebSocketDisconnect:
            logger.info("Client disconnected from Statelock session %s", session.governor.session_id)
        except RuntimeError:
            if not session.reporter.client_closed:
                raise
            logger.info("Client websocket closed for Statelock session %s", session.governor.session_id)

    async def _handle_client_message(self, session: _Session, raw_message: str) -> bool:
        """Route one agent command. Returns False when the session ends."""
        payload = parse_cdp_message(raw_message)
        if session.reporter.terminating:
            await session.reporter.refuse(payload)
            return True
        if payload is None:
            # Never forwarded: what reaches Chromium is always the command Statelock evaluated.
            if not session.reporter.client_closed:
                await session.client_ws.send_text(NOT_JSON_RESPONSE)
            return True
        if await session.commands.answer(payload, session):
            return True
        return await self._route_command(session, payload)

    async def _route_command(self, session: _Session, payload: dict[str, Any]) -> bool:
        """Govern an input action or forward a command. Returns False when the session ends."""
        # A script click the agent's code just made is replayed first, so this command sees its effect.
        await session.governor.replays_done(self.services.settings.replay_wait)
        if not await self._preflight(session, payload):
            return False
        if payload.get("method") in COOKIE_METHODS:
            try:
                body, verdict = await session.cookies.answer(payload)
            except CdpError as error:
                body = statelock_error(str(error))
            else:
                await session.governor.record_command(payload, verdict)
            await session.respond(payload, body)
            return True
        if payload.get("method") == REQUEST_COMMAND:
            # Beside the agent's other commands: a request can take up to 30 s.
            session.requests.tasks.spawn(self._answer_request(session, payload))
            return True
        action = classify_cdp_action(payload)
        if action is not None and action.method == FILE_UPLOAD_METHOD:
            action = await self._with_uploaded_files(session, payload, action)
            if action is None:
                return False
        if action is not None:
            session.governor.scripts.input_dispatched(session.targets.target_for(action.session_id))
            return await session.governor.govern_input(action, payload)
        await self._prepare_command(session, payload)
        await session.browser_ws.send(json.dumps(payload))
        return True

    async def _answer_request(self, session: _Session, payload: dict[str, Any]) -> None:
        outcome = await session.requests.answer(payload)
        recorded = {**payload, "params": outcome.recorded}
        if outcome.ends_session:
            # A background task: it answers and closes, but never reads the agent's socket.
            await session.governor.refuse_command(recorded, verdict=outcome.verdict, from_client_pump=False)
            return
        await session.governor.record_command(recorded, outcome.verdict)
        await session.respond(payload, outcome.body)

    async def _prepare_command(self, session: _Session, payload: dict[str, Any]) -> None:
        """Bookkeeping for a forwarded (non-input) command; tags the agent's code in it."""
        target_id = session.targets.target_for(as_str(payload.get("sessionId")))
        if payload.get("method") in AGENT_NAVIGATION_METHODS:
            # Remember values on the page the agent is about to leave.
            await session.governor.field_watcher.remember_page(target_id, "before_navigation")
        if session.guard is not None:
            session.guard.note_command(payload, target_id)
        session.governor.scripts.started(payload, target_id)
        tag_agent_code(payload)

    async def _with_uploaded_files(
        self, session: _Session, payload: dict[str, Any], action: CdpAction
    ) -> CdpAction | None:
        """DOM.setFileInputFiles may only name files uploaded in this session.

        Returns the action with the files' evidence (name, size, SHA-256) added to
        its recorded params, or None after refusing the command.
        """
        try:
            files = session.uploads.resolve(action.params.get("files"))
        except UploadError as error:
            await session.governor.refuse_command(
                payload, rule=SystemRule.FILE_UPLOAD_PATH.value, reason=f"Blocked DOM.setFileInputFiles: {error}"
            )
            return None
        params = {**action.params, "statelock_uploads": [uploaded.as_dict() for uploaded in files]}
        return dataclasses.replace(action, params=params)

    async def _preflight(self, session: _Session, payload: dict[str, Any]) -> bool:
        """Wait for the tab's setup and refuse forbidden commands. Returns False if refused."""
        setup_error = await session.targets.await_ready(as_str(payload.get("sessionId")))
        if setup_error is not None:
            await session.governor.refuse_command(
                payload,
                rule=SystemRule.TARGET_SETUP.value,
                reason=(
                    "Blocked command because Statelock could not prepare the tab "
                    f"(page guard, download folder): {setup_error}"
                ),
            )
            return False
        refused = refused_command(payload)
        if refused is not None:
            rule, reason = refused
            await session.governor.refuse_command(payload, rule=rule, reason=reason)
            return False
        target_id = session.targets.target_for(as_str(payload.get("sessionId")))
        internal = session.contexts.refusal(payload, target_id)
        if internal is not None:
            await session.governor.refuse_command(payload, rule=SystemRule.STATELOCK_INTERNALS.value, reason=internal)
            return False
        return True

    async def _browser_to_client(self, session: _Session) -> None:
        try:
            while True:
                raw_message = await session.browser_ws.recv()
                if session.reporter.client_closed:
                    return
                session.targets.track(raw_message)
                session.contexts.observe(raw_message)
                session.governor.scripts.observe(raw_message)
                if session.governor.claim_held_response(raw_message):
                    continue
                scrubber = session.governor.scrubber
                if isinstance(raw_message, bytes):
                    raw_message = filter_network_message(raw_message.decode("utf-8", "replace")).encode("utf-8")
                    if scrubber:
                        raw_message = scrubber.text(raw_message.decode("utf-8", "replace")).encode("utf-8")
                    await session.client_ws.send_bytes(raw_message)
                else:
                    # An injected secret never reaches the agent (e.g. reading the field's value back).
                    raw_message = filter_network_message(raw_message)
                    await session.client_ws.send_text(scrubber.text(raw_message) if scrubber else raw_message)
        except WebSocketDisconnect:
            logger.info("Client disconnected from Statelock session %s", session.governor.session_id)
        except websockets.ConnectionClosed:
            logger.info("Browser CDP connection closed for session %s", session.governor.session_id)
        except RuntimeError:
            if not session.reporter.client_closed:
                raise
