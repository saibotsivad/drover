import { Router } from 'express';
import { OrchestratorHttpError } from '../orchestrator.js';
import { layout } from '../views/layout.js';
import { containerDetailPage } from '../views/partials/container-detail.js';
import { containerListPage, containerRows } from '../views/partials/containers-list.js';
import { describeOrchestratorError, errorPanel, renderOrchestratorError } from '../views/partials/errors.js';
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

async function fetchLogFiles(orchestrator, encodedId) {
	try {
		const logFiles = await orchestrator.getJson(`/containers/${encodedId}/logs/files`);
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
	if (resolved.kind === 'live') path = `/containers/${encodedId}/logs`;
	else if (resolved.kind === 'orchestrator') path = `/containers/${encodedId}/logs/orchestrator`;
	else path = `/containers/${encodedId}/logs/files/${encodeURIComponent(resolved.filename)}`;

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

	router.get('/containers', async (req, res) => {
		try {
			const containers = await orchestrator.getJson('/containers');
			const visible = containers.filter((c) => ACTIVE_STATUSES.has(c.status));
			if (isHtmx(req)) {
				sendFragment(res, containerRows(visible));
				return;
			}
			sendPage(res, layout({
				title: 'Containers',
				activePath: '/views/containers',
				body: containerListPage(visible),
			}));
		} catch (err) {
			const { status } = describeOrchestratorError(err);
			if (isHtmx(req)) {
				sendFragment(res, html`<tr class="error-row"><td colspan="6">${renderOrchestratorError(err)}</td></tr>`, status);
				return;
			}
			res.status(status).type('html').send(layout({
				title: 'Containers',
				activePath: '/views/containers',
				body: renderOrchestratorError(err),
			}).toString());
		}
	});

	router.get('/containers/:id', async (req, res) => {
		const id = req.params.id;
		const encodedId = encodeURIComponent(id);
		try {
			const [container, filesResult] = await Promise.all([
				orchestrator.getJson(`/containers/${encodedId}`),
				fetchLogFiles(orchestrator, encodedId),
			]);
			const { logFiles, filesUnavailable } = filesResult;
			const { logSource, logContent, logUnavailable } = await fetchLogContent(
				orchestrator,
				encodedId,
				req.query.log_source,
				logFiles,
			);
			sendPage(res, layout({
				title: `Container ${container.id}`,
				activePath: '/views/containers',
				body: containerDetailPage(container, {
					logFiles,
					filesUnavailable,
					logSource,
					logContent,
					logUnavailable,
				}),
			}));
		} catch (err) {
			const { status } = describeOrchestratorError(err);
			res.status(status).type('html').send(layout({
				title: 'Container',
				activePath: '/views/containers',
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
