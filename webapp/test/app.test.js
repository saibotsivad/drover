import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { Writable } from 'node:stream';
import { createApp } from '../src/app.js';
import { createLogger } from '../src/logger.js';

class Sink extends Writable {
	_write(_c, _e, cb) { cb(); }
}

function startApp() {
	const logger = createLogger({ level: 'error', stream: new Sink(), errorStream: new Sink() });
	const config = { orchestratorUrl: 'http://127.0.0.1:1', apiKey: null, port: 0, logLevel: 'error' };
	const app = createApp({ config, logger });
	return new Promise((resolve) => {
		const server = app.listen(0, '127.0.0.1', () => {
			const { port } = server.address();
			resolve({
				server,
				url: `http://127.0.0.1:${port}`,
				close: () => new Promise((r) => server.close(() => r())),
			});
		});
	});
}

test('GET /health returns { healthy: true }', async () => {
	const { url, close } = await startApp();
	try {
		const res = await fetch(`${url}/health`);
		assert.equal(res.status, 200);
		const body = await res.json();
		assert.deepEqual(body, { healthy: true });
	} finally {
		await close();
	}
});

test('GET / serves static index.html', async () => {
	const { url, close } = await startApp();
	try {
		const res = await fetch(`${url}/`);
		assert.equal(res.status, 200);
		const text = await res.text();
		assert.match(text, /<title>Drover<\/title>/);
	} finally {
		await close();
	}
});

test('static assets are served from /public', async () => {
	const { url, close } = await startApp();
	try {
		const res = await fetch(`${url}/css/styles.css`);
		assert.equal(res.status, 200);
		const text = await res.text();
		assert.match(text, /font-family/);
	} finally {
		await close();
	}
});

test('unknown path returns 404', async () => {
	const { url, close } = await startApp();
	try {
		const res = await fetch(`${url}/does/not/exist`);
		assert.equal(res.status, 404);
	} finally {
		await close();
	}
});
