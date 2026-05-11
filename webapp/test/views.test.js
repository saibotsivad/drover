import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { Writable } from 'node:stream';
import { createApp } from '../src/app.js';
import { createLogger } from '../src/logger.js';
import { OrchestratorHttpError, OrchestratorUnreachableError } from '../src/orchestrator.js';

class Sink extends Writable {
	_write(_c, _e, cb) { cb(); }
}

function makeFakeOrchestrator(handlers = {}) {
	return {
		getJson: handlers.getJson ?? (async () => { throw new Error('getJson not stubbed'); }),
		postJson: handlers.postJson ?? (async () => { throw new Error('postJson not stubbed'); }),
		del: handlers.del ?? (async () => { throw new Error('del not stubbed'); }),
	};
}

function startApp(orchestrator) {
	const logger = createLogger({ level: 'error', stream: new Sink(), errorStream: new Sink() });
	const config = { orchestratorUrl: 'http://127.0.0.1:1', apiKey: null, port: 0, logLevel: 'error' };
	const app = createApp({ config, logger, orchestrator });
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

const SAMPLE_CONTAINERS = [
	{
		id: 'c-aaa',
		image: 'python-runner',
		privileged: false,
		status: 'running',
		label: 'experiment-1',
		timeout_seconds: 300,
		created_at: '2026-05-01T12:00:00Z',
		stopped_at: null,
		last_seen: '2026-05-04T01:00:00Z',
		error_code: null,
	},
	{
		id: 'c-bbb',
		image: 'node-runner',
		privileged: false,
		status: 'destroyed',
		label: null,
		timeout_seconds: 60,
		created_at: '2026-05-02T12:00:00Z',
		stopped_at: '2026-05-02T13:00:00Z',
		last_seen: null,
		error_code: null,
	},
];

const SAMPLE_IMAGES = [
	{
		name: 'python-runner',
		tags: ['latest', '3.12'],
		labels: { 'drover.managed': 'true', 'drover.name': 'python-runner' },
		size: 200_000_000,
		created: '2026-04-01T00:00:00Z',
	},
	{
		name: 'node-runner',
		tags: ['latest'],
		labels: { 'drover.managed': 'true', 'drover.name': 'node-runner' },
		size: 150_000_000,
		created: '2026-04-02T00:00:00Z',
	},
];

// --- /views/containers ----------------------------------------------------

test('GET /views/containers renders the table and hides destroyed rows', async () => {
	const orchestrator = makeFakeOrchestrator({
		getJson: async (path) => {
			assert.equal(path, '/containers');
			return SAMPLE_CONTAINERS;
		},
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/views/containers`);
		assert.equal(res.status, 200);
		const text = await res.text();
		assert.match(text, /<title>Containers — Drover<\/title>/);
		assert.match(text, /id="container-c-aaa"/);
		assert.equal(text.includes('id="container-c-bbb"'), false, 'destroyed container should be filtered out');
		assert.match(text, /experiment-1/);
		assert.match(text, /hx-trigger="every 5s"/);
	} finally {
		await close();
	}
});

test('GET /views/containers returns just rows when HX-Request is set', async () => {
	const orchestrator = makeFakeOrchestrator({ getJson: async () => SAMPLE_CONTAINERS });
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/views/containers`, { headers: { 'HX-Request': 'true' } });
		assert.equal(res.status, 200);
		const text = await res.text();
		assert.equal(text.includes('<title>'), false, 'fragment must not include the layout');
		assert.equal(text.includes('<table'), false, 'fragment must not include the table wrapper');
		assert.match(text, /<tr id="container-c-aaa">/);
	} finally {
		await close();
	}
});

test('GET /views/containers renders an error page when the orchestrator is unreachable', async () => {
	const orchestrator = makeFakeOrchestrator({
		getJson: async () => { throw new OrchestratorUnreachableError(new Error('econnrefused')); },
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/views/containers`);
		assert.equal(res.status, 502);
		const text = await res.text();
		assert.match(text, /Orchestrator unreachable/);
	} finally {
		await close();
	}
});

// --- /views/containers/:id ------------------------------------------------

test('GET /views/containers/:id renders metadata', async () => {
	const orchestrator = makeFakeOrchestrator({
		getJson: async (path) => {
			assert.equal(path, '/containers/c-aaa');
			return SAMPLE_CONTAINERS[0];
		},
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/views/containers/c-aaa`);
		assert.equal(res.status, 200);
		const text = await res.text();
		assert.match(text, /Container <code>c-aaa<\/code>/);
		assert.match(text, /python-runner/);
		assert.match(text, /experiment-1/);
		assert.match(text, /id="container-meta"/);
	} finally {
		await close();
	}
});

test('GET /views/containers/:id renders 404 when orchestrator returns 404', async () => {
	const orchestrator = makeFakeOrchestrator({
		getJson: async () => { throw new OrchestratorHttpError(404, { detail: 'not found' }); },
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/views/containers/missing`);
		assert.equal(res.status, 404);
		const text = await res.text();
		assert.match(text, /Not found/);
	} finally {
		await close();
	}
});

// --- /views/images --------------------------------------------------------

test('GET /views/images renders the image list', async () => {
	const orchestrator = makeFakeOrchestrator({
		getJson: async (path) => {
			assert.equal(path, '/images');
			return SAMPLE_IMAGES;
		},
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/views/images`);
		assert.equal(res.status, 200);
		const text = await res.text();
		assert.match(text, /python-runner/);
		assert.match(text, /node-runner/);
		assert.match(text, /drover\.managed=true/);
		assert.match(text, /drover\.name=python-runner/);
	} finally {
		await close();
	}
});

test('GET /views/images surfaces errors from the orchestrator', async () => {
	const orchestrator = makeFakeOrchestrator({
		getJson: async () => { throw new OrchestratorHttpError(401, null); },
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/views/images`);
		assert.equal(res.status, 401);
		const text = await res.text();
		assert.match(text, /Unauthorized/);
	} finally {
		await close();
	}
});

// --- /views/launch --------------------------------------------------------

test('GET /views/launch renders the form with image options', async () => {
	const orchestrator = makeFakeOrchestrator({ getJson: async () => SAMPLE_IMAGES });
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/views/launch`);
		assert.equal(res.status, 200);
		const text = await res.text();
		assert.match(text, /<form[^>]*hx-post="\/actions\/containers"/);
		assert.match(text, /<option\s+value="python-runner"/);
		assert.match(text, /<option\s+value="node-runner"/);
	} finally {
		await close();
	}
});

test('GET /views/launch falls back to a free-text image input when /images fails', async () => {
	const orchestrator = makeFakeOrchestrator({
		getJson: async () => { throw new OrchestratorUnreachableError(new Error('boom')); },
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/views/launch`);
		assert.equal(res.status, 200);
		const text = await res.text();
		assert.match(text, /Could not load images/);
		assert.match(text, /<input[^>]*name="image"/);
	} finally {
		await close();
	}
});

// --- escape regression ----------------------------------------------------

test('GET /views/containers/:id escapes attacker-controlled fields', async () => {
	const evilContainer = {
		id: '<script>alert(1)</script>',
		image: '"><script>',
		privileged: false,
		status: 'running',
		label: '<img src=x>',
		timeout_seconds: 300,
		created_at: '2026-05-01T12:00:00Z',
		stopped_at: null,
		last_seen: null,
		error_code: null,
	};
	const orchestrator = makeFakeOrchestrator({ getJson: async () => evilContainer });
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/views/containers/anything`);
		const text = await res.text();
		assert.equal(text.includes('<script>alert(1)</script>'), false, 'id leaked unescaped');
		assert.equal(text.includes('<img src=x>'), false, 'label leaked unescaped');
	} finally {
		await close();
	}
});
