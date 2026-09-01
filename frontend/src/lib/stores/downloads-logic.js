/**
 * Pure decision logic extracted from `stores/downloads.ts` so it can be
 * unit tested with `node:test` without a DOM or Svelte runtime.
 */

/**
 * A `download_progress` WebSocket event only updates an item already
 * present in the local queue snapshot (see the `.map()` in
 * `stores/downloads.ts`'s `download_progress` handler). If the event's
 * `item_id` isn't in that snapshot yet — e.g. the enqueue's debounced
 * `loadQueue()` hasn't resolved before the SDK's first progress callback
 * fires — the update is silently dropped and the item never appears.
 *
 * @param {Array<{ id?: unknown }>} items current queue store snapshot
 * @param {unknown} itemId `item_id` from the incoming `download_progress` event
 * @returns {boolean} true when the queue store should be reloaded from the server
 */
export function shouldReloadQueueForUnknownItem(items, itemId) {
  if (itemId === undefined || itemId === null) return false;
  return !items.some((item) => item.id === itemId);
}
