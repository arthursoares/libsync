"""API tests for /scan-fuzzy, /mark-downloaded, /unmark-downloaded."""
import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import create_app


@pytest.fixture
def app():
    return create_app(db_path=":memory:")


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def album_id(app):
    db = app.state.db
    aid = db.upsert_album(
        source="qobuz", source_album_id="42",
        title="Abbey Road", artist="The Beatles",
        track_count=17, bit_depth=24, sample_rate=96.0,
    )
    db.upsert_track(album_id=aid, source_track_id="t1",
                    title="Come Together", artist="The Beatles",
                    track_number=1)
    return aid


class TestMarkDownloaded:
    async def test_happy_path(self, client, album_id):
        resp = await client.post(
            f"/api/library/albums/{album_id}/mark-downloaded",
            json={"local_folder_path": None},
        )
        assert resp.status_code == 200
        assert resp.json()["download_status"] == "complete"

    async def test_with_folder(self, client, app, album_id, tmp_path):
        folder = tmp_path / "Beatles - Abbey Road"
        folder.mkdir()
        app.state.db.set_config("downloads_path", str(tmp_path))
        resp = await client.post(
            f"/api/library/albums/{album_id}/mark-downloaded",
            json={"local_folder_path": str(folder)},
        )
        assert resp.status_code == 200
        assert resp.json()["local_folder_path"] == str(folder)

    async def test_unmark_reverses(self, client, album_id):
        await client.post(f"/api/library/albums/{album_id}/mark-downloaded", json={})
        resp = await client.post(f"/api/library/albums/{album_id}/unmark-downloaded")
        assert resp.status_code == 200
        assert resp.json()["download_status"] == "not_downloaded"

    async def test_unknown_album_returns_404(self, client):
        resp = await client.post("/api/library/albums/99999/mark-downloaded", json={})
        assert resp.status_code == 404

    async def test_rejects_path_outside_downloads_root(self, client, app, album_id, tmp_path):
        app.state.db.set_config("downloads_path", str(tmp_path / "music"))
        (tmp_path / "music").mkdir()
        # Request a path that escapes the downloads root.
        resp = await client.post(
            f"/api/library/albums/{album_id}/mark-downloaded",
            json={"local_folder_path": str(tmp_path / "elsewhere")},
        )
        assert resp.status_code == 400

    async def test_rejects_dot_dot_traversal(self, client, app, album_id, tmp_path):
        downloads_root = tmp_path / "music"
        downloads_root.mkdir()
        app.state.db.set_config("downloads_path", str(downloads_root))
        # Traverses back up with ..
        resp = await client.post(
            f"/api/library/albums/{album_id}/mark-downloaded",
            json={"local_folder_path": f"{downloads_root}/../evil"},
        )
        assert resp.status_code == 400

    async def test_accepts_path_inside_downloads_root(self, client, app, album_id, tmp_path):
        downloads_root = tmp_path / "music"
        (downloads_root / "Album").mkdir(parents=True)
        app.state.db.set_config("downloads_path", str(downloads_root))
        resp = await client.post(
            f"/api/library/albums/{album_id}/mark-downloaded",
            json={"local_folder_path": str(downloads_root / "Album")},
        )
        assert resp.status_code == 200


class TestScanFuzzy:
    async def test_starts_job_and_returns_id(self, client, app, album_id, tmp_path):
        app.state.db.set_config("downloads_path", str(tmp_path / "music"))
        (tmp_path / "music").mkdir()

        resp = await client.post("/api/library/scan-fuzzy")
        assert resp.status_code == 200
        assert "job_id" in resp.json()
        # Let the background task finish so it doesn't leak into the next test.
        job_id = resp.json()["job_id"]
        for _ in range(20):
            st = (await client.get(f"/api/library/scan-fuzzy/{job_id}")).json()
            if st["status"] == "complete":
                break
            await asyncio.sleep(0.05)

    async def test_concurrent_returns_409(
        self, client, app, album_id, tmp_path, monkeypatch
    ):
        app.state.db.set_config("downloads_path", str(tmp_path / "music"))
        (tmp_path / "music").mkdir()

        # Force the job to hang so we can fire a concurrent start. The route
        # imports the module (`from ..services import scan as scan_service`)
        # and looks up `run_scan` at call time, so monkeypatching the
        # attribute on the module is what we want.
        import backend.services.scan as scan_mod

        original = scan_mod.run_scan

        async def slow(*args, **kwargs):
            await asyncio.sleep(0.3)
            return await original(*args, **kwargs)

        monkeypatch.setattr(scan_mod, "run_scan", slow)

        r1 = await client.post("/api/library/scan-fuzzy")
        r2 = await client.post("/api/library/scan-fuzzy")
        assert r1.status_code == 200
        assert r2.status_code == 409
        # Drain the first job.
        job_id = r1.json()["job_id"]
        for _ in range(20):
            st = (await client.get(f"/api/library/scan-fuzzy/{job_id}")).json()
            if st["status"] != "running":
                break
            await asyncio.sleep(0.1)

    async def test_status_endpoint(self, client, app, album_id, tmp_path):
        app.state.db.set_config("downloads_path", str(tmp_path / "music"))
        (tmp_path / "music").mkdir()

        job_id = (await client.post("/api/library/scan-fuzzy")).json()["job_id"]
        for _ in range(40):
            resp = await client.get(f"/api/library/scan-fuzzy/{job_id}")
            if resp.json()["status"] == "complete":
                break
            await asyncio.sleep(0.05)
        body = resp.json()
        assert body["status"] == "complete"
        assert "auto_matched" in body
