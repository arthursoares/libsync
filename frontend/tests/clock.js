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
  t.mock.method(globalThis, 'setInterval', (callback, delay, ...args) => {
    tasks.set(++id, { at: now + delay, repeat: delay, callback: () => callback(...args) });
    return id;
  });
  t.mock.method(globalThis, 'clearInterval', (key) => tasks.delete(key));
  return {
    advance(ms) {
      const end = now + ms;
      while (true) {
        const due = [...tasks].filter(([, task]) => task.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        const [key, task] = due;
        now = task.at;
        if (task.repeat) task.at += task.repeat;
        else tasks.delete(key);
        task.callback();
      }
      now = end;
    },
    get pending() { return tasks.size; },
  };
}
