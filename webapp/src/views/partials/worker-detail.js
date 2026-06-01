import { html, safe } from '../render.js';
import { statusPill } from './workers-list.js';

function metadataRow(label, value) {
	return html`<div class="meta-row">
		<dt>${label}</dt>
		<dd>${value}</dd>
	</div>`;
}

function actionBar(worker) {
	const id = worker.id;
	const isStoppable = worker.status === 'running' || worker.status === 'initializing';
	const isResumable = worker.status === 'stopped';
	const isDestroyable = worker.status !== 'destroying' && worker.status !== 'destroyed';
	if (!isStoppable && !isResumable && !isDestroyable) return null;
	return html`<div class="action-bar">
		${isResumable ? html`<button
			type="button"
			class="btn btn-primary btn-resume"
			hx-post="/actions/workers/${id}/resume"
		>Resume</button>` : null}
		${isStoppable ? html`<button
			type="button"
			class="btn btn-secondary btn-stop"
			hx-post="/actions/workers/${id}/stop"
			hx-target="#worker-meta"
			hx-swap="outerHTML"
		>Stop</button>` : null}
		${isDestroyable ? html`<button
			type="button"
			class="btn btn-danger btn-destroy"
			hx-delete="/actions/workers/${id}"
			hx-target="#worker-meta"
			hx-swap="outerHTML"
			hx-confirm="Destroy this worker?"
		>Destroy</button>` : null}
	</div>`;
}

function commandStatusPill(status) {
	const slug = String(status || 'unknown').toLowerCase();
	return html`<span class="status status-${safe(slug)}">${status}</span>`;
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

function commandRow(workerId, command) {
	const encodedWorker = encodeURIComponent(workerId);
	const encodedCommand = encodeURIComponent(command.command_id);
	const href = `/views/workers/${encodedWorker}/execs/${encodedCommand}`;
	const exitCode = command.exit_code === null || command.exit_code === undefined
		? html`<span class="muted">—</span>`
		: command.exit_code;
	return html`<tr id="command-${command.command_id}">
		<td><a href="${href}"><code>${command.command}</code></a></td>
		<td>${commandStatusPill(command.status)}</td>
		<td>${exitCode}</td>
		<td><time datetime="${command.created_at}">${formatRelative(command.created_at)}</time></td>
	</tr>`;
}

export function commandRows(workerId, commands) {
	return commands.length === 0
		? html`<tr class="empty"><td colspan="4">No exec commands yet</td></tr>`
		: commands.map((c) => commandRow(workerId, c));
}

function execInputSection(workerId) {
	return html`<section class="exec-input-section">
		<form
			class="exec-input-form"
			hx-post="/actions/workers/${workerId}/execs"
			hx-target="#command-rows"
			hx-swap="innerHTML"
			hx-on::after-request="if(event.detail.successful){this.reset();this.querySelector('[data-replicated-value]').dataset.replicatedValue=''}"
		>
			<div class="grow-wrap" data-replicated-value="">
				<textarea
					name="command"
					placeholder="e.g. ls -la /etc"
					rows="1"
					oninput="this.parentNode.dataset.replicatedValue = this.value"
				></textarea>
			</div>
			<button type="submit" class="btn btn-primary">Run</button>
		</form>
	</section>`;
}

function execSection(workerId, commands) {
	return html`<section class="exec-section">
		<h3>Exec Commands</h3>
		<table class="data-table">
			<thead>
				<tr>
					<th>Command</th>
					<th>Status</th>
					<th>Exit code</th>
					<th>Started</th>
				</tr>
			</thead>
			<tbody id="command-rows">${commandRows(workerId, commands)}</tbody>
		</table>
	</section>`;
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
		`window.location.href='/views/workers/${encodedId}?log_source='+encodeURIComponent(this.value)`,
	);
	const fileOptions = logFiles.map((name) =>
		logSourceOption(`file:${encodeURIComponent(name)}`, name, logSource),
	);
	let viewer;
	if (logContent === null) {
		const message = logUnavailable
			? 'Worker logging not configured'
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
				${logSourceOption('live', 'Live worker logs', logSource)}
				${fileOptions}
				${logSourceOption('orchestrator', 'Orchestrator logs', logSource)}
			</select>
		</label>
		${filesUnavailable ? html`<p class="muted log-files-note">File-based log capture is not configured (DROVER_LOG_DIR is unset)</p>` : null}
		${viewer}
	</section>`;
}

export function workerDetailPage(worker, logOpts = null, commands = null, { canExec = false } = {}) {
	return html`<section id="worker-detail">
		<div class="page-header">
			<h2>Worker <code>${worker.id}</code></h2>
			<a class="btn btn-secondary" href="/views/workers">Back to list</a>
		</div>
		<dl id="worker-meta" class="meta-grid">
			${metadataRow('Status', statusPill(worker.status))}
			${metadataRow('Image', html`<code>${worker.image}</code>`)}
			${metadataRow('Label', worker.label || html`<span class="muted">—</span>`)}
			${metadataRow('Privileged', worker.privileged ? 'yes' : 'no')}
			${metadataRow('Timeout (seconds)', worker.timeout_seconds)}
			${metadataRow('Created', html`<time datetime="${worker.created_at}">${worker.created_at}</time>`)}
			${worker.last_seen ? metadataRow('Last seen', html`<time datetime="${worker.last_seen}">${worker.last_seen}</time>`) : null}
			${worker.stopped_at ? metadataRow('Stopped', html`<time datetime="${worker.stopped_at}">${worker.stopped_at}</time>`) : null}
			${worker.error_code ? metadataRow('Error code', html`<code>${worker.error_code}</code>`) : null}
		</dl>
		${actionBar(worker)}
		${canExec && commands !== null ? execInputSection(worker.id) : null}
		${canExec && commands !== null ? execSection(worker.id, commands) : null}
		${!canExec ? html`<section class="exec-section">
			<h3>Exec Commands</h3>
			<p class="muted">This image does not support exec commands.</p>
		</section>` : null}
		${logOpts ? logsSection(worker.id, logOpts) : null}
	</section>`;
}
