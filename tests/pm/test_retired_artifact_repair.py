"""A retired rolling artifact can be repaired without weakening pin integrity."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest

from pm.install import _install
from pm.lock import Facts, Lockfile
from pm.package import InstallError, Package
from pm.store import Store
from tests.pm._range_server import RangeHandler, dl_server, url  # noqa: F401


class RollingPackage(Package):
    name = "rolling-test"
    version_style = "minor"
    on_path = False

    def __init__(self, old_url: str, live_url: str, digest: str, versions=None):
        self.old_url = old_url
        self.live_url = live_url
        self.digest = digest
        self.versions = list(versions or ["9.0.2"])
        self.latest_calls = 0
        self.fetch_versions = []
        self.digest_versions = []

    def latest_versions(self, target: str, locked=None) -> list[str]:
        self.latest_calls += 1
        return list(self.versions)

    def fetch_url(self, version: str, target: str) -> str:
        self.fetch_versions.append(version)
        if version == "9.0.1":
            return self.old_url
        if version in ("9.0.2", "9.1.1"):
            return self.live_url
        raise InstallError(self.name, f"no test artifact for {version}")

    def known_sha256(self, version: str, artifact_url: str) -> str | None:
        self.digest_versions.append(version)
        return self.digest if artifact_url == self.live_url else None


def _zip_payload() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("payload.txt", "repaired")
    return stream.getvalue()


def _locked(tmp_path, old_url: str, digest: str) -> Lockfile:
    lockfile = Lockfile(tmp_path / "lock.json")
    lockfile.set_pin(
        RollingPackage.name,
        "9.0.1",
        {"linux-x64": {"url": old_url, "sha256": digest}},
    )
    lockfile.save()
    return Lockfile(lockfile.path)


def _mirror_403(monkeypatch, dl_server, requests):
    from pm import artifact_mirror

    mirror = url(dl_server, "/mirror")
    monkeypatch.setattr(artifact_mirror, "mirror_url", lambda _digest: mirror)
    original = RangeHandler.do_GET

    def respond(handler):
        requests.append(handler.path)
        if handler.path == "/mirror":
            handler.send_error(403)
        else:
            original(handler)

    monkeypatch.setattr(RangeHandler, "do_GET", respond)
    return mirror


def test_retired_origin_repins_same_minor_and_retries_once(tmp_path, dl_server, monkeypatch):
    payload = _zip_payload()
    digest = hashlib.sha256(payload).hexdigest()
    old = url(dl_server, "/retired.zip")
    live = url(dl_server, "/live.zip")
    RangeHandler.payloads = {"/live.zip": payload}
    requests = []
    _mirror_403(monkeypatch, dl_server, requests)

    package = RollingPackage(old, live, digest, versions=["9.1.0", "9.0.2"])
    lockfile = _locked(tmp_path, old, "0" * 64)
    entry = _install(
        package,
        lockfile,
        Facts(tmp_path / "facts.json"),
        Store(tmp_path / "store"),
        "linux-x64",
    )

    assert (entry / "payload.txt").read_text() == "repaired"
    shipped = json.loads(lockfile.path.read_text())
    pin = shipped["packages"][package.name]
    assert pin["version"] == "9.0.1"
    assert pin["artifacts"]["linux-x64"] == {"url": old, "sha256": "0" * 64}
    fact = Facts(tmp_path / "facts.json").get(package.name)
    assert fact["replaces"] == ["0" * 64]
    assert fact["artifact_rows"] == [{"url": live, "sha256": digest}]
    assert package.fetch_versions == ["9.0.2"]
    assert package.digest_versions == ["9.0.2"]
    assert requests.count("/retired.zip") == 1
    assert requests.count("/mirror") == 1
    assert requests.count("/live.zip") >= 1


def test_hash_mismatch_never_becomes_a_repin(tmp_path, dl_server, monkeypatch):
    old = url(dl_server, "/old.zip")
    live = url(dl_server, "/live.zip")
    RangeHandler.payloads = {"/old.zip": b"changed upstream", "/live.zip": _zip_payload()}
    package = RollingPackage(old, live, hashlib.sha256(RangeHandler.payloads["/live.zip"]).hexdigest())
    lockfile = _locked(tmp_path, old, hashlib.sha256(b"expected bytes").hexdigest())

    with pytest.raises(InstallError) as failure:
        _install(
            package,
            lockfile,
            Facts(tmp_path / "facts.json"),
            Store(tmp_path / "store"),
            "linux-x64",
        )

    from pm.downloader import HashError

    assert isinstance(failure.value.__cause__, HashError)
    assert package.latest_calls == 0
    assert Lockfile(lockfile.path).artifacts(package.name, "linux-x64")[0]["url"] == old


def test_access_denial_is_not_treated_as_retirement(tmp_path, dl_server, monkeypatch):
    old = url(dl_server, "/blocked.zip")
    live = url(dl_server, "/live.zip")
    RangeHandler.payloads = {"/live.zip": _zip_payload()}
    requests = []
    _mirror_403(monkeypatch, dl_server, requests)
    original = RangeHandler.do_GET

    def blocked(handler):
        if handler.path == "/blocked.zip":
            requests.append(handler.path)
            handler.send_error(403)
        else:
            original(handler)

    monkeypatch.setattr(RangeHandler, "do_GET", blocked)
    package = RollingPackage(old, live, hashlib.sha256(RangeHandler.payloads["/live.zip"]).hexdigest())
    lockfile = _locked(tmp_path, old, "0" * 64)

    with pytest.raises(InstallError):
        _install(
            package,
            lockfile,
            Facts(tmp_path / "facts.json"),
            Store(tmp_path / "store"),
            "linux-x64",
        )

    assert package.latest_calls == 0
    assert Lockfile(lockfile.path).artifacts(package.name, "linux-x64")[0]["url"] == old


def test_unchanged_index_preserves_original_failure(tmp_path, dl_server, monkeypatch):
    old = url(dl_server, "/retired.zip")
    requests = []
    _mirror_403(monkeypatch, dl_server, requests)
    package = RollingPackage(old, url(dl_server, "/unused.zip"), "f" * 64, versions=["9.0.1"])
    lockfile = _locked(tmp_path, old, "0" * 64)

    with pytest.raises(InstallError) as failure:
        _install(
            package,
            lockfile,
            Facts(tmp_path / "facts.json"),
            Store(tmp_path / "store"),
            "linux-x64",
        )

    assert "404" in str(failure.value) and "403" in str(failure.value)
    assert package.latest_calls == 1
    assert package.fetch_versions == ["9.0.1"]
    assert Lockfile(lockfile.path).artifacts(package.name, "linux-x64")[0]["url"] == old


def test_ffmpeg_uses_release_digest_instead_of_streaming_the_asset(monkeypatch):
    from pm import packages

    calls = []

    def digests(repo, tag):
        calls.append((repo, tag))
        return {"ffmpeg-n9.0.2-win64-gpl-9.0.zip": "abc123"}

    monkeypatch.setattr(packages, "_github_release_digests", digests)
    artifact = (
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/"
        "autobuild-2026-09-27-13-04/ffmpeg-n9.0.2-win64-gpl-9.0.zip"
    )

    assert packages.Ffmpeg().known_sha256("9.0.2", artifact) == "abc123"
    assert calls == [("BtbN/FFmpeg-Builds", "autobuild-2026-09-27-13-04")]
    assert packages.Ffmpeg().known_sha256(
        "9.0.2", "https://ffmpeg.martin-riedl.de/download/macos/arm64/build/ffmpeg.zip"
    ) is None


def test_cross_target_stage_records_repaired_identity_and_stays_warm(tmp_path, dl_server, monkeypatch):
    payload = _zip_payload()
    digest = hashlib.sha256(payload).hexdigest()
    old = url(dl_server, "/retired.zip")
    live = url(dl_server, "/live.zip")
    RangeHandler.payloads = {"/live.zip": payload}
    requests = []
    _mirror_403(monkeypatch, dl_server, requests)

    package = RollingPackage(old, live, digest)
    lockfile = _locked(tmp_path, old, "0" * 64)
    store = Store(tmp_path / "store")
    entry = _install(package, lockfile, None, store, "linux-x64")

    assert json.loads((entry / ".pm-stage-pin.json").read_text()) == {
        "target": "linux-x64",
        "sha256": [digest],
        "repair_version": "9.0.1",
        "replaces": ["0" * 64],
        "artifact_rows": [{"url": live, "sha256": digest}],
    }

    requests.clear()
    assert _install(package, lockfile, None, store, "linux-x64") == entry
    assert requests == []


def test_repaired_fact_is_bound_to_the_locked_version(tmp_path, dl_server, monkeypatch):
    payload = _zip_payload()
    digest = hashlib.sha256(payload).hexdigest()
    old = url(dl_server, "/retired.zip")
    live = url(dl_server, "/live.zip")
    next_live = url(dl_server, "/next-live.zip")
    RangeHandler.payloads = {"/live.zip": payload, "/next-live.zip": payload}
    requests = []
    _mirror_403(monkeypatch, dl_server, requests)

    package = RollingPackage(old, live, digest)
    lockfile = _locked(tmp_path, old, "0" * 64)
    facts = Facts(tmp_path / "facts.json")
    store = Store(tmp_path / "store")
    _install(package, lockfile, facts, store, "linux-x64")

    lockfile.set_pin(
        package.name,
        "9.1.0",
        {"linux-x64": {"url": old, "sha256": "0" * 64}},
    )
    lockfile.save()
    package.live_url = next_live
    package.versions = ["9.1.1"]

    requests.clear()
    _install(package, Lockfile(lockfile.path), Facts(facts.path), store, "linux-x64")

    assert "/retired.zip" in requests
    fact = Facts(facts.path).get(package.name)
    assert fact["version"] == "9.1.0"
    assert fact["artifact_rows"][0]["url"] == next_live


def test_copying_a_repaired_entry_preserves_its_effective_artifact_identity(
    tmp_path, dl_server, monkeypatch,
):
    payload = _zip_payload()
    digest = hashlib.sha256(payload).hexdigest()
    old = url(dl_server, "/retired.zip")
    live = url(dl_server, "/live.zip")
    RangeHandler.payloads = {"/live.zip": payload}
    _mirror_403(monkeypatch, dl_server, [])

    package = RollingPackage(old, live, digest)
    lockfile = _locked(tmp_path, old, "0" * 64)
    source_store = Store(tmp_path / "source")
    source_facts = Facts(tmp_path / "source-facts.json")
    _install(package, lockfile, source_facts, source_store, "linux-x64")

    destination_store = Store(tmp_path / "destination")
    destination_facts = Facts(tmp_path / "destination-facts.json")
    _install(
        package,
        lockfile,
        destination_facts,
        destination_store,
        "linux-x64",
        copy_from=(Facts(source_facts.path), source_store),
    )

    copied = Facts(destination_facts.path).get(package.name)
    assert copied["replaces"] == ["0" * 64]
    assert copied["artifacts"] == [digest]
    assert copied["artifact_rows"] == [{"url": live, "sha256": digest}]


def test_malformed_stage_repair_is_ignored_instead_of_crashing(tmp_path):
    old = "https://supplier.invalid/old.zip"
    package = RollingPackage(old, "https://supplier.invalid/live.zip", "a" * 64)
    lockfile = _locked(tmp_path, old, "0" * 64)
    store = Store(tmp_path / "store")
    entry = store.entry(package.store_entry("9.0.1", "linux-x64"))
    entry.mkdir(parents=True)
    (entry / ".pm-stage-pin.json").write_text("[]", encoding="utf-8")

    from pm.install import _stage_artifact_repair

    assert _stage_artifact_repair(
        entry,
        "9.0.1",
        "linux-x64",
        lockfile.artifacts(package.name, "linux-x64"),
    ) is None
