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
				url: `http://127.0.0.1:${port}`,
				close: () => new Promise((r) => server.close(() => r())),
			});
		});
	});
}

const STUB_PATHS = [
	{ path: '/views/containers', title: /Containers — Drover/, body: /Container list/ },
	{ path: '/views/images', title: /Images — Drover/, body: /Image list/ },
	{ path: '/views/launch', title: /Launch — Drover/, body: /Launch form/ },
];

for (const { path, title, body } of STUB_PATHS) {
	test(`GET ${path} returns a stub page`, async () => {
		const { url, close } = await startApp();
		try {
			const res = await fetch(`${url}${path}`);
			assert.equal(res.status, 200);
			assert.match(res.headers.get('content-type') || '', /text\/html/);
			const text = await res.text();
			assert.match(text, title);
			assert.match(text, body);
			assert.match(text, /<script src="\/vendor\/htmx\.min\.js"/);
		} finally {
			await close();
		}
	});
}

test('GET /views/containers/:id renders the id and escapes it', async () => {
	const { url, close } = await startApp();
	try {
		const evilId = '<script>alert(1)</script>';
		const res = await fetch(`${url}/views/containers/${encodeURIComponent(evilId)}`);
		assert.equal(res.status, 200);
		const text = await res.text();
		assert.equal(text.includes('<script>alert(1)</script>'), false);
		assert.match(text, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
	} finally {
		await close();
	}
});

test('layout marks the active nav link', async () => {
	const { url, close } = await startApp();
	try {
		const res = await fetch(`${url}/views/images`);
		const text = await res.text();
		assert.match(text, /href="\/views\/images"\s+class="active"/);
		assert.match(text, /href="\/views\/containers"\s+class=""/);
	} finally {
		await close();
	}
});
