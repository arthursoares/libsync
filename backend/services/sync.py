"""Sync service — diff streaming library against local downloads."""

import asyncio
import logging

from ..models.database import AppDatabase
from .event_bus import EventBus
from .tasks import await_task_completion

logger = logging.getLogger("streamrip")


class SyncServiceStoppingError(RuntimeError):
    """The sync service is shutting down and no longer accepts work."""


class SyncService:
    def __init__(
        self,
        db: AppDatabase,
        event_bus: EventBus,
        clients: dict,
        library_service,
        download_service=None,
    ):
        self.db = db
        self.event_bus = event_bus
        self.clients = clients
        self.library_service = library_service
        self.download_service = download_service
        self._auto_sync_task: asyncio.Task | None = None
        self._sync_tasks: set[asyncio.Task] = set()
        self._shutdown_task: asyncio.Task[None] | None = None
        self._stopping = False

    async def get_diff(self, source: str) -> dict:
        """Compare streaming library against local database."""
        client = self.clients.get(source)
        # Both SDK clients open their transport via __aenter__ in lifespan
        # and don't expose ``logged_in`` — treat presence of the client as
        # sufficient. If the session is actually down, the SDK call below
        # will surface a proper error.
        if client is None:
            return {
                "new_albums": [],
                "removed_albums": [],
                "source": source,
                "last_sync": None,
                "connected": False,
            }

        all_items = await self.library_service.fetch_all_favorites(source, client)

        streaming_ids = set()
        new_albums = []

        for item in all_items:
            parsed = self.library_service._extract_album_data(source, item)
            if parsed is None:
                continue
            source_album_id = parsed["source_album_id"]
            streaming_ids.add(source_album_id)

            existing = self.db.get_album_by_source_id(source, source_album_id)
            if existing is None:
                new_albums.append(parsed)

        # Find removed: albums in DB but not in streaming library
        local_albums = self.db.get_albums(source, limit=10000)
        removed_albums = [
            a for a in local_albums if a["source_album_id"] not in streaming_ids
        ]

        # Get last sync time
        history = self.db.get_sync_history(source, limit=1)
        last_sync = history[0]["completed_at"] if history else None

        return {
            "new_albums": new_albums,
            "removed_albums": removed_albums,
            "source": source,
            "last_sync": last_sync,
            "connected": True,
        }

    async def run_sync(self, source: str, download_new: bool = False) -> dict:
        """Run a full sync: refresh library, optionally enqueue new albums.

        When ``download_new=True`` and a ``download_service`` was passed
        to the constructor, the new albums detected by the diff are
        enqueued for download via ``download_service.enqueue``.  The
        returned dict reports how many were queued, but the downloads
        themselves run async in the worker — this method does not block
        on completion.
        """
        if self._stopping:
            raise SyncServiceStoppingError("Sync service is shutting down")

        current = asyncio.current_task()
        if current is not None:
            self._sync_tasks.add(current)
        run_id: int | None = None

        try:
            run_id = self.db.create_sync_run(source)
            await self.event_bus.publish(
                "sync_started", {"source": source, "run_id": run_id}
            )

            # Refresh library from streaming API.  refresh_library returns
            # the IDs of newly-discovered albums so we can enqueue them for
            # download *before* a second get_diff pass (which would see them
            # as already in the DB and return an empty new_albums list).
            refresh_result = await self.library_service.refresh_library(source)
            new_ids = refresh_result.get("new_album_ids", [])

            albums_downloaded = 0
            if download_new and self.download_service is not None and new_ids:
                logger.info(
                    "Auto-sync enqueueing %d new %s albums for download",
                    len(new_ids),
                    source,
                )
                try:
                    await self.download_service.enqueue(source, new_ids)
                    albums_downloaded = len(new_ids)
                except Exception:
                    logger.exception("Failed to enqueue auto-sync downloads")

            self.db.complete_sync_run(
                run_id,
                albums_found=refresh_result["total"],
                albums_new=refresh_result["new"],
                albums_removed=0,
                albums_downloaded=albums_downloaded,
            )

            await self.event_bus.publish(
                "sync_complete",
                {
                    "source": source,
                    "run_id": run_id,
                    "new_count": refresh_result["new"],
                    "downloaded_count": albums_downloaded,
                },
            )

            return {
                "run_id": run_id,
                "albums_found": refresh_result["total"],
                "albums_new": refresh_result["new"],
                "albums_downloaded": albums_downloaded,
                "status": "complete",
            }
        except asyncio.CancelledError:
            if run_id is not None:
                self.db.interrupt_sync_run(run_id)
            raise
        except Exception as e:
            logger.exception("Sync failed for %s", source)
            if run_id is not None:
                self.db.fail_sync_run(run_id)
            return {"run_id": run_id, "status": "failed", "error": str(e)}
        finally:
            if current is not None:
                self._sync_tasks.discard(current)

    async def get_history(self, source: str, limit: int = 10) -> list[dict]:
        return self.db.get_sync_history(source, limit=limit)

    async def start_auto_sync(
        self,
        source: str,
        interval_seconds: float,
        download_new: bool = True,
    ):
        """Start a background auto-sync task.

        ``download_new`` is forwarded to ``run_sync``: when True (default),
        new albums detected by each scheduled diff are enqueued for
        download.  Set to False to refresh the library only without
        kicking off downloads.
        """
        if self._stopping:
            raise SyncServiceStoppingError("Sync service is shutting down")
        await self.stop_auto_sync()
        if self._stopping:
            raise SyncServiceStoppingError("Sync service is shutting down")

        async def _auto_sync_loop():
            while not self._stopping:
                await asyncio.sleep(interval_seconds)
                if self._stopping:
                    break
                try:
                    logger.info(
                        "Auto-sync running for %s (download_new=%s)",
                        source,
                        download_new,
                    )
                    await self.run_sync(source, download_new=download_new)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Auto-sync failed for %s", source)

        task = asyncio.create_task(_auto_sync_loop())
        self._auto_sync_task = task

        def clear_finished(done: asyncio.Task) -> None:
            if self._auto_sync_task is done:
                self._auto_sync_task = None

        task.add_done_callback(clear_finished)

    async def stop_auto_sync(self) -> None:
        task = self._auto_sync_task
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        try:
            await await_task_completion(
                task,
                operation="auto-sync scheduler stop",
                suppress_inner_cancellation=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Auto-sync scheduler failed while stopping")
        finally:
            if task.done() and self._auto_sync_task is task:
                self._auto_sync_task = None

    def begin_shutdown(self) -> None:
        """Synchronously close manual and scheduled sync admission."""
        self._stopping = True

    async def _drain_shutdown(self) -> None:
        await self.stop_auto_sync()

        current = asyncio.current_task()
        tasks = {
            task for task in self._sync_tasks if task is not current and not task.done()
        }
        if tasks:
            logger.info("Shutdown interrupting %d active sync task(s)", len(tasks))
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def shutdown(self) -> None:
        """Stop scheduled work and retain drainage through caller cancellation."""
        self.begin_shutdown()
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(self._drain_shutdown())
        await await_task_completion(
            self._shutdown_task,
            operation="sync service shutdown",
        )
