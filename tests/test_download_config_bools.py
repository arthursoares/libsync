"""Boolean config values must be honored regardless of casing.

Regression: settings POSTed as Pydantic ``bool`` are persisted via
``str(value)`` (yielding ``"True"``/``"False"``).  The download path
compared against lowercase ``"false"`` / ``("true", "1")`` literals,
which silently inverted the user's intent for several knobs:

  - ``embed_artwork = False``     → embed_cover stayed True
  - ``source_subdirectories = True`` → stayed False
  - ``disc_subdirectories = False`` → stayed True
  - ``qobuz_download_booklets = False`` → stayed True
"""
import os
import tempfile

import pytest

from backend.models.database import AppDatabase
from backend.services.download import DownloadService
from backend.services.event_bus import EventBus


@pytest.fixture
def db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    database = AppDatabase(path)
    yield database
    os.unlink(path)


@pytest.fixture
def service(db):
    return DownloadService(db, EventBus(), clients={}, download_path="/tmp")


class TestCapitalizedBooleanConfigsHonored:
    def test_embed_artwork_capitalized_false_disables_cover(self, db, service):
        db.set_config("embed_artwork", "False")
        kwargs = service._build_dl_config_kwargs(
            source="qobuz", item={"force": False}, quality=3, downloads_db=None
        )
        assert kwargs["embed_cover"] is False

    def test_source_subdirectories_capitalized_true_enables(self, db, service):
        db.set_config("source_subdirectories", "True")
        kwargs = service._build_dl_config_kwargs(
            source="qobuz", item={"force": False}, quality=3, downloads_db=None
        )
        assert kwargs["source_subdirectories"] is True

    def test_disc_subdirectories_capitalized_false_disables(self, db, service):
        db.set_config("disc_subdirectories", "False")
        kwargs = service._build_dl_config_kwargs(
            source="qobuz", item={"force": False}, quality=3, downloads_db=None
        )
        assert kwargs["disc_subdirectories"] is False

    def test_qobuz_download_booklets_capitalized_false_disables(self, db, service):
        db.set_config("qobuz_download_booklets", "False")
        kwargs = service._build_dl_config_kwargs(
            source="qobuz", item={"force": False}, quality=3, downloads_db=None
        )
        assert kwargs["download_booklets"] is False


class TestLowercaseBooleansStillWork:
    """Backwards compatibility: any value stored before the fix used lowercase."""

    def test_embed_artwork_lowercase_false_still_disables(self, db, service):
        db.set_config("embed_artwork", "false")
        kwargs = service._build_dl_config_kwargs(
            source="qobuz", item={"force": False}, quality=3, downloads_db=None
        )
        assert kwargs["embed_cover"] is False

    def test_source_subdirectories_lowercase_true_still_enables(self, db, service):
        db.set_config("source_subdirectories", "true")
        kwargs = service._build_dl_config_kwargs(
            source="qobuz", item={"force": False}, quality=3, downloads_db=None
        )
        assert kwargs["source_subdirectories"] is True


class TestBooleanDefaults:
    """When a config key is unset, defaults must match pre-fix behavior."""

    def test_embed_artwork_unset_defaults_true(self, db, service):
        kwargs = service._build_dl_config_kwargs(
            source="qobuz", item={"force": False}, quality=3, downloads_db=None
        )
        assert kwargs["embed_cover"] is True

    def test_source_subdirectories_unset_defaults_false(self, db, service):
        kwargs = service._build_dl_config_kwargs(
            source="qobuz", item={"force": False}, quality=3, downloads_db=None
        )
        assert kwargs["source_subdirectories"] is False

    def test_disc_subdirectories_unset_defaults_true(self, db, service):
        kwargs = service._build_dl_config_kwargs(
            source="qobuz", item={"force": False}, quality=3, downloads_db=None
        )
        assert kwargs["disc_subdirectories"] is True

    def test_download_booklets_only_present_for_qobuz(self, db, service):
        qobuz_kwargs = service._build_dl_config_kwargs(
            source="qobuz", item={"force": False}, quality=3, downloads_db=None
        )
        tidal_kwargs = service._build_dl_config_kwargs(
            source="tidal", item={"force": False}, quality=3, downloads_db=None
        )
        assert "download_booklets" in qobuz_kwargs
        assert "download_booklets" not in tidal_kwargs
