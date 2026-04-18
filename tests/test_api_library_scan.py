"""API tests for /scan-fuzzy, /mark-downloaded, /unmark-downloaded."""
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

    async def test_with_folder(self, client, album_id, tmp_path):
        folder = tmp_path / "Beatles - Abbey Road"
        folder.mkdir()
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
