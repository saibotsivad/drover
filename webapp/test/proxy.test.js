import { strict as assert } from 'node:assert';
import http from 'node:http';
import { test } from 'node:test';
import { Writable } from 'node:stream';
import { createApp } from '../src/app.js';
import { createLogger } from '../src/logger.js';

class CapturingStream extends Writable {
	constructor() {
		super();
		this.chunks = [];
	}
	_write(c, _e, cb) { this.chunks.push(c.toString('utf8')); cb(); }
	get text() { return this.chunks.join(''); }
	get records() {
		return this.text.split('\n').filter(Boolean).map((l) => JSON.parse(l));
	}
}

function startFakeOrchestrator(handler) {
	return new Promise((resolve) => {
		const server = http.createServer((req, res) => {
			let body = '';
			req.on('data', (c) => { body += c; });
			req.on('end', () => handler(req, res, body));
		});
		server.listen(0, '127.0.0.1', () => {
			const { port } = server.address();
			resolve({
				url: `http://127.0.0.1:${port}`,
				close: () => new Promise((r) => server.close(() => r())),
			});
		});
	});
}

function startWebapp({ orchestratorUrl, apiKey = null, logLevel = 'debug' } = {}) {
	const stream = new CapturingStream();
	const errorStream = new CapturingStream();
	const logger = createLogger({ level: logLevel, stream, errorStream });
	const config = { orchestratorUrl, apiKey, port: 0, logLevel };
	const app = createApp({ config, logger });
	return new Promise((resolve) => {
		const server = app.listen(0, '127.0.0.1', () => {
			const { port } = server.address();
			resolve({
				url: `http://127.0.0.1:${port}`,
				stream,
				errorStream,
				close: () => new Promise((r) => server.close(() => r())),
			});
		});
	});
}

test('proxy strips /api/orchestrator prefix when forwarding', async () => {
	const seen = [];
	const orch = await startFakeOrchestrator((req, res) => {
		seen.push({ url: req.url, method: req.method });
		res.writeHead(200, { 'Content-Type': 'application/json' });
		res.end(JSON.stringify({ ok: true, path: req.url }));
	});
	const app = await startWebapp({ orchestratorUrl: orch.url });
	try {
		const res = await fetch(`${app.url}/api/orchestrator/containers`);
		assert.equal(res.status, 200);
		const body = await res.json();
		assert.equal(body.path, '/containers');
		assert.equal(seen.length, 1);
		assert.equal(seen[0].url, '/containers');
	} finally {
		await app.close();
		await orch.close();
	}
});

test('proxy strips prefix on nested paths and preserves query string', async () => {
	const seen = [];
	const orch = await startFakeOrchestrator((req, res) => {
		seen.push(req.url);
		res.writeHead(200);
		res.end();
	});
	const app = await startWebapp({ orchestratorUrl: orch.url });
	try {
		await fetch(`${app.url}/api/orchestrator/containers/abc123/logs?tail=50`);
		assert.equal(seen[0], '/containers/abc123/logs?tail=50');
	} finally {
		await app.close();
		await orch.close();
	}
});

test('proxy injects Authorization: Bearer when DROVER_API_KEY is set', async () => {
	const seen = [];
	const orch = await startFakeOrchestrator((req, res) => {
		seen.push(req.headers);
		res.writeHead(204);
		res.end();
	});
	const app = await startWebapp({ orchestratorUrl: orch.url, apiKey: 'super-secret-token-value' });
	try {
		const res = await fetch(`${app.url}/api/orchestrator/containers`);
		assert.equal(res.status, 204);
		assert.equal(seen[0].authorization, 'Bearer super-secret-token-value');
	} finally {
		await app.close();
		await orch.close();
	}
});

test('proxy does not add Authorization header when key is unset', async () => {
	const seen = [];
	const orch = await startFakeOrchestrator((req, res) => {
		seen.push(req.headers);
		res.writeHead(204);
		res.end();
	});
	const app = await startWebapp({ orchestratorUrl: orch.url, apiKey: null });
	try {
		await fetch(`${app.url}/api/orchestrator/containers`);
		assert.equal(seen[0].authorization, undefined);
	} finally {
		await app.close();
		await orch.close();
	}
});

test('proxy overwrites client-supplied Authorization header with the configured key', async () => {
	const seen = [];
	const orch = await startFakeOrchestrator((req, res) => {
		seen.push(req.headers);
		res.writeHead(204);
		res.end();
	});
	const app = await startWebapp({ orchestratorUrl: orch.url, apiKey: 'real-key' });
	try {
		await fetch(`${app.url}/api/orchestrator/containers`, {
			headers: { Authorization: 'Bearer client-supplied-fake-key' },
		});
		assert.equal(seen[0].authorization, 'Bearer real-key');
	} finally {
		await app.close();
		await orch.close();
	}
});

test('proxy forwards request body and the orchestrator response', async () => {
	const orch = await startFakeOrchestrator((req, res, body) => {
		const parsed = JSON.parse(body);
		res.writeHead(201, { 'Content-Type': 'application/json' });
		res.end(JSON.stringify({ echoed: parsed }));
	});
	const app = await startWebapp({ orchestratorUrl: orch.url });
	try {
		const res = await fetch(`${app.url}/api/orchestrator/containers`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ image: 'example' }),
		});
		assert.equal(res.status, 201);
		const body = await res.json();
		assert.deepEqual(body, { echoed: { image: 'example' } });
	} finally {
		await app.close();
		await orch.close();
	}
});

test('proxy returns 502 when the orchestrator is unreachable', async () => {
	const app = await startWebapp({ orchestratorUrl: 'http://127.0.0.1:1' });
	try {
		const res = await fetch(`${app.url}/api/orchestrator/containers`);
		assert.equal(res.status, 502);
		const body = await res.json();
		assert.equal(body.error, 'orchestrator_unreachable');
	} finally {
		await app.close();
	}
});

test('redaction holds for proxied paths: no Authorization or body in logs', async () => {
	const orch = await startFakeOrchestrator((_req, res) => {
		res.writeHead(200);
		res.end();
	});
	const app = await startWebapp({ orchestratorUrl: orch.url, apiKey: 'super-secret-token-value' });
	try {
		await fetch(`${app.url}/api/orchestrator/containers`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ env: { DROVER_AUTO_GIT_TOKEN: 'ghp_supersecret' } }),
		});
		const text = app.stream.text + app.errorStream.text;
		assert.equal(text.includes('super-secret-token-value'), false, 'auth token leaked');
		assert.equal(text.includes('Bearer'), false, 'auth scheme leaked');
		assert.equal(text.includes('ghp_supersecret'), false, 'request body secret leaked');
		assert.equal(text.includes('DROVER_AUTO_GIT_TOKEN'), false, 'request body field leaked');
		const record = app.stream.records.find((r) => r.msg === 'request');
		assert.ok(record, 'expected a request log line for the proxied request');
		assert.equal(record.method, 'POST');
		assert.equal(record.path, '/api/orchestrator/containers');
		assert.equal(record.status, 200);
	} finally {
		await app.close();
		await orch.close();
	}
});

test('proxy logs proxy_error when orchestrator is unreachable', async () => {
	const app = await startWebapp({ orchestratorUrl: 'http://127.0.0.1:1' });
	try {
		await fetch(`${app.url}/api/orchestrator/containers`);
		const errorRecord = app.errorStream.records.find((r) => r.msg === 'proxy_error');
		assert.ok(errorRecord, 'expected proxy_error log entry');
		assert.equal(typeof errorRecord.error, 'string');
	} finally {
		await app.close();
	}
});
