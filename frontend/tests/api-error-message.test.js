// @ts-nocheck
import test from 'node:test';
import assert from 'node:assert/strict';

import { getApiErrorMessage } from '../src/lib/api/error-message.js';

test('getApiErrorMessage prefers detail, then error, then message', () => {
  assert.equal(getApiErrorMessage({ detail: 'detail text' }, 'fallback'), 'detail text');
  assert.equal(getApiErrorMessage({ error: 'error text' }, 'fallback'), 'error text');
  assert.equal(getApiErrorMessage({ message: 'message text' }, 'fallback'), 'message text');
});

test('getApiErrorMessage falls back for empty payloads', () => {
  assert.equal(getApiErrorMessage({}, 'fallback'), 'fallback');
  assert.equal(getApiErrorMessage('', 'fallback'), 'fallback');
});
