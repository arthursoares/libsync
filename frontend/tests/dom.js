// @ts-nocheck
import { register } from 'node:module';
import { JSDOM } from 'jsdom';

register('./component-loader.js', import.meta.url);
const dom = new JSDOM('<!doctype html><html><head></head><body></body></html>', {
  url: 'http://localhost/settings', pretendToBeVisual: true,
});
for (const key of ['window', 'document', 'Node', 'Element', 'HTMLElement', 'HTMLInputElement',
  'HTMLSelectElement', 'HTMLButtonElement', 'Text', 'Comment', 'Event', 'MouseEvent',
  'CustomEvent', 'MutationObserver', 'getComputedStyle']) {
  globalThis[key] = dom.window[key];
}
Object.defineProperty(globalThis, 'navigator', {
  configurable: true,
  value: dom.window.navigator,
});
globalThis.requestAnimationFrame = dom.window.requestAnimationFrame.bind(dom.window);
globalThis.cancelAnimationFrame = dom.window.cancelAnimationFrame.bind(dom.window);
// Never let a missing mock make a real request.
globalThis.fetch = async (url) => { throw new Error(`Unmocked fetch: ${url}`); };

export const { mount, unmount, flushSync, tick } = await import('svelte');
export function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
export async function settle() {
  for (let i = 0; i < 5; i++) { await Promise.resolve(); flushSync(); }
}
export function button(text, root = document) {
  const found = [...root.querySelectorAll('button')].find((node) => node.textContent.trim() === text);
  if (!found) throw new Error(`Button not found: ${text}`);
  return found;
}
