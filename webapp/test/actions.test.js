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
		getJson: handlers.getJson ?? (async () => []),
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

function form(data) {
	return new URLSearchParams(data).toString();
}

const FORM_HEADERS = { 'Content-Type': 'application/x-www-form-urlencoded' };

// --- POST /actions/workers -----------------------------------------------

test('POST /actions/workers forwards a parsed payload and redirects on success', async () => {
	let received = null;
	const orchestrator = makeFakeOrchestrator({
		postJson: async (path, body) => {
			assert.equal(path, '/workers');
			received = body;
			return { id: 'c-new', status: 'initializing' };
		},
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/actions/workers`, {
			method: 'POST',
			headers: FORM_HEADERS,
			body: form({
				image: 'python-runner',
				label: 'my-job',
				env: 'FOO=bar\nBAZ=qux\n# this is not a comment, just dropped\n=oops\n  \n',
				timeout_seconds: '600',
				privileged: 'true',
			}),
			redirect: 'manual',
		});
		assert.equal(res.status, 201);
		assert.equal(res.headers.get('hx-redirect'), '/views/workers/c-new');
		assert.deepEqual(received, {
			image: 'python-runner',
			label: 'my-job',
			privileged: true,
			env: { FOO: 'bar', BAZ: 'qux' },
			timeout_seconds: 600,
		});
	} finally {
		await close();
	}
});

test('POST /actions/workers re-renders the form on validation error', async () => {
	const orchestrator = makeFakeOrchestrator({
		getJson: async () => [{ name: 'python-runner', tags: [], size: 0, created: '2026-01-01T00:00:00Z' }],
		postJson: async () => { throw new OrchestratorHttpError(422, { detail: 'image is required' }); },
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/actions/workers`, {
			method: 'POST',
			headers: FORM_HEADERS,
			body: form({ image: '', label: 'x', timeout_seconds: '300' }),
		});
		assert.equal(res.status, 422);
		assert.equal(res.headers.get('hx-redirect'), null);
		const text = await res.text();
		assert.match(text, /Invalid request/);
		assert.match(text, /image is required/);
		assert.match(text, /<form[^>]*hx-post="\/actions\/workers"/);
	} finally {
		await close();
	}
});

test('POST /actions/workers omits the label field when empty', async () => {
	let received = null;
	const orchestrator = makeFakeOrchestrator({
		postJson: async (_path, body) => {
			received = body;
			return { id: 'c-new', status: 'initializing' };
		},
	});
	const { url, close } = await startApp(orchestrator);
	try {
		await fetch(`${url}/actions/workers`, {
			method: 'POST',
			headers: FORM_HEADERS,
			body: form({ image: 'python-runner', label: '', env: '', timeout_seconds: '300' }),
		});
		assert.equal('label' in received, false);
		assert.equal(received.privileged, false);
		assert.deepEqual(received.env, {});
	} finally {
		await close();
	}
});

// --- POST /actions/workers/:id/stop --------------------------------------

test('POST /actions/workers/:id/stop returns the updated row', async () => {
	const orchestrator = makeFakeOrchestrator({
		postJson: async (path) => {
			assert.equal(path, '/workers/c-aaa/stop');
			return {
				id: 'c-aaa',
				image: 'python-runner',
				privileged: false,
				status: 'stopped',
				label: null,
				timeout_seconds: 300,
				created_at: '2026-05-01T12:00:00Z',
				stopped_at: '2026-05-04T01:00:00Z',
				last_seen: null,
				error_code: null,
			};
		},
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/actions/workers/c-aaa/stop`, { method: 'POST' });
		assert.equal(res.status, 200);
		const text = await res.text();
		assert.match(text, /<tr id="worker-c-aaa">/);
		assert.match(text, /status-stopped/);
	} finally {
		await close();
	}
});

test('POST /actions/workers/:id/stop returns an error row on failure', async () => {
	const orchestrator = makeFakeOrchestrator({
		postJson: async () => { throw new OrchestratorHttpError(409, { detail: 'already stopped' }); },
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/actions/workers/c-aaa/stop`, { method: 'POST' });
		assert.equal(res.status, 409);
		const text = await res.text();
		assert.match(text, /<tr id="worker-c-aaa"[^>]*class="error-row"/);
		assert.match(text, /already stopped/);
	} finally {
		await close();
	}
});

// --- POST /actions/workers/:id/resume ------------------------------------

test('POST /actions/workers/:id/resume triggers a resume and redirects via HX-Redirect', async () => {
	let calledPath = null;
	const orchestrator = makeFakeOrchestrator({
		postJson: async (path) => {
			calledPath = path;
			return {
				id: 'c-aaa',
				image: 'python-runner',
				privileged: false,
				status: 'resuming',
				label: null,
				timeout_seconds: 300,
				created_at: '2026-05-01T12:00:00Z',
				stopped_at: null,
				last_seen: null,
				error_code: null,
			};
		},
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/actions/workers/c-aaa/resume`, {
			method: 'POST',
			redirect: 'manual',
		});
		assert.equal(res.status, 200);
		assert.equal(calledPath, '/workers/c-aaa/resume');
		assert.equal(res.headers.get('hx-redirect'), '/views/workers/c-aaa');
		const text = await res.text();
		assert.equal(text, '');
	} finally {
		await close();
	}
});

test('POST /actions/workers/:id/resume renders an error fragment on orchestrator failure', async () => {
	const orchestrator = makeFakeOrchestrator({
		postJson: async () => { throw new OrchestratorHttpError(409, { detail: 'worker is not stopped' }); },
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/actions/workers/c-aaa/resume`, { method: 'POST' });
		assert.equal(res.status, 409);
		assert.equal(res.headers.get('hx-redirect'), null);
		const text = await res.text();
		assert.match(text, /worker is not stopped/);
	} finally {
		await close();
	}
});

// --- DELETE /actions/workers/:id -----------------------------------------

test('DELETE /actions/workers/:id returns an empty body on success', async () => {
	let calledPath = null;
	const orchestrator = makeFakeOrchestrator({
		del: async (path) => {
			calledPath = path;
			return null;
		},
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/actions/workers/c-aaa`, { method: 'DELETE' });
		assert.equal(res.status, 200);
		const text = await res.text();
		assert.equal(text, '');
		assert.equal(calledPath, '/workers/c-aaa');
	} finally {
		await close();
	}
});

test('DELETE /actions/workers/:id returns an error row on orchestrator failure', async () => {
	const orchestrator = makeFakeOrchestrator({
		del: async () => { throw new OrchestratorUnreachableError(new Error('econnrefused')); },
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/actions/workers/c-aaa`, { method: 'DELETE' });
		assert.equal(res.status, 502);
		const text = await res.text();
		assert.match(text, /Orchestrator unreachable/);
	} finally {
		await close();
	}
});
