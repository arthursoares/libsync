import { writable } from 'svelte/store';
import { api } from '$lib/api/client';
import { onEvent } from './websocket';

export const albums = writable<any[]>([]);
export const totalAlbums = writable(0);
export const currentSource = writable('qobuz');
export const searchQuery = writable('');
export const sortBy = writable('added_to_library_at');
export const filterStatus = writable('all');
export const selectedAlbum = writable<any | null>(null);

export async function loadAlbums(source: string, params?: Record<string, string>) {
  const data = await api.library.getAlbums(source, params);
  albums.set(data.albums);
  totalAlbums.set(data.total);
}

export async function loadAlbumDetail(source: string, id: number) {
  const data = await api.library.getAlbum(source, id);
  selectedAlbum.set(data);
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
  selectedAlbum.update((current) =>
    current && current.id === albumId ? { ...current, download_status: status } : current
  );
});
