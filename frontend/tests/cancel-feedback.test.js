// @ts-nocheck
import assert from 'node:assert/strict';
import test from 'node:test';
import { mount, unmount, settle, deferred, button } from './dom.js';
import { clock } from './clock.js';
const { default: Host } = await import('./fixtures/DownloadsHost.svelte');
const { api } = await import('../src/lib/api/client.ts');
const downloads = await import('../src/lib/stores/downloads.ts');
const { get } = await import('svelte/store');
globalThis.localStorage = window.localStorage;
let socket;
globalThis.WebSocket = class { constructor() { socket = this; } };
const initial = [
  { id: 'one', source: 'qobuz', source_album_id: '123', title: 'One', artist: 'Artist', status: 'downloading', speed: 2 },
  { id: 'two', source: 'tidal', source_album_id: '456', title: 'Two', artist: 'Artist', status: 'pending', speed: 0 },
];
const snapshot = (items) => ({ items, active_count: items.filter((i) => i.status === 'downloading').length,
  total_speed: items.filter((i) => i.status === 'downloading').reduce((sum, i) => sum + i.speed, 0) });

for (const bulk of [false, true]) {
  for (const fail of [false, true]) {
    test(`${bulk ? 'Cancel All' : 'individual cancel'} ${fail ? 'failure keeps' : 'success reconciles'} queue and sidebar without WS`, async (t) => {
      const time = clock(t);
      let items = initial.map((item) => ({ ...item }));
      let gets = 0;
      api.downloads.getQueue = async () => { gets++; return snapshot(items); };
      api.auth.status = async () => [];
      const cancel = async (id) => {
        if (fail) throw new Error('Cancellation unavailable');
        items = items.map((item) => !id || item.id === id ? { ...item, status: 'cancelled', speed: 0 } : item);
      };
      api.downloads.cancel = cancel;
      api.downloads.cancelAll = () => cancel();
      const component = mount(Host, { target: document.body });
      try {
        await settle(); time.advance(300); await settle();
        assert.equal(document.querySelector('.nav-badge').textContent, '1');
        if (bulk) { button('✕ Cancel All').click(); await settle(); button('Confirm Cancel All').click(); }
        else document.querySelector('[title="Cancel"]').click();
        await settle();
        if (fail) {
          assert.equal(get(downloads.queue)[0].status, 'downloading');
          assert.equal(document.querySelector('.nav-badge').textContent, '1');
          assert.match(document.body.textContent, /Cancellation unavailable/);
        } else {
          assert.ok(gets >= 2, 'successful mutation must await canonical reconciliation');
          assert.equal(document.querySelector('.nav-badge'), null);
          assert.equal(get(downloads.activeCount), 0);
          assert.equal(get(downloads.totalSpeed), 0);
          assert.equal(document.querySelectorAll('[title="Cancel"]').length, bulk ? 0 : 1);
          // SDK cancellation is soft; late progress must not restore a cancelled row.
          socket.onmessage({ data: JSON.stringify({ type: 'download_progress', data: { item_id: 'one', status: 'downloading', speed: 9 } }) });
          await settle();
          assert.equal(get(downloads.queue)[0].status, 'cancelled');
          assert.equal(document.querySelector('.nav-badge'), null);
        }
      } finally { await unmount(component); document.body.replaceChildren(); }
    });
  }
}

test('a pre-cancellation queue response cannot overwrite the successful reconciliation', async (t) => {
  const time = clock(t);
  const old = deferred();
  api.downloads.getQueue = () => old.promise;
  downloads.loadQueue(); time.advance(300); await settle();
  api.downloads.cancel = async () => {};
  api.downloads.getQueue = async () => snapshot([{ ...initial[0], status: 'cancelled', speed: 0 }]);
  assert.equal(typeof downloads.cancelDownload, 'function');
  await downloads.cancelDownload('one');
  old.resolve(snapshot(initial)); await settle();
  assert.equal(get(downloads.queue)[0].status, 'cancelled');
  assert.equal(get(downloads.activeCount), 0);
});
