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
		getText: handlers.getText ?? (async () => { throw new Error('getText not stubbed'); }),
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
			if (path === '/containers/c-aaa') return SAMPLE_CONTAINERS[0];
			if (path === '/containers/c-aaa/logs/files') return [];
			if (path === '/containers/c-aaa/execs') return [];
			throw new Error(`unexpected getJson: ${path}`);
		},
		getText: async (path) => {
			assert.equal(path, '/containers/c-aaa/logs');
			return 'log line\n';
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
		assert.match(text, /id="container-detail"/);
		assert.match(text, /<select[^>]*class="log-source-select"/);
		assert.match(text, /Live container logs/);
		assert.match(text, /Orchestrator logs/);
		assert.match(text, /<pre id="log-viewer"[^>]*>log line\n<\/pre>/);
		assert.match(text, /Exec Commands/);
		assert.match(text, /No exec commands yet/);
	} finally {
		await close();
	}
});

test('GET /views/containers/:id renders 404 when orchestrator returns 404', async () => {
	const orchestrator = makeFakeOrchestrator({
		getJson: async () => { throw new OrchestratorHttpError(404, { detail: 'not found' }); },
		getText: async () => { throw new OrchestratorHttpError(404, { detail: 'not found' }); },
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

test('GET /views/containers/:id lists log files in the source dropdown', async () => {
	const orchestrator = makeFakeOrchestrator({
		getJson: async (path) => {
			if (path === '/containers/c-aaa') return SAMPLE_CONTAINERS[0];
			if (path === '/containers/c-aaa/logs/files') return ['0.log', '1.log'];
			if (path === '/containers/c-aaa/execs') return [];
			throw new Error(`unexpected getJson: ${path}`);
		},
		getText: async () => '',
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/views/containers/c-aaa`);
		assert.equal(res.status, 200);
		const text = await res.text();
		assert.match(text, /<option\s+value="file:0\.log"[^>]*>0\.log<\/option>/);
		assert.match(text, /<option\s+value="file:1\.log"[^>]*>1\.log<\/option>/);
		assert.equal(text.includes('DROVER_LOG_DIR'), false);
	} finally {
		await close();
	}
});

test('GET /views/containers/:id shows note when log files endpoint returns 409', async () => {
	const orchestrator = makeFakeOrchestrator({
		getJson: async (path) => {
			if (path === '/containers/c-aaa') return SAMPLE_CONTAINERS[0];
			if (path === '/containers/c-aaa/logs/files') throw new OrchestratorHttpError(409, { detail: 'disabled' });
			if (path === '/containers/c-aaa/execs') return [];
			throw new Error(`unexpected getJson: ${path}`);
		},
		getText: async () => 'live logs',
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/views/containers/c-aaa`);
		assert.equal(res.status, 200);
		const text = await res.text();
		assert.match(text, /DROVER_LOG_DIR/);
	} finally {
		await close();
	}
});

test('GET /views/containers/:id?log_source=orchestrator fetches orchestrator logs', async () => {
	let textPath = null;
	const orchestrator = makeFakeOrchestrator({
		getJson: async (path) => {
			if (path === '/containers/c-aaa') return SAMPLE_CONTAINERS[0];
			if (path === '/containers/c-aaa/logs/files') return [];
			if (path === '/containers/c-aaa/execs') return [];
			throw new Error(`unexpected getJson: ${path}`);
		},
		getText: async (path) => {
			textPath = path;
			return 'orchestrator log line\n';
		},
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/views/containers/c-aaa?log_source=orchestrator`);
		assert.equal(res.status, 200);
		assert.equal(textPath, '/containers/c-aaa/logs/orchestrator');
		const text = await res.text();
		assert.match(text, /<option\s+value="orchestrator"\s+selected/);
		assert.match(text, /orchestrator log line/);
	} finally {
		await close();
	}
});

test('GET /views/containers/:id?log_source=file:X fetches the file when listed', async () => {
	let textPath = null;
	const orchestrator = makeFakeOrchestrator({
		getJson: async (path) => {
			if (path === '/containers/c-aaa') return SAMPLE_CONTAINERS[0];
			if (path === '/containers/c-aaa/logs/files') return ['0.log'];
			if (path === '/containers/c-aaa/execs') return [];
			throw new Error(`unexpected getJson: ${path}`);
		},
		getText: async (path) => {
			textPath = path;
			return 'file content\n';
		},
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/views/containers/c-aaa?log_source=file:0.log`);
		assert.equal(res.status, 200);
		assert.equal(textPath, '/containers/c-aaa/logs/files/0.log');
		const text = await res.text();
		assert.match(text, /file content/);
	} finally {
		await close();
	}
});

test('GET /views/containers/:id?log_source=file:missing shows log file not found', async () => {
	let textCalled = false;
	const orchestrator = makeFakeOrchestrator({
		getJson: async (path) => {
			if (path === '/containers/c-aaa') return SAMPLE_CONTAINERS[0];
			if (path === '/containers/c-aaa/logs/files') return ['0.log'];
			if (path === '/containers/c-aaa/execs') return [];
			throw new Error(`unexpected getJson: ${path}`);
		},
		getText: async () => { textCalled = true; return ''; },
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/views/containers/c-aaa?log_source=file:nope.log`);
		assert.equal(res.status, 200);
		assert.equal(textCalled, false, 'should not fetch log content for invalid filename');
		const text = await res.text();
		assert.match(text, /Log file not found/);
	} finally {
		await close();
	}
});

test('GET /views/containers/:id treats live 404 as empty log output', async () => {
	const orchestrator = makeFakeOrchestrator({
		getJson: async (path) => {
			if (path === '/containers/c-aaa') return SAMPLE_CONTAINERS[0];
			if (path === '/containers/c-aaa/logs/files') return [];
			if (path === '/containers/c-aaa/execs') return [];
			throw new Error(`unexpected getJson: ${path}`);
		},
		getText: async () => { throw new OrchestratorHttpError(404, { detail: 'no docker' }); },
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/views/containers/c-aaa`);
		assert.equal(res.status, 200);
		const text = await res.text();
		assert.match(text, /\(no log output\)/);
	} finally {
		await close();
	}
});

test('GET /views/containers/:id?log_source=orchestrator handles 503', async () => {
	const orchestrator = makeFakeOrchestrator({
		getJson: async (path) => {
			if (path === '/containers/c-aaa') return SAMPLE_CONTAINERS[0];
			if (path === '/containers/c-aaa/logs/files') return [];
			if (path === '/containers/c-aaa/execs') return [];
			throw new Error(`unexpected getJson: ${path}`);
		},
		getText: async () => { throw new OrchestratorHttpError(503, { detail: 'cannot detect' }); },
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/views/containers/c-aaa?log_source=orchestrator`);
		assert.equal(res.status, 200);
		const text = await res.text();
		assert.match(text, /Container logging not configured/);
	} finally {
		await close();
	}
});

// --- action bar (Stop / Resume / Destroy) --------------------------------

test('GET /views/containers/:id renders a Resume button when the container is stopped', async () => {
	const stoppedContainer = {
		...SAMPLE_CONTAINERS[0],
		status: 'stopped',
		stopped_at: '2026-05-04T01:00:00Z',
	};
	const orchestrator = makeFakeOrchestrator({
		getJson: async (path) => {
			if (path === '/containers/c-aaa') return stoppedContainer;
			if (path === '/containers/c-aaa/logs/files') return [];
			if (path === '/containers/c-aaa/execs') return [];
			throw new Error(`unexpected getJson: ${path}`);
		},
		getText: async () => '',
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/views/containers/c-aaa`);
		assert.equal(res.status, 200);
		const text = await res.text();
		assert.match(text, /<button[^>]*class="btn btn-primary btn-resume"[^>]*hx-post="\/actions\/containers\/c-aaa\/resume"[^>]*>Resume<\/button>/);
		// Stop button must not be rendered on a stopped container.
		assert.equal(text.includes('btn-stop'), false, 'Stop button must not appear when stopped');
	} finally {
		await close();
	}
});

test('GET /views/containers/:id does not render a Resume button when the container is running', async () => {
	const orchestrator = makeFakeOrchestrator({
		getJson: async (path) => {
			if (path === '/containers/c-aaa') return SAMPLE_CONTAINERS[0];
			if (path === '/containers/c-aaa/logs/files') return [];
			if (path === '/containers/c-aaa/execs') return [];
			throw new Error(`unexpected getJson: ${path}`);
		},
		getText: async () => '',
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/views/containers/c-aaa`);
		assert.equal(res.status, 200);
		const text = await res.text();
		assert.equal(text.includes('btn-resume'), false, 'Resume button must not appear when running');
		assert.match(text, /btn-stop/);
	} finally {
		await close();
	}
});

// --- exec commands panel + exec output view ------------------------------

const SAMPLE_COMMANDS = [
	{
		command_id: 'cmd-001',
		command: 'echo hello',
		status: 'complete',
		exit_code: 0,
		created_at: '2026-05-15T10:00:00Z',
	},
	{
		command_id: 'cmd-002',
		command: 'sleep 5',
		status: 'running',
		exit_code: null,
		created_at: '2026-05-15T10:05:00Z',
	},
];

test('GET /views/containers/:id renders the exec commands table when commands exist', async () => {
	const orchestrator = makeFakeOrchestrator({
		getJson: async (path) => {
			if (path === '/containers/c-aaa') return SAMPLE_CONTAINERS[0];
			if (path === '/containers/c-aaa/logs/files') return [];
			if (path === '/containers/c-aaa/execs') return SAMPLE_COMMANDS;
			throw new Error(`unexpected getJson: ${path}`);
		},
		getText: async () => '',
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/views/containers/c-aaa`);
		assert.equal(res.status, 200);
		const text = await res.text();
		assert.match(text, /id="command-rows"/);
		assert.match(text, /id="command-cmd-001"/);
		assert.match(text, /id="command-cmd-002"/);
		assert.match(text, /echo hello/);
		assert.match(text, /sleep 5/);
		assert.match(text, /href="\/views\/containers\/c-aaa\/execs\/cmd-001"/);
		assert.match(text, /status-complete/);
		assert.match(text, /status-running/);
		assert.equal(text.includes('No exec commands yet'), false);
	} finally {
		await close();
	}
});

test('GET /views/containers/:id swallows 404 from /execs (matches missing-container path)', async () => {
	const orchestrator = makeFakeOrchestrator({
		getJson: async (path) => {
			if (path === '/containers/c-aaa') return SAMPLE_CONTAINERS[0];
			if (path === '/containers/c-aaa/logs/files') return [];
			if (path === '/containers/c-aaa/execs') throw new OrchestratorHttpError(404, { detail: 'gone' });
			throw new Error(`unexpected getJson: ${path}`);
		},
		getText: async () => '',
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/views/containers/c-aaa`);
		assert.equal(res.status, 200);
		const text = await res.text();
		assert.match(text, /No exec commands yet/);
	} finally {
		await close();
	}
});

test('GET /views/containers/:id/execs/:commandId renders the exec output page', async () => {
	const exec = {
		command_id: 'cmd-001',
		command: 'echo hello',
		status: 'complete',
		exit_code: 0,
		messages: [
			{ seq: 1, stream: 'stdout', data: 'hello\n' },
			{ seq: 2, stream: 'stderr', data: 'warn: something\n' },
		],
	};
	const orchestrator = makeFakeOrchestrator({
		getJson: async (path) => {
			if (path === '/containers/c-aaa') return SAMPLE_CONTAINERS[0];
			if (path === '/containers/c-aaa/execs/cmd-001') return exec;
			throw new Error(`unexpected getJson: ${path}`);
		},
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/views/containers/c-aaa/execs/cmd-001`);
		assert.equal(res.status, 200);
		const text = await res.text();
		assert.match(text, /id="exec-detail"/);
		assert.match(text, /id="exec-meta"/);
		assert.match(text, /id="exec-output"/);
		assert.match(text, /Exec: <code>echo hello<\/code>/);
		assert.match(text, /Exit code/);
		assert.match(text, /<span class="output-chunk">hello\n<\/span>/);
		assert.match(text, /<span class="output-chunk output-stderr">warn: something\n<\/span>/);
		assert.match(text, /href="\/views\/containers\/c-aaa"/);
	} finally {
		await close();
	}
});

test('GET /views/containers/:id/execs/:commandId renders empty output state', async () => {
	const exec = {
		command_id: 'cmd-002',
		command: 'sleep 5',
		status: 'running',
		exit_code: null,
		messages: [],
	};
	const orchestrator = makeFakeOrchestrator({
		getJson: async (path) => {
			if (path === '/containers/c-aaa') return SAMPLE_CONTAINERS[0];
			if (path === '/containers/c-aaa/execs/cmd-002') return exec;
			throw new Error(`unexpected getJson: ${path}`);
		},
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/views/containers/c-aaa/execs/cmd-002`);
		assert.equal(res.status, 200);
		const text = await res.text();
		assert.match(text, /\(no output yet\)/);
		assert.equal(text.includes('Exit code'), false, 'exit code row should be hidden when not complete');
	} finally {
		await close();
	}
});

test('GET /views/containers/:id/execs/:commandId returns 404 when the command is missing', async () => {
	const orchestrator = makeFakeOrchestrator({
		getJson: async (path) => {
			if (path === '/containers/c-aaa') return SAMPLE_CONTAINERS[0];
			if (path === '/containers/c-aaa/execs/missing') {
				throw new OrchestratorHttpError(404, { detail: 'not found' });
			}
			throw new Error(`unexpected getJson: ${path}`);
		},
	});
	const { url, close } = await startApp(orchestrator);
	try {
		const res = await fetch(`${url}/views/containers/c-aaa/execs/missing`);
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
	const orchestrator = makeFakeOrchestrator({
		getJson: async (path) => {
			if (path.endsWith('/logs/files')) return [];
			if (path.endsWith('/execs')) return [];
			return evilContainer;
		},
		getText: async () => '',
	});
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
