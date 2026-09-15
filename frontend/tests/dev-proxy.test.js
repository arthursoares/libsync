// @ts-nocheck
import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { loadConfigFromFile } from 'vite';

test('development proxies same-origin API HTTP and WebSocket traffic to the backend', async () => {
	const root = fileURLToPath(new URL('../', import.meta.url));
	const { config } = await loadConfigFromFile(
		{ command: 'serve', mode: 'development' }, `${root}vite.config.ts`, root
	);
	assert.equal(config.server?.proxy?.['/api']?.target, 'http://localhost:8080');
	assert.equal(config.server.proxy['/api'].ws, true);
	assert.equal(config.server.proxy['/api'].rewrite, undefined, 'preserve the /api path');
	assert.equal(config.base, undefined, 'keep production assets and clients same-origin');
});
