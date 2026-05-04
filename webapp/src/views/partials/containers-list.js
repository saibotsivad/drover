import { html, safe } from '../render.js';

export function statusPill(status) {
	const slug = String(status || 'unknown').toLowerCase();
	return html`<span class="status status-${safe(slug)}">${status}</span>`;
}

function actionButtons(container) {
	const id = container.id;
	const isStoppable = container.status === 'running' || container.status === 'initializing';
	const isDestroyable = container.status !== 'destroying' && container.status !== 'destroyed';
	return html`<div class="row-actions">
		${isStoppable ? html`<button
			type="button"
			class="btn btn-secondary"
			hx-post="/actions/containers/${id}/stop"
			hx-target="#container-${id}"
			hx-swap="outerHTML"
		>Stop</button>` : null}
		${isDestroyable ? html`<button
			type="button"
			class="btn btn-danger"
			hx-delete="/actions/containers/${id}"
			hx-target="#container-${id}"
			hx-swap="outerHTML"
			hx-confirm="Destroy this container?"
		>Destroy</button>` : null}
	</div>`;
}

export function containerRow(container) {
	return html`<tr id="container-${container.id}">
		<td><a href="/views/containers/${container.id}"><code>${container.id}</code></a></td>
		<td><code>${container.image}</code></td>
		<td>${container.label || html`<span class="muted">—</span>`}</td>
		<td>${statusPill(container.status)}</td>
		<td><time datetime="${container.created_at}">${formatRelative(container.created_at)}</time></td>
		<td>${actionButtons(container)}</td>
	</tr>`;
}

export function containerRows(containers) {
	if (containers.length === 0) {
		return html`<tr class="empty"><td colspan="6">No containers.</td></tr>`;
	}
	return containers.map(containerRow);
}

export function containerListPage(containers) {
	return html`<section>
		<div class="page-header">
			<h2>Containers</h2>
			<a class="btn btn-primary" href="/views/launch">Launch container</a>
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
				id="container-rows"
				hx-get="/views/containers"
				hx-trigger="every 5s"
				hx-target="#container-rows"
				hx-swap="innerHTML"
			>${containerRows(containers)}</tbody>
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
