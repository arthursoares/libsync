import { get, writable } from 'svelte/store';
import { api } from '$lib/api/client';
import { onEvent } from './websocket';

export const albums = writable<any[]>([]);
export const totalAlbums = writable(0);
export const currentSource = writable('qobuz');
export const searchQuery = writable('');
export const sortBy = writable('added_to_library_at');
export const filterStatus = writable('all');
export const selectedAlbum = writable<any | null>(null);
let detailRequest = 0;
let selectionGeneration = 0;
let selectedStatusRevision = 0;

export function selectAlbum(album: any) {
  selectionGeneration++;
  selectedAlbum.set(album);
}

// Data patches are not a new selection. Closing/reopening even the same album is.
export function captureAlbumSelection(source: string, id: number) {
  const generation = selectionGeneration;
  return () => {
    const current = get(selectedAlbum);
    return generation === selectionGeneration && current?.source === source && current?.id === id;
  };
}

export function clearAlbumDetail() {
  detailRequest++;
  selectAlbum(null);
}

// Invalidate immediately, even before a mounted page's source effect runs.
currentSource.subscribe(clearAlbumDetail);

export async function loadAlbums(source: string, params?: Record<string, string>) {
  const data = await api.library.getAlbums(source, params);
  albums.set(data.albums);
  totalAlbums.set(data.total);
}

export async function loadAlbumDetail(source: string, id: number) {
  const request = ++detailRequest;
  const isCurrentSelection = captureAlbumSelection(source, id);
  const statusRevision = selectedStatusRevision;
  const data = await api.library.getAlbum(source, id);
  if (request !== detailRequest || get(currentSource) !== source || !isCurrentSelection()) return;
  const current = get(selectedAlbum);
  selectedAlbum.set({
    ...data,
    source,
    // A status event received during this GET is newer than its snapshot.
    ...(statusRevision !== selectedStatusRevision ? { download_status: current.download_status } : {}),
  });
}

// The backend publishes `album_status_changed` when an album is marked /
// unmarked as downloaded (either via the manual button on AlbumDetail or
// via a fuzzy-scan auto-match). Patch the matching row in `albums` and
// `selectedAlbum` in place so the Library grid and the open detail panel
// update without a manual refresh.
onEvent('album_status_changed', (data) => {
  const albumId = data.album_id as number;
  const status = data.status as string;
  if (typeof albumId !== 'number' || typeof status !== 'string') return;

  albums.update((list) =>
    list.map((a) => (a.id === albumId ? { ...a, download_status: status } : a))
  );
  selectedAlbum.update((current) => {
    if (!current || current.id !== albumId) return current;
    selectedStatusRevision++;
    return { ...current, download_status: status };
  });
});
