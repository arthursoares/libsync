// @ts-nocheck
import assert from 'node:assert/strict';
import test from 'node:test';
import { mount, unmount, settle, deferred, button } from './dom.js';

const { default: Library } = await import('../src/routes/library/+page.svelte');
const { default: Search } = await import('../src/routes/search/+page.svelte');
const { default: AlbumDetail } = await import('../src/lib/components/AlbumDetail.svelte');
const { api } = await import('../src/lib/api/client.ts');
const { currentSource, selectedAlbum } = await import('../src/lib/stores/library.ts');
const { lastCompletedDownload } = await import('../src/lib/stores/downloads.ts');
const { get } = await import('svelte/store');
const { connectWebSocket } = await import('../src/lib/stores/websocket.ts');
// Exercise the production socket dispatcher and album_status_changed handler.
let testSocket;
globalThis.WebSocket = class {
  constructor() { testSocket = this; }
};
connectWebSocket();
function albumStatus(id, status) {
  testSocket.onmessage({ data: JSON.stringify({ type: 'album_status_changed',
    data: { album_id: id, status } }) });
}
const album = (source) => ({ id: source === 'qobuz' ? 1 : 2, source,
  source_album_id: '123', title: `${source} album`, artist: 'Artist', tracks: [] });

async function start(Page) {
  currentSource.set('qobuz');
  selectedAlbum.set(null);
  api.auth.status = async () => [];
  api.library.getAlbums = async (source) => ({ albums: [album(source)], total: 1 });
  api.library.search = async (source) => ({ albums: [album(source)], total: 1 });
  api.downloads.getQueue = async () => ({ items: [], active_count: 0, total_speed: 0 });
  const component = mount(Page, { target: document.body });
  await settle();
  if (Page === Search) {
    const input = document.querySelector('.search-input');
    input.value = 'test';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    await settle();
  }
  return component;
}

for (const [name, Page] of [['Library', Library], ['Search', Search]]) {
  test(`${name} clears cross-source selection before queuing the overlapping Tidal catalog ID`, async () => {
    const component = await start(Page);
    const queued = [];
    api.downloads.enqueue = async (source, ids) => queued.push([source, ids]);
    try {
      document.querySelector('[title="Multi-select"]').click();
      await settle();
      document.querySelector('.album-grid input[type="checkbox"]').click();
      await settle();
      assert.equal(document.querySelector('.batch-count').textContent, '1 selected');
      currentSource.set('tidal');
      await settle();
      assert.equal(document.querySelector('.batch-bar'), null);
      assert.equal(document.querySelector('[title="Multi-select"]').classList.contains('active'), false);
      document.querySelector('[title="Multi-select"]').click();
      await settle();
      document.querySelector('.album-grid input[type="checkbox"]').click();
      await settle();
      button('▸ Download', document.querySelector('.batch-bar')).click();
      await settle();
      assert.deepEqual(queued, [['tidal', ['123']]]);
    } finally {
      await unmount(component);
      document.body.replaceChildren();
    }
  });

  test(`${name} closes detail on source change and ignores late detail responses`, async () => {
    const component = await start(Page);
    const oldDetail = deferred();
    const newDetail = deferred();
    const requests = [];
    api.library.getAlbum = (source, id) => {
      requests.push([source, id]);
      return source === 'qobuz' ? oldDetail.promise : newDetail.promise;
    };
    try {
      document.querySelector('.album-card').click();
      await settle();
      assert.ok(document.querySelector('.detail-panel.open'));
      currentSource.set('tidal');
      await settle();
      assert.equal(document.querySelector('.detail-panel.open'), null);
      assert.equal(get(selectedAlbum), null);
      document.querySelector('.album-card').click();
      await settle();
      newDetail.resolve({ ...album('tidal'), title: 'Current detail' });
      await settle();
      oldDetail.resolve({ ...album('qobuz'), title: 'Stale detail' });
      await settle();
      assert.equal(document.querySelector('.detail-album-title').textContent, 'Current detail');
      assert.deepEqual(requests, [['qobuz', 1], ['tidal', 2]]);
      const refresh = deferred();
      api.library.getAlbum = () => refresh.promise;
      lastCompletedDownload.set({ source: 'tidal', source_album_id: '123' });
      await settle();
      document.querySelector('.detail-close').click();
      await settle();
      refresh.resolve({ ...album('tidal'), title: 'Closed detail response' });
      await settle();
      assert.equal(get(selectedAlbum), null);
    } finally {
      await unmount(component);
      document.body.replaceChildren();
      lastCompletedDownload.set(null);
    }
  });
}

test('AlbumDetail download and refresh use the displayed album source, not the global service', async () => {
  currentSource.set('tidal');
  lastCompletedDownload.set(null);
  const queued = [];
  const refreshed = [];
  api.downloads.enqueue = async (source, ids) => queued.push([source, ids]);
  api.downloads.getQueue = async () => ({ items: [], active_count: 0, total_speed: 0 });
  api.library.getAlbum = async (source, id) => { refreshed.push([source, id]); return album(source); };
  const component = mount(AlbumDetail, { target: document.body, props: { album: album('qobuz'), open: true } });
  try {
    await settle();
    button('▸ Download').click();
    await settle();
    assert.deepEqual(queued, [['qobuz', ['123']]]);
    lastCompletedDownload.set({ source: 'tidal', source_album_id: '123' });
    await settle();
    assert.deepEqual(refreshed, [], 'a matching ID from another service is not this album');
    lastCompletedDownload.set({ source: 'qobuz', source_album_id: '123' });
    await settle();
    assert.deepEqual(refreshed, [['qobuz', 1]], 'detail API takes the local database ID');
  } finally {
    await unmount(component);
    document.body.replaceChildren();
    lastCompletedDownload.set(null);
  }
});

test('Library applies pending detail tracks without overwriting a newer same-album status event', async () => {
  const component = await start(Library);
  const detail = deferred();
  api.library.getAlbum = () => detail.promise;
  try {
    document.querySelector('.album-card').click();
    await settle();
    albumStatus(1, 'complete');
    await settle();
    detail.resolve({ ...album('qobuz'), download_status: 'not_downloaded',
      tracks: [{ title: 'Loaded track', track_number: 1 }] });
    await settle();
    assert.ok(document.querySelector('.detail-panel.open'));
    assert.match(document.querySelector('.detail-panel').textContent, /Loaded track/);
    assert.equal(get(selectedAlbum).download_status, 'complete');
    assert.ok(button('Unmark as downloaded'));
  } finally {
    await unmount(component);
    document.body.replaceChildren();
  }
});

for (const [initial, action, next] of [
  ['not_downloaded', 'Mark as downloaded', 'complete'],
  ['complete', 'Unmark as downloaded', 'not_downloaded'],
]) {
  test(`Library ${action} refreshes tracks when its status event precedes the HTTP response`, async () => {
    const component = await start(Library);
    const mutation = deferred();
    let requests = 0;
    api.library.getAlbum = async () => {
      requests++;
      return { ...album('qobuz'), download_status: requests === 1 ? initial : next,
        tracks: requests === 1 ? [] : [{ title: 'Refreshed track', track_number: 1 }] };
    };
    api.library.markDownloaded = () => mutation.promise;
    api.library.unmarkDownloaded = () => mutation.promise;
    try {
      document.querySelector('.album-card').click();
      await settle();
      button(action).click();
      await settle();
      albumStatus(1, next);
      await settle();
      mutation.resolve({});
      await settle();
      assert.equal(requests, 2, 'the same selection must still refresh after its object is patched');
      assert.match(document.querySelector('.detail-panel').textContent, /Refreshed track/);
      assert.equal(get(selectedAlbum).download_status, next);
    } finally {
      await unmount(component);
      document.body.replaceChildren();
    }
  });
}

test('Library close and reopen of the same album invalidates the old detail lifecycle', async () => {
  const component = await start(Library);
  const old = deferred();
  const reopened = deferred();
  let requests = 0;
  api.library.getAlbum = () => (++requests === 1 ? old.promise : reopened.promise);
  try {
    document.querySelector('.album-card').click();
    await settle();
    document.querySelector('.detail-close').click();
    await settle();
    document.querySelector('.album-card').click();
    await settle();
    old.resolve({ ...album('qobuz'), title: 'Old lifecycle' });
    await settle();
    assert.equal(document.querySelector('.detail-album-title').textContent, 'qobuz album');
    reopened.resolve({ ...album('qobuz'), title: 'Reopened lifecycle' });
    await settle();
    assert.equal(document.querySelector('.detail-album-title').textContent, 'Reopened lifecycle');
  } finally {
    await unmount(component);
    document.body.replaceChildren();
  }
});

test('Library does not refresh a reopened selection when an old mark request completes', async () => {
  const component = await start(Library);
  const mutation = deferred();
  let requests = 0;
  api.library.getAlbum = async () => { requests++; return album('qobuz'); };
  api.library.markDownloaded = () => mutation.promise;
  try {
    document.querySelector('.album-card').click();
    await settle();
    button('Mark as downloaded').click();
    await settle();
    document.querySelector('.detail-close').click();
    await settle();
    document.querySelector('.album-card').click();
    await settle();
    assert.equal(requests, 2);
    mutation.resolve({});
    await settle();
    assert.equal(requests, 2, 'the previous selection lifecycle cannot refresh this one');
  } finally {
    await unmount(component);
    document.body.replaceChildren();
  }
});
