import { addToast } from '$lib/stores/toast';
import { readApiErrorMessage } from './error-message.js';

const BASE = '/api';

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!resp.ok) {
    const errorText = await readApiErrorMessage(resp, `HTTP ${resp.status}`);
    addToast(`API error: ${errorText}`, 'error');
    throw new Error(errorText);
  }
  return resp.json();
}

export const api = {
  library: {
    getAlbums: (source: string, params?: Record<string, string>) => {
      const qs = params ? `?${new URLSearchParams(params)}` : '';
      return request<any>(`/library/${source}/albums${qs}`);
    },
    getAlbum: (source: string, id: number) =>
      request<any>(`/library/${source}/albums/${id}`),
    refresh: (source: string) =>
      request<any>(`/library/refresh/${source}`, { method: 'POST' }),
    search: (source: string, query: string, params?: Record<string, string>) => {
      const qs = new URLSearchParams({ q: query, ...(params ?? {}) });
      return request<any>(`/library/search/${source}?${qs}`);
    },
    listPlaylists: (source: string) =>
      request<any[]>(`/library/${source}/playlists`),
    getPlaylist: (source: string, id: number) =>
      request<any>(`/library/${source}/playlists/${id}`),
    scanFuzzy: () =>
      request<{ job_id: string }>('/library/scan-fuzzy', { method: 'POST' }),
    scanFuzzyStatus: (jobId: string) =>
      request<any>(`/library/scan-fuzzy/${jobId}`),
    markDownloaded: (albumId: number, localFolderPath: string | null) =>
      request<any>(`/library/albums/${albumId}/mark-downloaded`, {
        method: 'POST',
        body: JSON.stringify({ local_folder_path: localFolderPath }),
      }),
    unmarkDownloaded: (albumId: number) =>
      request<any>(`/library/albums/${albumId}/unmark-downloaded`, { method: 'POST' }),
  },
  downloads: {
    getQueue: () => request<any>('/downloads/queue'),
    enqueue: (
      source: string,
      albumIds: string[],
      options?: {
        force?: boolean;
        albums?: Array<{
          source_album_id: string;
          title: string;
          artist: string;
          cover_url?: string | null;
          track_count?: number | null;
          release_date?: string | null;
        }>;
      },
    ) =>
      request<any>('/downloads/queue', {
        method: 'POST',
        body: JSON.stringify({
          source,
          album_ids: albumIds,
          force: options?.force ?? false,
          albums: options?.albums,
        }),
      }),
    cancel: (itemId: string) =>
      request<any>(`/downloads/queue/${itemId}`, { method: 'DELETE' }),
    cancelAll: () =>
      request<any>('/downloads/cancel', { method: 'POST' }),
  },
  config: {
    get: () => request<any>('/config'),
    update: (data: Record<string, unknown>) =>
      request<any>('/config', { method: 'PATCH', body: JSON.stringify(data) }),
  },
  auth: {
    status: () => request<any[]>('/auth/status'),
  },
};
