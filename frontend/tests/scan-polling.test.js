// @ts-nocheck
import assert from 'node:assert/strict';
import test from 'node:test';
import { mount, unmount, settle, deferred, button } from './dom.js';
import { clock } from './clock.js';
const { default: Settings } = await import('../src/routes/settings/+page.svelte');
const { toasts } = await import('../src/lib/stores/toast.ts');
const { get } = await import('svelte/store');
const json = (data, status = 200) => Response.json(data, { status });
const done = (scanned) => json({ status: 'complete', scanned, total: scanned });
const dialog = () => document.querySelector('[role="dialog"]');
async function flush() { await settle(); await settle(); }

async function setup(t, start, status) {
  toasts.set([]);
  const time = clock(t);
  const starts = [], polls = [];
  t.mock.method(globalThis, 'fetch', (url, options) => {
    if (url === '/api/config') return Promise.resolve(json({}));
    if (url === '/api/auth/status') return Promise.resolve(json([]));
    if (url === '/api/library/scan-fuzzy') { starts.push(options); return start(starts.length, options); }
    if (url.startsWith('/api/library/scan-fuzzy/')) {
      polls.push({ url, options });
      return status(url, polls.length, options);
    }
    throw new Error(`Unexpected request: ${url}`);
  });
  const component = mount(Settings, { target: document.body });
  await flush();
  const scan = button('▸ Scan Folder');
  assert.ok(scan);
  return { time, starts, polls, component, scan };
}

test('scan uses POST and polls sequentially, waiting 500ms after each running response', async (t) => {
  const slow = deferred();
  const state = await setup(t, async () => json({ job_id: 'one' }), async (_, count) => count === 1 ? slow.promise : done(8));
  try {
    state.scan.click(); await flush();
    assert.equal(state.starts[0].method, 'POST');
    state.time.advance(500); await flush();
    assert.equal(state.polls.length, 1);
    state.time.advance(5000); await flush();
    assert.equal(state.polls.length, 1, 'no overlapping status calls');
    slow.resolve(json({ status: 'running', scanned: 5, total: 8 })); await flush();
    assert.match(dialog().textContent, /5 \/ 8/);
    state.time.advance(499); await flush(); assert.equal(state.polls.length, 1);
    state.time.advance(1); await flush(); assert.equal(state.polls.length, 2);
    assert.match(dialog().textContent, /Scanned 8 folders/);
    state.time.advance(5000); await flush(); assert.equal(state.polls.length, 2);
  } finally { await unmount(state.component); document.body.replaceChildren(); }
});

test('a missing scan stops polling, shows a restart action, and starts a fresh job', async (t) => {
  const state = await setup(t, async (count) => json({ job_id: `job-${count}` }),
    async (url) => url.endsWith('job-1') ? json({ error: 'Job not found' }, 404) : done(9));
  try {
    state.scan.click(); await flush(); state.time.advance(500); await flush();
    assert.match(dialog().querySelector('[role="alert"]').textContent, /no longer available/i);
    assert.equal(get(toasts).length, 1, 'one error notification, not a polling storm');
    state.time.advance(5000); await flush(); assert.equal(state.polls.length, 1);
    assert.equal(get(toasts).length, 0);
    button('Start new scan', dialog()).click(); await flush();
    state.time.advance(500); await flush();
    assert.equal(state.starts.length, 2);
    assert.equal(state.polls[1].url, '/api/library/scan-fuzzy/job-2');
    assert.match(dialog().textContent, /Scanned 9 folders/);
  } finally { await unmount(state.component); document.body.replaceChildren(); }
});

test('network failure stops polling and explicit retry checks the same job without another POST', async (t) => {
  const state = await setup(t, async () => json({ job_id: 'same-job' }), async (_, count) => {
    if (count === 1) throw new TypeError('Network unavailable');
    return done(4);
  });
  try {
    state.scan.click(); await flush(); state.time.advance(500); await flush();
    assert.match(dialog().querySelector('[role="alert"]').textContent, /could not check scan progress/i);
    state.time.advance(5000); await flush(); assert.equal(state.polls.length, 1);
    button('Retry status', dialog()).click(); await flush(); state.time.advance(500); await flush();
    assert.equal(state.starts.length, 1);
    assert.equal(state.polls[1].url, '/api/library/scan-fuzzy/same-job');
    assert.match(dialog().textContent, /Scanned 4 folders/);
  } finally { await unmount(state.component); document.body.replaceChildren(); }
});

test('close before start resolves aborts the request and cannot start polling after reopening', async (t) => {
  const old = deferred();
  const state = await setup(t, async (count) => count === 1 ? old.promise : json({ job_id: 'new-job' }), async () => done(7));
  try {
    state.scan.click(); await flush();
    dialog().querySelector('[aria-label="Close"]').click(); await flush();
    assert.equal(state.starts[0].signal?.aborted, true);
    assert.equal(dialog(), null);
    state.scan.click(); await flush();
    old.resolve(json({ job_id: 'old-job' })); await flush();
    state.time.advance(500); await flush();
    assert.deepEqual(state.polls.map((p) => p.url), ['/api/library/scan-fuzzy/new-job']);
    assert.match(dialog().textContent, /Scanned 7 folders/);
  } finally { await unmount(state.component); document.body.replaceChildren(); }
});

test('close aborts a pending status request and its late response cannot replace a reopened scan', async (t) => {
  const old = deferred();
  const state = await setup(t, async (count) => json({ job_id: `job-${count}` }),
    async (url) => url.endsWith('job-1') ? old.promise : done(3));
  try {
    state.scan.click(); await flush(); state.time.advance(500); await flush();
    dialog().querySelector('[aria-label="Close"]').click(); await flush();
    assert.equal(state.polls[0].options.signal?.aborted, true);
    state.scan.click(); await flush(); state.time.advance(500); await flush();
    old.resolve(done(999)); await flush();
    assert.match(dialog().textContent, /Scanned 3 folders/);
    assert.doesNotMatch(dialog().textContent, /999/);
    state.time.advance(5000); await flush(); assert.equal(state.polls.length, 2);
  } finally { await unmount(state.component); document.body.replaceChildren(); }
});

test('unmount before the start response prevents subsequent polling', async (t) => {
  const start = deferred();
  const state = await setup(t, () => start.promise, async () => done(1));
  state.scan.click(); await flush();
  await unmount(state.component); document.body.replaceChildren();
  start.resolve(json({ job_id: 'late-job' })); await flush();
  state.time.advance(5000); await flush();
  assert.equal(state.polls.length, 0);
  assert.equal(state.starts[0].signal?.aborted, true);
});

test('unmount during a status request ignores a late 404 without notifying or polling again', async (t) => {
  const status = deferred();
  const state = await setup(t, async () => json({ job_id: 'one' }), () => status.promise);
  state.scan.click(); await flush(); state.time.advance(500); await flush();
  await unmount(state.component); document.body.replaceChildren();
  status.resolve(json({ error: 'Job not found' }, 404)); await flush();
  state.time.advance(5000); await flush();
  assert.equal(state.polls.length, 1);
  assert.equal(state.polls[0].options.signal.aborted, true);
  assert.equal(get(toasts).length, 0);
});

test('a terminal backend scan error stops polling and offers a fresh scan', async (t) => {
  const state = await setup(t, async () => json({ job_id: 'one' }),
    async () => json({ status: 'error', error: 'Scan failed — see server logs' }));
  try {
    state.scan.click(); await flush(); state.time.advance(500); await flush();
    assert.match(dialog().querySelector('[role="alert"]').textContent, /see server logs/);
    assert.ok(button('Start new scan', dialog()));
    state.time.advance(5000); await flush(); assert.equal(state.polls.length, 1);
  } finally { await unmount(state.component); document.body.replaceChildren(); }
});
