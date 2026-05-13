import { Router } from 'express';
import { layout } from '../views/layout.js';
import { containerDetailPage } from '../views/partials/container-detail.js';
import { containerListPage, containerRows } from '../views/partials/containers-list.js';
import { containerLogsFragment } from '../views/partials/container-logs.js';
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
		try {
			const container = await orchestrator.getJson(`/containers/${encodeURIComponent(req.params.id)}`);
			sendPage(res, layout({
				title: `Container ${container.id}`,
				activePath: '/views/containers',
				body: containerDetailPage(container),
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

	router.get('/containers/:id/logs', async (req, res) => {
		try {
			const payload = await orchestrator.getJson(
				`/containers/${encodeURIComponent(req.params.id)}/logs`,
			);
			sendFragment(res, containerLogsFragment(payload));
		} catch (err) {
			const { status } = describeOrchestratorError(err);
			sendFragment(res, html`<section id="container-logs" class="logs-section">
				<h2>Logs</h2>
				${renderOrchestratorError(err)}
			</section>`, status);
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
