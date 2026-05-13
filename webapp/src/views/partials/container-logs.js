import { html } from '../render.js';

// Statuses where new container output is still possible — keep polling.
const LIVE_STATUSES = new Set(['initializing', 'running', 'stopping', 'resuming', 'destroying']);

function joinFrames(frames) {
	if (!Array.isArray(frames) || frames.length === 0) return '';
	return frames.map((f) => f.data).join('');
}

function containerSection(payload) {
	if (payload.container_logs_unavailable) {
		return html`<div class="logs-pane">
			<h3>Container output</h3>
			<p class="muted">Docker has already removed this container; live output is no longer available.</p>
		</div>`;
	}
	const text = joinFrames(payload.container_logs);
	return html`<div class="logs-pane">
		<h3>Container output</h3>
		${text
			? html`<pre class="logs-output">${text}</pre>`
			: html`<p class="muted">No output captured yet.</p>`}
	</div>`;
}

function orchestratorSection(payload) {
	const records = Array.isArray(payload.orchestrator_logs) ? payload.orchestrator_logs : [];
	if (records.length === 0) {
		return html`<div class="logs-pane">
			<h3>Orchestrator events</h3>
			<p class="muted">No orchestrator events recorded for this container.</p>
		</div>`;
	}
	return html`<div class="logs-pane">
		<h3>Orchestrator events</h3>
		<ol class="orchestrator-events">
			${records.map((r) => html`<li class="orchestrator-event level-${r.level.toLowerCase()}">
				<time datetime="${r.ts}">${r.ts}</time>
				<span class="level">${r.level}</span>
				<span class="message">${r.message}</span>
			</li>`)}
		</ol>
	</div>`;
}

export function containerLogsFragment(payload) {
	const isLive = LIVE_STATUSES.has(payload.status);
	const triggerAttrs = isLive
		? html` hx-trigger="load, every 3s"`
		: html` hx-trigger="load"`;
	return html`<section
		id="container-logs"
		class="logs-section"
		hx-get="/views/containers/${payload.container_id}/logs"
		hx-swap="outerHTML"
		${triggerAttrs}
	>
		<h2>Logs</h2>
		${containerSection(payload)}
		${orchestratorSection(payload)}
		${isLive
			? html`<p class="muted logs-footer">Auto-refreshing every 3 seconds.</p>`
			: html`<p class="muted logs-footer">Container is no longer active; logs will not refresh.</p>`}
	</section>`;
}

export function containerLogsPlaceholder(containerId) {
	return html`<section
		id="container-logs"
		class="logs-section"
		hx-get="/views/containers/${containerId}/logs"
		hx-swap="outerHTML"
		hx-trigger="load"
	>
		<h2>Logs</h2>
		<p class="muted">Loading logs…</p>
	</section>`;
}
