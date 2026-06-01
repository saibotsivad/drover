import { Router } from 'express';
import { OrchestratorHttpError } from '../orchestrator.js';
import { layout } from '../views/layout.js';
import { workerDetailPage } from '../views/partials/worker-detail.js';
import { workerListPage, workerRows } from '../views/partials/workers-list.js';
import { describeOrchestratorError, errorPanel, renderOrchestratorError } from '../views/partials/errors.js';
import { execOutputPage } from '../views/partials/exec-output.js';
import { imagesListPage } from '../views/partials/images-list.js';
import { launchFormPage } from '../views/partials/launch-form.js';
import { html } from '../views/render.js';

const ACTIVE_STATUSES = new Set(['initializing', 'running', 'stopping', 'stopped', 'resuming', 'destroying', 'error']);

function isHtmx(req) {
	return req.get('HX-Request') === 'true';
}

function sendPage(res, page) {
	res.type('html').send(page.toString());
}

function sendFragment(res, fragment, status = 200) {
	res.status(status).type('html').send(fragment.toString());
}

async function fetchCommands(orchestrator, encodedId) {
	try {
		const commands = await orchestrator.getJson(`/workers/${encodedId}/execs`);
		return Array.isArray(commands) ? commands : [];
	} catch (err) {
		if (err instanceof OrchestratorHttpError && err.status === 404) {
			return [];
		}
		throw err;
	}
}

async function fetchImages(orchestrator) {
	try {
		const images = await orchestrator.getJson('/images');
		return Array.isArray(images) ? images : [];
	} catch {
		return [];
	}
}

function imageCapabilities(images, imageName) {
	const info = images.find((img) => img && img.name === imageName);
	const raw = info?.labels?.['drover.capabilities'] ?? '';
	return new Set(String(raw).split(',').map((s) => s.trim()).filter(Boolean));
}

async function fetchLogFiles(orchestrator, encodedId) {
	try {
		const logFiles = await orchestrator.getJson(`/workers/${encodedId}/logs/files`);
		return { logFiles: Array.isArray(logFiles) ? logFiles : [], filesUnavailable: false };
	} catch (err) {
		if (err instanceof OrchestratorHttpError && err.status === 409) {
			return { logFiles: [], filesUnavailable: true };
		}
		throw err;
	}
}

function resolveLogSource(rawLogSource, logFiles) {
	if (typeof rawLogSource !== 'string' || rawLogSource === '' || rawLogSource === 'live') {
		return { kind: 'live', value: 'live' };
	}
	if (rawLogSource === 'orchestrator') {
		return { kind: 'orchestrator', value: 'orchestrator' };
	}
	if (rawLogSource.startsWith('file:')) {
		const encodedFilename = rawLogSource.slice('file:'.length);
		let filename;
		try {
			filename = decodeURIComponent(encodedFilename);
		} catch {
			return { kind: 'invalid-file', value: rawLogSource, filename: encodedFilename };
		}
		if (!logFiles.includes(filename)) {
			return { kind: 'invalid-file', value: rawLogSource, filename };
		}
		return { kind: 'file', value: rawLogSource, filename };
	}
	return { kind: 'live', value: 'live' };
}

async function fetchLogContent(orchestrator, encodedId, rawLogSource, logFiles) {
	const resolved = resolveLogSource(rawLogSource, logFiles);
	if (resolved.kind === 'invalid-file') {
		return { logSource: resolved.value, logContent: null, logUnavailable: false, logError: 'log file not found' };
	}
	let path;
	if (resolved.kind === 'live') path = `/workers/${encodedId}/logs`;
	else if (resolved.kind === 'orchestrator') path = `/workers/${encodedId}/logs/orchestrator`;
	else path = `/workers/${encodedId}/logs/files/${encodeURIComponent(resolved.filename)}`;

	try {
		const text = await orchestrator.getText(path);
		return { logSource: resolved.value, logContent: typeof text === 'string' ? text : '', logUnavailable: false };
	} catch (err) {
		if (err instanceof OrchestratorHttpError) {
			if (err.status === 409 || err.status === 503) {
				return { logSource: resolved.value, logContent: null, logUnavailable: true };
			}
			if (err.status === 404 && resolved.kind === 'live') {
				return { logSource: resolved.value, logContent: '', logUnavailable: false };
			}
		}
		throw err;
	}
}

export function createViewsRouter({ orchestrator }) {
	const router = Router();

	router.get('/workers', async (req, res) => {
		try {
			const workers = await orchestrator.getJson('/workers');
			const visible = workers.filter((c) => ACTIVE_STATUSES.has(c.status));
			if (isHtmx(req)) {
				sendFragment(res, workerRows(visible));
				return;
			}
			sendPage(res, layout({
				title: 'Workers',
				activePath: '/views/workers',
				body: workerListPage(visible),
			}));
		} catch (err) {
			const { status } = describeOrchestratorError(err);
			if (isHtmx(req)) {
				sendFragment(res, html`<tr class="error-row"><td colspan="6">${renderOrchestratorError(err)}</td></tr>`, status);
				return;
			}
			res.status(status).type('html').send(layout({
				title: 'Workers',
				activePath: '/views/workers',
				body: renderOrchestratorError(err),
			}).toString());
		}
	});

	router.get('/workers/:id', async (req, res) => {
		const id = req.params.id;
		const encodedId = encodeURIComponent(id);
		try {
			const [worker, filesResult, commands, images] = await Promise.all([
				orchestrator.getJson(`/workers/${encodedId}`),
				fetchLogFiles(orchestrator, encodedId),
				fetchCommands(orchestrator, encodedId),
				fetchImages(orchestrator),
			]);
			const { logFiles, filesUnavailable } = filesResult;
			const { logSource, logContent, logUnavailable } = await fetchLogContent(
				orchestrator,
				encodedId,
				req.query.log_source,
				logFiles,
			);
			const canExec = imageCapabilities(images, worker.image).has('exec');
			sendPage(res, layout({
				title: `Worker ${worker.id}`,
				activePath: '/views/workers',
				body: workerDetailPage(worker, {
					logFiles,
					filesUnavailable,
					logSource,
					logContent,
					logUnavailable,
				}, canExec ? commands : null, { canExec }),
			}));
		} catch (err) {
			const { status } = describeOrchestratorError(err);
			res.status(status).type('html').send(layout({
				title: 'Worker',
				activePath: '/views/workers',
				body: renderOrchestratorError(err),
			}).toString());
		}
	});

	router.get('/workers/:id/execs/:commandId', async (req, res) => {
		const id = req.params.id;
		const commandId = req.params.commandId;
		const encodedId = encodeURIComponent(id);
		const encodedCommandId = encodeURIComponent(commandId);
		try {
			const [worker, exec] = await Promise.all([
				orchestrator.getJson(`/workers/${encodedId}`),
				orchestrator.getJson(`/workers/${encodedId}/execs/${encodedCommandId}`),
			]);
			sendPage(res, layout({
				title: `Exec ${exec.command_id}`,
				activePath: '/views/workers',
				body: execOutputPage(worker, exec),
			}));
		} catch (err) {
			const { status } = describeOrchestratorError(err);
			res.status(status).type('html').send(layout({
				title: 'Exec',
				activePath: '/views/workers',
				body: renderOrchestratorError(err),
			}).toString());
		}
	});

	router.get('/images', async (_req, res) => {
		try {
			const images = await orchestrator.getJson('/images');
			sendPage(res, layout({
				title: 'Images',
				activePath: '/views/images',
				body: imagesListPage(images),
			}));
		} catch (err) {
			const { status } = describeOrchestratorError(err);
			res.status(status).type('html').send(layout({
				title: 'Images',
				activePath: '/views/images',
				body: renderOrchestratorError(err),
			}).toString());
		}
	});

	router.get('/launch', async (_req, res) => {
		let images = [];
		let warning = null;
		try {
			images = await orchestrator.getJson('/images');
		} catch (err) {
			warning = errorPanel({
				title: 'Could not load images',
				detail: describeOrchestratorError(err).detail,
			});
		}
		sendPage(res, layout({
			title: 'Launch',
			activePath: '/views/launch',
			body: launchFormPage({ images, error: warning }),
		}));
	});

	return router;
}
