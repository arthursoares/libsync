// @ts-nocheck
import assert from 'node:assert/strict';
import test from 'node:test';
import { mount, unmount, settle, deferred, button } from './dom.js';

const { default: Settings } = await import('../src/routes/settings/+page.svelte');
const { api } = await import('../src/lib/api/client.ts');

test('Settings blocks Save through delayed/failed GET and hydrates before successful retry enables it', async () => {
  const first = deferred();
  const retry = deferred();
  const auth = deferred();
  const patches = [];
  let gets = 0;
  api.config.get = () => (++gets === 1 ? first.promise : retry.promise);
  api.config.update = async (data) => { patches.push(data); };
  api.auth.status = () => auth.promise;
  const component = mount(Settings, { target: document.body });
  try {
    await settle();
    const save = button('Save Changes');
    assert.equal(save.disabled, true);
    save.click();
    await settle();
    assert.deepEqual(patches, []);
    first.reject(new Error('offline'));
    await settle();
    assert.match(document.querySelector('[role="alert"]').textContent, /could not load/i);
    assert.equal(save.disabled, true);
    save.click();
    const retryButton = button('Retry');
    retryButton.click();
    retryButton.click(); // Two clicks before DOM updates must not start competing loads.
    await settle();
    assert.equal(gets, 2);
    assert.equal(save.disabled, true);
    assert.deepEqual(patches, []);
    retry.resolve({ qobuz_user_id: 'saved-user', qobuz_token: 'saved-token',
      downloads_path: '/saved/music', max_connections: 2, embed_artwork: false });
    await settle();
    // An unrelated slow auth check must not leave the form half-hydrated.
    assert.equal(save.disabled, false);
    assert.equal(document.querySelector('input[placeholder="/music"]').value, '/saved/music');
    save.click();
    await settle();
    assert.equal(patches.length, 1);
    assert.equal(patches[0].qobuz_user_id, 'saved-user');
    assert.equal(patches[0].qobuz_token, 'saved-token');
    assert.equal(patches[0].downloads_path, '/saved/music');
    assert.equal(patches[0].max_connections, 2);
    assert.equal(patches[0].embed_artwork, false);
    assert.equal(patches[0].qobuz_quality, 3, 'defaults are applied only to a successful response');
    auth.resolve([]);
    await settle();
  } finally {
    await unmount(component);
    document.body.replaceChildren();
  }
});

test('Settings does not treat an empty response as loaded defaults', async () => {
  api.config.get = async () => null;
  const patches = [];
  api.config.update = async (data) => { patches.push(data); };
  const component = mount(Settings, { target: document.body });
  try {
    await settle();
    assert.equal(button('Save Changes').disabled, true);
    assert.ok(button('Retry'));
    button('Save Changes').click();
    assert.deepEqual(patches, []);
  } finally {
    await unmount(component);
    document.body.replaceChildren();
  }
});
