"""Tests for the SPA static-file catch-all route in backend.main."""

import pytest

from backend.main import create_app


def _get_serve_frontend(app):
    for route in app.routes:
        if getattr(route, "path", None) == "/{path:path}":
            return route.endpoint
    return None


@pytest.fixture
def app_with_static(tmp_path, monkeypatch):
    fake_backend = tmp_path / "backend"
    fake_backend.mkdir()
    static_dir = fake_backend / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html>SPA</html>")
    (static_dir / "app.js").write_text("// bundle")
    # Place secrets ONE level above static_dir so "../secret.txt" actually escapes.
    secret_one_up = fake_backend / "secret.txt"
    secret_one_up.write_text("SHOULD NOT BE SERVED")
    secret_two_up = tmp_path / "deep_secret.txt"
    secret_two_up.write_text("SHOULD NOT BE SERVED")

    monkeypatch.setattr("backend.main.__file__", str(fake_backend / "main.py"))
    return create_app(db_path=":memory:"), tmp_path, static_dir


class TestStaticFileServing:
    async def test_serves_legitimate_bundle_file(self, app_with_static):
        app, _, static_dir = app_with_static
        handler = _get_serve_frontend(app)
        assert handler is not None, (
            "catch-all route not registered when static dir exists"
        )

        result = await handler(path="app.js")
        assert result.path == str(static_dir / "app.js")

    async def test_falls_back_to_index_for_unknown_paths(self, app_with_static):
        app, _, static_dir = app_with_static
        handler = _get_serve_frontend(app)

        result = await handler(path="some/spa/route")
        assert result.path == str(static_dir / "index.html")

    async def test_traversal_does_not_serve_files_outside_static_dir(
        self, app_with_static
    ):
        app, _tmp_path, static_dir = app_with_static
        handler = _get_serve_frontend(app)
        secret_path = static_dir.parent / "secret.txt"
        assert secret_path.exists()

        result = await handler(path="../secret.txt")

        # Must NOT serve the secret file outside the static dir.
        assert result.path != str(secret_path), (
            "path traversal escaped static_dir — file disclosure vulnerability"
        )
        # Should fall back to index.html (SPA behavior for unknown routes).
        assert result.path == str(static_dir / "index.html")

    async def test_traversal_with_nested_segments_blocked(self, app_with_static):
        app, tmp_path, static_dir = app_with_static
        handler = _get_serve_frontend(app)
        deep_secret = tmp_path / "deep_secret.txt"
        assert deep_secret.exists()

        result = await handler(path="../../deep_secret.txt")
        assert result.path != str(deep_secret), (
            "nested path traversal escaped static_dir"
        )
        assert result.path == str(static_dir / "index.html")
