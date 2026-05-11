import { strict as assert } from 'node:assert';
import http from 'node:http';
import { test } from 'node:test';
import {
	OrchestratorHttpError,
	OrchestratorUnreachableError,
	createOrchestratorClient,
} from '../src/orchestrator.js';

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

test('getJson returns the parsed JSON response', async () => {
	const orch = await startFakeOrchestrator((_req, res) => {
		res.writeHead(200, { 'Content-Type': 'application/json' });
		res.end(JSON.stringify({ containers: ['a', 'b'] }));
	});
	try {
		const client = createOrchestratorClient({ baseUrl: orch.url });
		const data = await client.getJson('/containers');
		assert.deepEqual(data, { containers: ['a', 'b'] });
	} finally {
		await orch.close();
	}
});

test('getJson sends Authorization header when apiKey is set', async () => {
	let receivedAuth = null;
	const orch = await startFakeOrchestrator((req, res) => {
		receivedAuth = req.headers.authorization;
		res.writeHead(200, { 'Content-Type': 'application/json' });
		res.end('{}');
	});
	try {
		const client = createOrchestratorClient({ baseUrl: orch.url, apiKey: 'k1' });
		await client.getJson('/containers');
		assert.equal(receivedAuth, 'Bearer k1');
	} finally {
		await orch.close();
	}
});

test('getJson does not send Authorization header when apiKey is unset', async () => {
	let receivedAuth = 'sentinel';
	const orch = await startFakeOrchestrator((req, res) => {
		receivedAuth = req.headers.authorization;
		res.writeHead(200, { 'Content-Type': 'application/json' });
		res.end('{}');
	});
	try {
		const client = createOrchestratorClient({ baseUrl: orch.url });
		await client.getJson('/containers');
		assert.equal(receivedAuth, undefined);
	} finally {
		await orch.close();
	}
});

test('postJson sends body as JSON and returns parsed response', async () => {
	let receivedBody = null;
	let receivedMethod = null;
	let receivedContentType = null;
	const orch = await startFakeOrchestrator((req, res, body) => {
		receivedBody = body;
		receivedMethod = req.method;
		receivedContentType = req.headers['content-type'];
		res.writeHead(201, { 'Content-Type': 'application/json' });
		res.end(JSON.stringify({ id: 'c1' }));
	});
	try {
		const client = createOrchestratorClient({ baseUrl: orch.url });
		const data = await client.postJson('/containers', { image: 'x' });
		assert.equal(receivedMethod, 'POST');
		assert.equal(receivedContentType, 'application/json');
		assert.deepEqual(JSON.parse(receivedBody), { image: 'x' });
		assert.deepEqual(data, { id: 'c1' });
	} finally {
		await orch.close();
	}
});

test('del returns null on 204 No Content', async () => {
	const orch = await startFakeOrchestrator((req, res) => {
		assert.equal(req.method, 'DELETE');
		res.writeHead(204);
		res.end();
	});
	try {
		const client = createOrchestratorClient({ baseUrl: orch.url });
		const result = await client.del('/containers/abc');
		assert.equal(result, null);
	} finally {
		await orch.close();
	}
});

test('non-2xx response throws OrchestratorHttpError with status and parsed body', async () => {
	const orch = await startFakeOrchestrator((_req, res) => {
		res.writeHead(404, { 'Content-Type': 'application/json' });
		res.end(JSON.stringify({ error: 'not_found' }));
	});
	try {
		const client = createOrchestratorClient({ baseUrl: orch.url });
		await assert.rejects(
			() => client.getJson('/containers/missing'),
			(err) => {
				assert.ok(err instanceof OrchestratorHttpError);
				assert.equal(err.status, 404);
				assert.deepEqual(err.body, { error: 'not_found' });
				return true;
			},
		);
	} finally {
		await orch.close();
	}
});

test('non-2xx with non-JSON body returns text body', async () => {
	const orch = await startFakeOrchestrator((_req, res) => {
		res.writeHead(500, { 'Content-Type': 'text/plain' });
		res.end('server exploded');
	});
	try {
		const client = createOrchestratorClient({ baseUrl: orch.url });
		await assert.rejects(
			() => client.getJson('/containers'),
			(err) => {
				assert.ok(err instanceof OrchestratorHttpError);
				assert.equal(err.status, 500);
				assert.equal(err.body, 'server exploded');
				return true;
			},
		);
	} finally {
		await orch.close();
	}
});

test('connection failure throws OrchestratorUnreachableError', async () => {
	const client = createOrchestratorClient({ baseUrl: 'http://127.0.0.1:1' });
	await assert.rejects(
		() => client.getJson('/containers'),
		(err) => {
			assert.ok(err instanceof OrchestratorUnreachableError);
			assert.ok(err.cause);
			return true;
		},
	);
});

test('createOrchestratorClient rejects empty baseUrl', () => {
	assert.throws(() => createOrchestratorClient({ baseUrl: '' }));
	assert.throws(() => createOrchestratorClient({}));
});

test('trailing slash on baseUrl is normalized', async () => {
	let receivedPath = null;
	const orch = await startFakeOrchestrator((req, res) => {
		receivedPath = req.url;
		res.writeHead(200, { 'Content-Type': 'application/json' });
		res.end('{}');
	});
	try {
		const client = createOrchestratorClient({ baseUrl: `${orch.url}/` });
		await client.getJson('/containers');
		assert.equal(receivedPath, '/containers');
	} finally {
		await orch.close();
	}
});
