import { Router, urlencoded } from 'express';
import { containerRow } from '../views/partials/containers-list.js';
import { commandRows } from '../views/partials/container-detail.js';
import { describeOrchestratorError, renderOrchestratorError } from '../views/partials/errors.js';
import { launchFormPage } from '../views/partials/launch-form.js';
import { html } from '../views/render.js';

function parseEnv(text) {
	const env = {};
	if (!text) return env;
	for (const raw of String(text).split(/\r?\n/)) {
		const line = raw.trim();
		if (!line) continue;
		const eq = line.indexOf('=');
		if (eq <= 0) continue;
		const key = line.slice(0, eq).trim();
		const value = line.slice(eq + 1);
		if (key) env[key] = value;
	}
	return env;
}

function sendFragment(res, fragment, status = 200) {
	res.status(status).type('html').send(fragment.toString());
}

export function createActionsRouter({ orchestrator }) {
	const router = Router();
	router.use(urlencoded({ extended: false, limit: '64kb' }));

	router.post('/containers', async (req, res) => {
		const body = req.body || {};
		const values = {
			image: typeof body.image === 'string' ? body.image.trim() : '',
			label: typeof body.label === 'string' ? body.label.trim() : '',
			env: typeof body.env === 'string' ? body.env : '',
			timeout_seconds: body.timeout_seconds,
			privileged: body.privileged === 'true' || body.privileged === 'on',
		};

		const payload = {
			image: values.image,
			privileged: values.privileged,
			env: parseEnv(values.env),
			timeout_seconds: Number.parseInt(values.timeout_seconds, 10) || 300,
		};
		if (values.label) payload.label = values.label;

		let images = [];
		try {
			images = await orchestrator.getJson('/images');
		} catch {
			images = [];
		}

		try {
			const container = await orchestrator.postJson('/containers', payload);
			res.set('HX-Redirect', `/views/containers/${container.id}`);
			res.status(201).type('html').send('');
		} catch (err) {
			const { status } = describeOrchestratorError(err);
			sendFragment(res, launchFormPage({
				images,
				values,
				error: renderOrchestratorError(err),
			}), status);
		}
	});

	router.post('/containers/:id/stop', async (req, res) => {
		try {
			const container = await orchestrator.postJson(
				`/containers/${encodeURIComponent(req.params.id)}/stop`,
			);
			sendFragment(res, containerRow(container));
		} catch (err) {
			const { status } = describeOrchestratorError(err);
			sendFragment(res, html`<tr id="container-${req.params.id}" class="error-row">
				<td colspan="6">${renderOrchestratorError(err)}</td>
			</tr>`, status);
		}
	});

	router.post('/containers/:id/execs', async (req, res) => {
		const encodedId = encodeURIComponent(req.params.id);
		const body = req.body || {};
		const command = typeof body.command === 'string' ? body.command.trim() : '';
		try {
			await orchestrator.postJson(`/containers/${encodedId}/execs`, { command });
			const commands = await orchestrator.getJson(`/containers/${encodedId}/execs`);
			sendFragment(res, html`${commandRows(req.params.id, Array.isArray(commands) ? commands : [])}`);
		} catch (err) {
			const { status } = describeOrchestratorError(err);
			sendFragment(res, html`<tr class="error-row"><td colspan="4">${renderOrchestratorError(err)}</td></tr>`, status);
		}
	});

	router.delete('/containers/:id', async (req, res) => {
		try {
			await orchestrator.del(`/containers/${encodeURIComponent(req.params.id)}`);
			res.status(200).type('html').send('');
		} catch (err) {
			const { status } = describeOrchestratorError(err);
			sendFragment(res, html`<tr id="container-${req.params.id}" class="error-row">
				<td colspan="6">${renderOrchestratorError(err)}</td>
			</tr>`, status);
		}
	});

	return router;
}
