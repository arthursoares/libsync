// @ts-nocheck
// Controllable timeouts for production polling/debounce paths, without sleeps.
export function clock(t) {
  let now = 0;
  let id = 0;
  const tasks = new Map();
  t.mock.method(globalThis, 'setTimeout', (callback, delay = 0, ...args) => {
    tasks.set(++id, { at: now + delay, callback: () => callback(...args) });
    return id;
  });
  t.mock.method(globalThis, 'clearTimeout', (key) => tasks.delete(key));
  return {
    advance(ms) {
      now += ms;
      for (const [key, task] of [...tasks]) {
        if (task.at <= now && tasks.delete(key)) task.callback();
      }
    },
    get pending() { return tasks.size; },
  };
}
