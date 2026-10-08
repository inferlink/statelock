import asyncio
import base64
import hashlib
from pathlib import Path
from typing import Any

import pytest
from helpers import context_for, run

from statelock.core.actions import ActionKind, CdpAction
from statelock.core.enums import Decision
from statelock.policy import PolicyBundle, PolicyEvaluator
from statelock.proxy.downloads import DownloadError, SessionDownloads


class Harness:
    """Fake browser sender plus a DownloadPolicy that records what it was asked."""

    def __init__(self, tmp_path: Path, *, allow_begin: bool = True, allow_complete: bool = True, max_file: int = 100):
        self.sent: list[tuple[str, dict[str, Any] | None]] = []
        self.begins: list[dict[str, Any]] = []
        self.completes: list[dict[str, Any]] = []
        self.limits: list[str] = []
        self.allow_begin, self.allow_complete = allow_begin, allow_complete
        self.fail_send = False
        self.downloads = SessionDownloads(self._send, self, tmp_path, max_file, 150)

    async def download_begin(self, params, frame_id):
        self.begins.append({**params, "frame_id": frame_id})
        return self.allow_begin, "https://portal.test/docs"

    async def download_complete(self, params, begin_url):
        self.completes.append({**params, "begin_url": begin_url})
        await asyncio.sleep(0.01)  # let concurrent completions interleave
        return self.allow_complete

    async def download_limit(self, reason, params):
        self.limits.append(f"{reason} ({params['phase']})")

    async def _send(self, method, params=None):
        if self.fail_send:
            raise RuntimeError("browser refused")
        self.sent.append((method, params))
        return {}

    def event(self, method: str, **params: Any) -> None:
        self.downloads.on_event({"method": method, "params": params})

    async def settle(self) -> None:
        for _ in range(5):
            await asyncio.sleep(0.01)

    async def download(self, guid: str, data: bytes, name: str = "a.pdf") -> None:
        self.event("Browser.downloadWillBegin", guid=guid, url=f"https://portal.test/{name}", suggestedFilename=name)
        await self.settle()
        (self.downloads.root / guid).write_bytes(data)
        self.event(
            "Browser.downloadProgress", guid=guid, state="inProgress", receivedBytes=len(data), totalBytes=len(data)
        )
        self.event("Browser.downloadProgress", guid=guid, state="completed", receivedBytes=len(data))
        await self.settle()

    async def listing(self) -> dict[str, dict[str, Any]]:
        result = await self.downloads.handle("Statelock.downloads", {})
        return {d["guid"]: d for d in result["downloads"]}


def test_configure_sets_statelock_folder_and_honours_deny(tmp_path: Path) -> None:
    async def scenario():
        h = Harness(tmp_path)
        await h.downloads.context_ready(None)
        await h.downloads.apply_agent_request(
            "Browser.setDownloadBehavior", {"behavior": "allowAndName", "downloadPath": "/agent/tmp"}
        )
        await h.downloads.apply_agent_request(
            "Browser.setDownloadBehavior", {"behavior": "deny", "browserContextId": "C2"}
        )
        await h.downloads.context_ready("C3")
        await h.downloads.context_ready("C3")
        await h.downloads.close()
        return h.sent

    sent = run(scenario())
    assert [p["behavior"] for _, p in sent] == ["allowAndName", "allowAndName", "deny", "allowAndName"]
    assert all(p.get("downloadPath", "").startswith(str(tmp_path)) for _, p in sent if p["behavior"] != "deny")
    assert "/agent/tmp" not in str(sent)
    assert all(p["eventsEnabled"] for _, p in sent)
    assert sent[3][1]["browserContextId"] == "C3"


def test_allowed_download_is_hashed_and_readable(tmp_path: Path) -> None:
    async def scenario():
        h = Harness(tmp_path)
        await h.download("g1", b"0123456789")
        listing = await h.listing()
        read = await h.downloads.handle("Statelock.downloadRead", {"guid": "g1", "offset": 2, "length": 4})
        await h.downloads.close()
        return h, listing, read

    h, listing, read = run(scenario())
    assert listing["g1"]["state"] == "completed"
    assert listing["g1"]["sha256"] == hashlib.sha256(b"0123456789").hexdigest()
    assert base64.b64decode(read["data"]) == b"2345"
    assert h.begins[0]["phase"] == "begin"
    assert h.completes[0]["phase"] == "complete"
    assert h.completes[0]["size"] == 10
    assert h.completes[0]["begin_url"] == "https://portal.test/docs"
    assert not h.downloads.folder.exists  # removed with the session


@pytest.mark.parametrize("blocked_at", ["begin", "complete"])
def test_blocked_download_is_cancelled_and_never_readable(tmp_path: Path, blocked_at: str) -> None:
    allow_begin, allow_complete = blocked_at != "begin", blocked_at != "complete"

    async def scenario():
        h = Harness(tmp_path, allow_begin=allow_begin, allow_complete=allow_complete)
        await h.download("g1", b"data")
        listing = await h.listing()
        with pytest.raises(DownloadError, match="not available"):
            await h.downloads.handle("Statelock.downloadRead", {"guid": "g1"})
        exists = (h.downloads.root / "g1").exists()
        await h.downloads.close()
        return h, listing, exists

    h, listing, exists = run(scenario())
    assert listing["g1"]["state"] == "blocked"
    assert not exists
    if not allow_begin:
        assert ("Browser.cancelDownload", {"guid": "g1"}) in h.sent
        assert h.completes == []


def test_system_limits_cancel_the_download(tmp_path: Path) -> None:
    async def scenario():
        h = Harness(tmp_path, max_file=5)
        await h.download("big", b"0123456789")
        await h.download("ok", b"0123")
        return h, await h.listing()

    h, listing = run(scenario())
    assert listing["big"]["state"] == "blocked"
    assert "download limit of 5" in listing["big"]["reason"]
    assert h.limits and "download limit" in h.limits[0]
    assert listing["ok"]["state"] == "completed"


def test_download_commands_validate_input(tmp_path: Path) -> None:
    async def scenario():
        h = Harness(tmp_path)
        await h.download("g1", b"abc")
        for params in ({"guid": "nope"}, {"guid": "g1", "offset": -1}, {"guid": "g1", "length": 0}):
            with pytest.raises(DownloadError):
                await h.downloads.handle("Statelock.downloadRead", params)
        with pytest.raises(DownloadError, match="unknown Statelock command"):
            await h.downloads.handle("Statelock.downloadDelete", {})
        await h.downloads.close()

    run(scenario())


# restrict_downloads ---------------------------------------------------------------------------


def _evaluator() -> PolicyEvaluator:
    rule = {
        "restrict_downloads": {
            "extensions": ["pdf"],
            "max_bytes": 10,
            "max_downloads": 2,
            "name_pattern": "^INV",
            "url_pattern": "^https://portal\\.test/",
        }
    }
    return PolicyEvaluator(PolicyBundle.model_validate({"policies": [{"agent_id": "agent", "pre_conditions": [rule]}]}))


def _download_context(**params: Any):
    base = {"phase": "begin", "suggested_filename": "INV-1.pdf", "url": "https://portal.test/f", "download_count": 1}
    action = CdpAction(None, "Statelock.download", ActionKind.DOWNLOAD, {**base, **params})
    return context_for(action)


@pytest.mark.parametrize(
    ("params", "decision"),
    [
        ({}, Decision.ALLOW),
        ({"suggested_filename": "INV-1.exe"}, Decision.BLOCK),
        ({"suggested_filename": "report.pdf"}, Decision.BLOCK),
        ({"url": "https://evil.test/f"}, Decision.BLOCK),
        ({"download_count": 3}, Decision.BLOCK),
        ({"download_count": None}, Decision.BLOCK),  # unknown count: max_downloads fails closed
        ({"download_count": "1"}, Decision.BLOCK),
        ({"download_count": True}, Decision.BLOCK),
        ({"phase": "complete", "size": 10}, Decision.ALLOW),
        ({"phase": "complete", "size": 11}, Decision.BLOCK),
        ({"phase": "begin", "size": 11}, Decision.ALLOW),  # size is checked on completion
    ],
)
def test_restrict_downloads(params: dict, decision: Decision) -> None:
    assert run(_evaluator().evaluate(_download_context(**params))).decision == decision


def test_download_completion_uses_the_begin_page_policies() -> None:
    ev = PolicyEvaluator(
        PolicyBundle.model_validate(
            {
                "policies": [
                    {
                        "agent_id": "agent",
                        "target_url_contains": "/docs",
                        "pre_conditions": [
                            {"require_page_text": {"values": ["never there"]}},
                            {"restrict_downloads": {"max_bytes": 10}},
                        ],
                    }
                ]
            }
        )
    )
    small = _download_context(phase="complete", size=5)
    big = _download_context(phase="complete", size=50)
    # require_page_text is not re-run on completion; only restrict_downloads is.
    assert run(ev.evaluate_download_completion(small, "https://portal.test/docs")).decision == Decision.ALLOW
    assert run(ev.evaluate_download_completion(big, "https://portal.test/docs")).decision == Decision.BLOCK
    assert run(ev.evaluate_download_completion(big, "https://portal.test/other")).decision == Decision.ALLOW


def test_concurrent_completions_cannot_overrun_the_session_budget(tmp_path: Path) -> None:
    async def scenario():
        h = Harness(tmp_path)  # 100 per file, 150 per session
        for guid in ("a", "b"):
            h.event(
                "Browser.downloadWillBegin",
                guid=guid,
                url=f"https://portal.test/{guid}",
                suggestedFilename=f"{guid}.pdf",
            )
        await h.settle()
        for guid in ("a", "b"):
            (h.downloads.root / guid).write_bytes(b"x" * 90)
            h.event("Browser.downloadProgress", guid=guid, state="completed", receivedBytes=90)
        for _ in range(100):  # until both completions are decided
            listing = await h.listing()
            if all(item["state"] in {"blocked", "completed"} for item in listing.values()):
                break
            await h.settle()
        return h, listing

    h, listing = run(scenario())
    states = sorted(item["state"] for item in listing.values())
    assert states == ["blocked", "completed"]
    assert h.downloads.budget.used == 90
    assert h.limits  # the second one was a limit violation


def test_context_setup_failure_propagates(tmp_path: Path) -> None:
    async def scenario():
        h = Harness(tmp_path)
        h.fail_send = True
        with pytest.raises(RuntimeError, match="browser refused"):
            await h.downloads.context_ready("C9")
        await h.downloads.close()

    run(scenario())


def test_page_level_deny_applies_to_every_context(tmp_path: Path) -> None:
    async def scenario():
        h = Harness(tmp_path)
        await h.downloads.context_ready(None)
        await h.downloads.context_ready("C1")
        h.sent.clear()
        await h.downloads.apply_agent_request("Page.setDownloadBehavior", {"behavior": "deny"})
        await h.downloads.close()
        return h.sent

    sent = run(scenario())
    assert [(p["behavior"], p.get("browserContextId")) for _, p in sent] == [("deny", None), ("deny", "C1")]


def test_unreadable_download_is_a_command_error(tmp_path: Path) -> None:
    async def scenario():
        h = Harness(tmp_path)
        await h.download("g1", b"abc")
        (h.downloads.root / "g1").unlink()
        with pytest.raises(DownloadError, match="could not be read"):
            await h.downloads.handle("Statelock.downloadRead", {"guid": "g1"})
        await h.downloads.close()

    run(scenario())


def test_default_context_id_maps_to_the_default_configuration(tmp_path: Path) -> None:
    async def scenario():
        h = Harness(tmp_path)

        async def send(method, params=None):
            h.sent.append((method, params))
            if method == "Target.getTargets":
                return {"targetInfos": [{"targetId": "T1", "browserContextId": "DEFAULT"}]}
            return {}

        h.downloads._send = send
        await h.downloads.start()
        await h.downloads.context_ready("DEFAULT")
        await h.downloads.apply_agent_request(
            "Browser.setDownloadBehavior", {"behavior": "allow", "browserContextId": "DEFAULT"}
        )
        await h.downloads.close()
        return h.sent

    sent = run(scenario())
    behaviors = [params for method, params in sent if method == "Browser.setDownloadBehavior"]
    assert len(behaviors) == 2  # start, and the agent's request; none for the default id itself
    assert all("browserContextId" not in params for params in behaviors)
