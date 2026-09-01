// @ts-nocheck
import test from 'node:test';
import assert from 'node:assert/strict';

import { shouldReloadQueueForUnknownItem } from '../src/lib/stores/downloads-logic.js';

test('shouldReloadQueueForUnknownItem is false when the item is already in the queue', () => {
  const items = [{ id: 'abc' }, { id: 'def' }];
  assert.equal(shouldReloadQueueForUnknownItem(items, 'abc'), false);
});

test('shouldReloadQueueForUnknownItem is true when the item is missing from the queue', () => {
  const items = [{ id: 'abc' }];
  assert.equal(shouldReloadQueueForUnknownItem(items, 'never-seen'), true);
});

test('shouldReloadQueueForUnknownItem is true for an empty queue snapshot', () => {
  assert.equal(shouldReloadQueueForUnknownItem([], 'abc'), true);
});

test('shouldReloadQueueForUnknownItem is false when there is no item_id on the event', () => {
  const items = [{ id: 'abc' }];
  assert.equal(shouldReloadQueueForUnknownItem(items, undefined), false);
  assert.equal(shouldReloadQueueForUnknownItem(items, null), false);
});
