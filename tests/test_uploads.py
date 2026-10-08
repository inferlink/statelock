import base64
import hashlib
from pathlib import Path

import pytest

from statelock.proxy.uploads import SessionUploads, UploadError, safe_file_name
from statelock.wire import UPLOAD_BEGIN_COMMAND, UPLOAD_CHUNK_COMMAND, UPLOAD_END_COMMAND


def _uploads(tmp_path: Path, max_file: int = 1000, max_session: int = 1500) -> SessionUploads:
    return SessionUploads(tmp_path, max_file, max_session)


def _upload(store: SessionUploads, name: str, data: bytes, chunk: int = 3) -> dict:
    upload_id = store.handle(UPLOAD_BEGIN_COMMAND, {"name": name, "mimeType": "text/plain"})["uploadId"]
    for offset in range(0, len(data), chunk):
        store.handle(
            UPLOAD_CHUNK_COMMAND,
            {"uploadId": upload_id, "data": base64.b64encode(data[offset : offset + chunk]).decode()},
        )
    return store.handle(UPLOAD_END_COMMAND, {"uploadId": upload_id})


def test_chunked_upload_is_stored_hashed_and_resolvable(tmp_path: Path) -> None:
    store = _uploads(tmp_path)
    result = _upload(store, "report.pdf", b"0123456789")
    assert result["name"] == "report.pdf"
    assert result["size"] == 10
    assert result["sha256"] == hashlib.sha256(b"0123456789").hexdigest()
    assert Path(result["path"]).read_bytes() == b"0123456789"
    assert Path(result["path"]).name == "report.pdf"
    [resolved] = store.resolve([result["path"]])
    assert resolved.as_dict()["sha256"] == result["sha256"]
    assert store.resolve([]) == []


def test_only_uploaded_paths_resolve(tmp_path: Path) -> None:
    store = _uploads(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    with pytest.raises(UploadError, match="was not uploaded"):
        store.resolve([str(outside)])
    upload_id = store.handle(UPLOAD_BEGIN_COMMAND, {"name": "a.txt"})["uploadId"]
    pending_path = next(tmp_path.glob(f"statelock-uploads-*/{upload_id}/a.txt"))
    with pytest.raises(UploadError):
        store.resolve([str(pending_path)])  # not finished
    with pytest.raises(UploadError):
        store.resolve("not-a-list")


@pytest.mark.parametrize("name", ["", ".", "..", "a\x00b", "x" * 300, 5])
def test_bad_names_are_rejected(name: object) -> None:
    with pytest.raises(UploadError):
        safe_file_name(name)


def test_names_are_reduced_to_their_base_name() -> None:
    assert safe_file_name("../../etc/passwd") == "passwd"
    assert safe_file_name("C:\\Users\\me\\invoice.pdf") == "invoice.pdf"


def test_limits_and_bad_input(tmp_path: Path) -> None:
    store = _uploads(tmp_path, max_file=5, max_session=8)
    with pytest.raises(UploadError, match="upload limit of 5"):
        _upload(store, "big.bin", b"123456")
    _upload(store, "a.bin", b"12345")
    with pytest.raises(UploadError, match="session exceeds"):
        _upload(store, "b.bin", b"1234")
    upload_id = store.handle(UPLOAD_BEGIN_COMMAND, {"name": "c.bin"})["uploadId"]
    with pytest.raises(UploadError, match="base64"):
        store.handle(UPLOAD_CHUNK_COMMAND, {"uploadId": upload_id, "data": "!!!"})
    with pytest.raises(UploadError, match="unknown uploadId"):
        store.handle(UPLOAD_END_COMMAND, {"uploadId": "nope"})
    with pytest.raises(UploadError, match="unknown Statelock command"):
        store.handle("Statelock.somethingElse", {})


def test_close_removes_files(tmp_path: Path) -> None:
    store = _uploads(tmp_path)
    path = Path(_upload(store, "a.txt", b"abc")["path"])
    store.close()
    assert not path.exists()
    with pytest.raises(UploadError):
        store.resolve([str(path)])
