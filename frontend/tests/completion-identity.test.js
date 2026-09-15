// @ts-nocheck
import assert from 'node:assert/strict';
import test from 'node:test';
import { mount, unmount, settle, deferred } from './dom.js';
import { clock } from './clock.js';
const { default: Library } = await import('../src/routes/library/+page.svelte');
const { api } = await import('../src/lib/api/client.ts');
const library = await import('../src/lib/stores/library.ts');
const downloads = await import('../src/lib/stores/downloads.ts');
const { get } = await import('svelte/store');
const { connectWebSocket } = await import('../src/lib/stores/websocket.ts');
let socket;
globalThis.WebSocket = class { constructor() { socket = this; } };
connectWebSocket();
const emit = (type, data) => socket.onmessage({ data: JSON.stringify({ type, data }) });
const album = { id: 7, source: 'qobuz', source_album_id: '123', title: 'Album', artist: 'Artist', tracks: [] };
const item = { id: 'a-queue-uuid', source: 'qobuz', source_album_id: '123', album_db_id: 7,
  title: 'Album', artist: 'Artist', status: 'downloading', speed: 5 };
const snapshot = (items) => ({ items, active_count: 0, total_speed: 0 });

async function start(open = true) {
  library.currentSource.set('qobuz');
  library.clearAlbumDetail();
  downloads.lastCompletedDownload.set(null);
  api.auth.status = async () => [];
  api.library.getAlbums = async () => ({ albums: [album], total: 1 });
  const requests = [];
  api.library.getAlbum = async (source, id) => {
    requests.push([source, id]);
    return { ...album, tracks: requests.length > 1 ? [{ title: 'Completed track', track_number: 1 }] : [] };
  };
  const component = mount(Library, { target: document.body });
  await settle();
  if (open) { document.querySelector('.album-card').click(); await settle(); }
  return { component, requests };
}

for (const scenario of ['matching', 'wrong source', 'wrong album', 'closed', 'unknown resolved', 'unknown missing']) {
  test(`backend-shaped completion: ${scenario}`, async (t) => {
    const time = clock(t);
    const { component, requests } = await start(scenario !== 'closed');
    const queued = { ...item,
      source: scenario === 'wrong source' ? 'tidal' : 'qobuz',
      source_album_id: scenario === 'wrong album' ? '456' : '123' };
    downloads.queue.set(scenario.startsWith('unknown') ? [] : [queued]);
    let reconciliations = 0;
    api.downloads.getQueue = async () => {
      reconciliations++;
      return snapshot(scenario === 'unknown missing' ? [] : [{ ...queued, status: 'complete' }]);
    };
    try {
      const before = requests.length;
      emit('download_complete', { item_id: item.id, title: item.title, artist: item.artist });
      await settle(); time.advance(300); await settle();
      const matching = ['matching', 'unknown resolved'].includes(scenario);
      assert.equal(requests.length - before, matching ? 1 : 0);
      if (matching) {
        assert.deepEqual(requests.at(-1), ['qobuz', 7]);
        assert.match(document.querySelector('.detail-panel').textContent, /Completed track/);
        const completion = get(downloads.lastCompletedDownload);
        assert.equal(completion.item_id, 'a-queue-uuid');
        assert.equal(completion.source, 'qobuz');
        assert.equal(completion.source_album_id, '123');
        assert.equal(completion.album_db_id, 7);
      }
      if (scenario.startsWith('unknown')) assert.equal(reconciliations, 1);
    } finally { await unmount(component); document.body.replaceChildren(); }
  });
}

test('completion cannot be undone by an older queue GET or late progress, and closed detail rejects its pending refresh', async (t) => {
  const time = clock(t);
  const { component } = await start();
  downloads.queue.set([item]);
  const oldQueue = deferred();
  api.downloads.getQueue = () => oldQueue.promise;
  downloads.loadQueue(); time.advance(300); await settle();
  const detail = deferred();
  let detailGets = 0;
  api.library.getAlbum = () => { detailGets++; return detail.promise; };
  try {
    emit('download_complete', { item_id: item.id, title: item.title, artist: item.artist });
    await settle();
    assert.equal(detailGets, 1);
    oldQueue.resolve(snapshot([item])); await settle();
    emit('download_progress', { item_id: item.id, status: 'downloading', speed: 9,
      track_statuses: [{ num: 1, name: 'Late', status: 'downloading' }] });
    await settle();
    assert.equal(get(downloads.queue)[0].status, 'complete');
    assert.equal(get(downloads.activeCount), 0);
    assert.equal(get(downloads.liveTrackStatuses)['123'], undefined);
    document.querySelector('.detail-close').click(); await settle();
    detail.resolve({ ...album, title: 'Stale completion detail' }); await settle();
    assert.equal(get(library.selectedAlbum), null);
  } finally { await unmount(component); document.body.replaceChildren(); }
});

test('closing while an unknown completion is being reconciled does not fetch detail later', async (t) => {
  clock(t);
  const { component, requests } = await start();
  downloads.queue.set([]);
  const reconciliation = deferred();
  api.downloads.getQueue = () => reconciliation.promise;
  try {
    emit('download_complete', { item_id: item.id, title: item.title, artist: item.artist });
    await settle();
    document.querySelector('.detail-close').click(); await settle();
    reconciliation.resolve(snapshot([{ ...item, status: 'complete' }])); await settle();
    assert.equal(requests.length, 1, 'only the initial detail fetch occurred');
    assert.equal(get(library.selectedAlbum), null);
  } finally { await unmount(component); document.body.replaceChildren(); }
});
