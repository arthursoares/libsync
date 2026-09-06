// @ts-nocheck
import assert from 'node:assert/strict';
import test from 'node:test';
import { mount, unmount, settle } from './dom.js';

const { default: Host } = await import('./fixtures/SyncDiffHost.svelte');
const album = (id) => ({ id, title: `Album ${id}`, artist: 'Artist' });

test('mounted SyncDiff notifies once on initialization, deselection, and items replacement', async () => {
  const notifications = [];
  const component = mount(Host, { target: document.body,
    props: { initialItems: [album('a'), album('b')], notify: (ids) => notifications.push(ids) } });
  try {
    await settle();
    assert.equal(notifications.length, 1);
    assert.deepEqual(notifications, [['a', 'b']]);
    const rows = () => [...document.querySelectorAll('.diff-item--selectable')];
    assert.deepEqual(rows().map((row) => row.getAttribute('aria-pressed')), ['true', 'true']);
    rows()[0].click();
    await settle();
    assert.deepEqual(notifications, [['a', 'b'], ['b']]);
    assert.deepEqual(rows().map((row) => row.getAttribute('aria-pressed')), ['false', 'true']);
    assert.equal(document.querySelector('[data-selection]').textContent, 'b');
    await settle();
    assert.equal(notifications.length, 2, 'deselection stays settled');
    component.replaceItems([album('c')]);
    await settle();
    assert.deepEqual(notifications, [['a', 'b'], ['b'], ['c']]);
    assert.equal(document.querySelector('[data-notifications]').textContent, '3');
    assert.equal(rows()[0].getAttribute('aria-pressed'), 'true');
  } finally {
    await unmount(component);
    document.body.replaceChildren();
  }
});
