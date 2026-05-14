import { html, safe } from '../render.js';
import { statusPill } from './containers-list.js';

function metadataRow(label, value) {
	return html`<div class="meta-row">
		<dt>${label}</dt>
		<dd>${value}</dd>
	</div>`;
}

function actionBar(container) {
	const id = container.id;
	const isStoppable = container.status === 'running' || container.status === 'initializing';
	const isDestroyable = container.status !== 'destroying' && container.status !== 'destroyed';
	if (!isStoppable && !isDestroyable) return null;
	return html`<div class="action-bar">
		${isStoppable ? html`<button
			type="button"
			class="btn btn-secondary"
			hx-post="/actions/containers/${id}/stop"
			hx-target="#container-meta"
			hx-swap="outerHTML"
		>Stop</button>` : null}
		${isDestroyable ? html`<button
			type="button"
			class="btn btn-danger"
			hx-delete="/actions/containers/${id}"
			hx-target="#container-meta"
			hx-swap="outerHTML"
			hx-confirm="Destroy this container?"
		>Destroy</button>` : null}
	</div>`;
}

function logSourceOption(value, label, selectedValue) {
	const selected = value === selectedValue ? safe(' selected') : null;
	return html`<option value="${value}"${selected}>${label}</option>`;
}

function logsSection(id, opts) {
	const {
		logFiles = [],
		filesUnavailable = false,
		logSource = 'live',
		logContent = null,
		logUnavailable = false,
	} = opts || {};
	const encodedId = encodeURIComponent(id);
	const onchange = safe(
		`window.location.href='/views/containers/${encodedId}?log_source='+encodeURIComponent(this.value)`,
	);
	const fileOptions = logFiles.map((name) =>
		logSourceOption(`file:${encodeURIComponent(name)}`, name, logSource),
	);
	let viewer;
	if (logContent === null) {
		const message = logUnavailable
			? 'Container logging not configured'
			: 'Log file not found';
		viewer = html`<p class="muted log-viewer-empty">${message}</p>`;
	} else if (logContent === '') {
		viewer = html`<p class="muted log-viewer-empty">(no log output)</p>`;
	} else {
		viewer = html`<pre id="log-viewer" class="log-viewer">${logContent}</pre>`;
	}
	return html`<section class="logs-section">
		<h3>Logs</h3>
		<label class="log-source-label">
			Source
			<select class="log-source-select" onchange="${onchange}">
				${logSourceOption('live', 'Live container logs', logSource)}
				${fileOptions}
				${logSourceOption('orchestrator', 'Orchestrator logs', logSource)}
			</select>
		</label>
		${filesUnavailable ? html`<p class="muted log-files-note">File-based log capture is not configured (DROVER_LOG_DIR is unset)</p>` : null}
		${viewer}
	</section>`;
}

export function containerDetailPage(container, logOpts = null) {
	return html`<section>
		<div class="page-header">
			<h2>Container <code>${container.id}</code></h2>
			<a class="btn btn-secondary" href="/views/containers">Back to list</a>
		</div>
		<dl id="container-meta" class="meta-grid">
			${metadataRow('Status', statusPill(container.status))}
			${metadataRow('Image', html`<code>${container.image}</code>`)}
			${metadataRow('Label', container.label || html`<span class="muted">—</span>`)}
			${metadataRow('Privileged', container.privileged ? 'yes' : 'no')}
			${metadataRow('Timeout (seconds)', container.timeout_seconds)}
			${metadataRow('Created', html`<time datetime="${container.created_at}">${container.created_at}</time>`)}
			${container.last_seen ? metadataRow('Last seen', html`<time datetime="${container.last_seen}">${container.last_seen}</time>`) : null}
			${container.stopped_at ? metadataRow('Stopped', html`<time datetime="${container.stopped_at}">${container.stopped_at}</time>`) : null}
			${container.error_code ? metadataRow('Error code', html`<code>${container.error_code}</code>`) : null}
		</dl>
		${actionBar(container)}
		${logOpts ? logsSection(container.id, logOpts) : null}
	</section>`;
}
