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
