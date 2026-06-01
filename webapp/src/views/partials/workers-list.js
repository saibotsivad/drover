import { html, safe } from '../render.js';

export function statusPill(status) {
	const slug = String(status || 'unknown').toLowerCase();
	return html`<span class="status status-${safe(slug)}">${status}</span>`;
}

function actionButtons(worker) {
	const id = worker.id;
	const isStoppable = worker.status === 'running' || worker.status === 'initializing';
	const isDestroyable = worker.status !== 'destroying' && worker.status !== 'destroyed';
	return html`<div class="row-actions">
		${isStoppable ? html`<button
			type="button"
			class="btn btn-secondary btn-stop"
			hx-post="/actions/workers/${id}/stop"
			hx-target="#worker-${id}"
			hx-swap="outerHTML"
		>Stop</button>` : null}
		${isDestroyable ? html`<button
			type="button"
			class="btn btn-danger btn-destroy"
			hx-delete="/actions/workers/${id}"
			hx-target="#worker-${id}"
			hx-swap="outerHTML"
			hx-confirm="Destroy this worker?"
		>Destroy</button>` : null}
	</div>`;
}

export function workerRow(worker) {
	return html`<tr id="worker-${worker.id}">
		<td><a href="/views/workers/${worker.id}"><code>${worker.id}</code></a></td>
		<td><code>${worker.image}</code></td>
		<td>${worker.label || html`<span class="muted">—</span>`}</td>
		<td>${statusPill(worker.status)}</td>
		<td><time datetime="${worker.created_at}">${formatRelative(worker.created_at)}</time></td>
		<td>${actionButtons(worker)}</td>
	</tr>`;
}

export function workerRows(workers) {
	if (workers.length === 0) {
		return html`<tr class="empty"><td colspan="6">No workers.</td></tr>`;
	}
	return workers.map(workerRow);
}

export function workerListPage(workers) {
	return html`<section id="workers-list">
		<div class="page-header">
			<h2>Workers</h2>
			<a class="btn btn-primary" href="/views/launch">Launch worker</a>
		</div>
		<table class="data-table">
			<thead>
				<tr>
					<th>ID</th>
					<th>Image</th>
					<th>Label</th>
					<th>Status</th>
					<th>Created</th>
					<th class="col-actions">Actions</th>
				</tr>
			</thead>
			<tbody
				id="worker-rows"
				hx-get="/views/workers"
				hx-trigger="every 5s"
				hx-target="#worker-rows"
				hx-swap="innerHTML"
			>${workerRows(workers)}</tbody>
		</table>
	</section>`;
}

function formatRelative(iso) {
	if (!iso) return '';
	const then = Date.parse(iso);
	if (Number.isNaN(then)) return iso;
	const diff = Math.max(0, Date.now() - then) / 1000;
	if (diff < 60) return 'just now';
	if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
	if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
	return `${Math.floor(diff / 86400)}d ago`;
}
