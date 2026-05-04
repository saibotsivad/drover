import { OrchestratorHttpError, OrchestratorUnreachableError } from '../../orchestrator.js';
import { html } from '../render.js';

export function errorPanel({ title, detail = null }) {
	return html`<section class="error-panel" role="alert">
		<h2>${title}</h2>
		${detail ? html`<p>${detail}</p>` : null}
	</section>`;
}

export function describeOrchestratorError(err) {
	if (err instanceof OrchestratorUnreachableError) {
		return { status: 502, title: 'Orchestrator unreachable', detail: 'Could not reach the Drover orchestrator. Check that it is running and that DROVER_ORCHESTRATOR_URL is correct.' };
	}
	if (err instanceof OrchestratorHttpError) {
		const detail = formatBody(err.body);
		if (err.status === 401) return { status: 401, title: 'Unauthorized', detail: 'The orchestrator rejected the request. Check that DROVER_API_KEY matches the orchestrator.' };
		if (err.status === 404) return { status: 404, title: 'Not found', detail: detail || 'The orchestrator did not find the requested resource.' };
		if (err.status === 422) return { status: 422, title: 'Invalid request', detail: detail || 'The orchestrator rejected the request as invalid.' };
		return { status: err.status, title: `Orchestrator error (HTTP ${err.status})`, detail };
	}
	return { status: 500, title: 'Unexpected error', detail: err?.message ?? null };
}

export function renderOrchestratorError(err) {
	const { title, detail } = describeOrchestratorError(err);
	return errorPanel({ title, detail });
}

function formatBody(body) {
	if (body == null) return null;
	if (typeof body === 'string') return body;
	if (typeof body === 'object' && typeof body.detail === 'string') return body.detail;
	try {
		return JSON.stringify(body);
	} catch {
		return null;
	}
}
