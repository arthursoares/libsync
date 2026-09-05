// @ts-nocheck
import assert from 'node:assert/strict';
import test from 'node:test';
import { mount, unmount, settle, button } from './dom.js';

const { default: Settings } = await import('../src/routes/settings/+page.svelte');
const redirect = 'https://login.tidal.com/android/login/auth?code=test-code';
const busyMessage = 'Downloads are active. Wait for them to finish, then try again.';
const input = () => document.querySelector('input[placeholder="https://login.tidal.com/android/login/auth?code=..."]');
async function flush() { await settle(); await settle(); }

for (const status of [409, 400]) {
  test(`Settings PKCE completion HTTP ${status} ${status === 409 ? 'retains the same authorization for explicit retry' : 'keeps the existing terminal error behavior'}`, async (t) => {
    let starts = 0;
    const completions = [];
    t.mock.method(window, 'open', () => null);
    t.mock.method(globalThis, 'fetch', async (url, options) => {
      if (url === '/api/config') return Response.json({});
      if (url === '/api/auth/status') return Response.json([]);
      if (url === '/api/auth/tidal/pkce-start') {
        starts++;
        assert.equal(options.method, 'POST');
        return Response.json({ handle: 'same-handle', auth_url: 'https://login.tidal.com/authorize',
          redirect_uri_prefix: 'https://login.tidal.com/android/login/auth' });
      }
      if (url === '/api/auth/tidal/pkce-complete') {
        assert.equal(options.method, 'POST');
        completions.push(JSON.parse(options.body));
        return completions.length === 1
          ? Response.json({ detail: status === 409 ? busyMessage : 'Authorization code expired' }, { status })
          : Response.json({ status: 'authorized' });
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const component = mount(Settings, { target: document.body });
    try {
      await flush();
      button('▸ Connect Tidal (HiRes)').click(); await flush();
      input().value = redirect;
      input().dispatchEvent(new Event('input', { bubbles: true })); await flush();
      button('Submit').click(); await flush();
      assert.equal(starts, 1);
      assert.equal(completions.length, 1, 'no automatic completion retries');
      if (status === 409) {
        assert.match(document.body.textContent, /Downloads are active/);
        assert.ok(input(), 'busy response must retain the redirect input');
        assert.equal(input().value, redirect);
        assert.equal(button('Submit').disabled, false);
        assert.equal([...document.querySelectorAll('button')].some((node) => node.textContent.includes('Connect Tidal (HiRes)')), false);
        await flush();
        assert.equal(completions.length, 1, 'retry remains user initiated');
        button('Submit').click(); await flush();
        assert.deepEqual(completions, [
          { handle: 'same-handle', redirect_url: redirect },
          { handle: 'same-handle', redirect_url: redirect },
        ]);
        assert.equal(starts, 1, 'retry must not request a new PKCE handle');
        assert.match(document.body.textContent, /Connected ↻/);
        assert.doesNotMatch(document.body.textContent, /Downloads are active/);
        assert.equal(input(), null);
      } else {
        assert.match(document.body.textContent, /Authorization code expired/);
        assert.equal(input(), null);
        assert.ok(button('▸ Connect Tidal (HiRes)'));
      }
    } finally {
      await unmount(component);
      document.body.replaceChildren();
    }
  });
}
