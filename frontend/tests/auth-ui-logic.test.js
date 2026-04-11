// @ts-nocheck
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  getApiErrorDetail,
  getSourceAuth,
  isSourceAuthenticated,
} from '../src/lib/auth-ui-logic.js';

test('getSourceAuth returns the matching source status', () => {
  const statuses = [
    { source: 'qobuz', authenticated: false },
    { source: 'tidal', authenticated: true },
  ];

  assert.deepEqual(getSourceAuth(statuses, 'tidal'), {
    source: 'tidal',
    authenticated: true,
  });
});

test('isSourceAuthenticated follows the selected source', () => {
  const statuses = [
    { source: 'qobuz', authenticated: true },
    { source: 'tidal', authenticated: false },
  ];

  assert.equal(isSourceAuthenticated(statuses, 'qobuz'), true);
  assert.equal(isSourceAuthenticated(statuses, 'tidal'), false);
});

test('getApiErrorDetail prefers detail over fallback', () => {
  assert.equal(
    getApiErrorDetail({ detail: 'bad code' }, 'OAuth failed'),
    'bad code',
  );
});

test('getApiErrorDetail falls back to error, then the provided fallback', () => {
  assert.equal(
    getApiErrorDetail({ error: 'legacy error' }, 'OAuth failed'),
    'legacy error',
  );
  assert.equal(getApiErrorDetail({}, 'OAuth failed'), 'OAuth failed');
});
