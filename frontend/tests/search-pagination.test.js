// @ts-nocheck
import assert from 'node:assert/strict';
import test from 'node:test';
import { mount, unmount, settle, deferred, button } from './dom.js';

const { default: Search } = await import('../src/routes/search/+page.svelte');
const { api } = await import('../src/lib/api/client.ts');
const { currentSource } = await import('../src/lib/stores/library.ts');
const album = (id, source = 'qobuz') => ({ id: 0, source_album_id: id, source,
  title: `Album ${id}`, artist: 'Artist' });

test('mounted Search appends page 2 without reloading page 1 and resets only on a source change', async () => {
  currentSource.set('qobuz');
  const requests = [];
  const page2 = deferred();
  const page3 = deferred();
  api.library.search = async (source, query, params) => {
    requests.push([source, query, params.page]);
    if (source === 'tidal') return { albums: [album('tidal-1', 'tidal')], total: 1 };
    if (params.page === '2') return page2.promise;
    if (params.page === '3') return page3.promise;
    return { albums: [album('one')], total: 3 };
  };
  const component = mount(Search, { target: document.body });
  try {
    await settle();
    const input = document.querySelector('.search-input');
    input.value = 'Radiohead';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    await settle();
    assert.equal(document.querySelectorAll('.album-card').length, 1);
    requests.length = 0;
    button('Load More (2 remaining)').click();
    await settle();
    assert.deepEqual(requests, [['qobuz', 'Radiohead', '2']], 'page mutation must not trigger a fresh search');
    page2.resolve({ albums: [album('two')], total: 3 });
    await settle();
    assert.equal(document.querySelectorAll('.album-card').length, 2);
    assert.match(document.querySelector('.album-grid').textContent, /Album one/);
    assert.match(document.querySelector('.album-grid').textContent, /Album two/);
    button('Load More (1 remaining)').click();
    await settle();
    currentSource.set('tidal');
    await settle();
    assert.deepEqual(requests, [
      ['qobuz', 'Radiohead', '2'], ['qobuz', 'Radiohead', '3'], ['tidal', 'Radiohead', '1'],
    ]);
    page3.resolve({ albums: [album('stale-three')], total: 3 });
    await settle();
    assert.equal(document.querySelectorAll('.album-card').length, 1);
    assert.match(document.querySelector('.album-grid').textContent, /Album tidal-1/);
    assert.doesNotMatch(document.querySelector('.album-grid').textContent, /Album one|Album two|stale-three/);
  } finally {
    await unmount(component);
    document.body.replaceChildren();
  }
});
