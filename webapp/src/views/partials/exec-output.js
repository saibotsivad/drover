import { html, safe } from '../render.js';

function statusPill(status) {
	const slug = String(status || 'unknown').toLowerCase();
	return html`<span class="status status-${safe(slug)}">${status}</span>`;
}

function metadataRow(label, value) {
	return html`<div class="meta-row">
		<dt>${label}</dt>
		<dd>${value}</dd>
	</div>`;
}

function outputChunk(message) {
	const cls = message.stream === 'stderr' ? 'output-chunk output-stderr' : 'output-chunk';
	return html`<span class="${cls}">${message.data}</span>`;
}

export function execOutputPage(container, exec) {
	const messages = Array.isArray(exec.messages) ? exec.messages : [];
	const containerHref = `/views/containers/${encodeURIComponent(container.id)}`;
	const output = messages.length === 0
		? html`<p class="muted exec-output-empty">(no output yet)</p>`
		: html`<pre id="exec-output" class="exec-output">${messages.map(outputChunk)}</pre>`;
	const showExitCode = exec.status === 'complete' && exec.exit_code !== null && exec.exit_code !== undefined;
	return html`<section id="exec-detail">
		<div class="page-header">
			<h2>Exec: <code>${exec.command}</code></h2>
			<a class="btn btn-secondary" href="${containerHref}">Back to container</a>
		</div>
		<dl id="exec-meta" class="meta-grid">
			${metadataRow('Status', statusPill(exec.status))}
			${showExitCode ? metadataRow('Exit code', exec.exit_code) : null}
			${metadataRow('Command ID', html`<code>${exec.command_id}</code>`)}
		</dl>
		<section class="exec-output-section">
			<h3>Output</h3>
			${output}
		</section>
	</section>`;
}
