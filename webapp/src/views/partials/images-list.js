import { html } from '../render.js';

function formatSize(bytes) {
	if (typeof bytes !== 'number' || !Number.isFinite(bytes)) return '';
	if (bytes < 1024) return `${bytes} B`;
	if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
	if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
	return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

function imageRow(image) {
	return html`<tr>
		<td><code>${image.name}</code></td>
		<td>${image.tags && image.tags.length
			? image.tags.map((t) => html`<code class="tag">${t}</code>`)
			: html`<span class="muted">—</span>`}</td>
		<td>${formatSize(image.size)}</td>
		<td><time datetime="${image.created}">${image.created}</time></td>
	</tr>`;
}

export function imagesListPage(images) {
	return html`<section>
		<div class="page-header">
			<h2>Images</h2>
		</div>
		<table class="data-table">
			<thead>
				<tr>
					<th>Name</th>
					<th>Tags</th>
					<th>Size</th>
					<th>Created</th>
				</tr>
			</thead>
			<tbody>${images.length === 0
				? html`<tr class="empty"><td colspan="4">No Drover-managed images. Add the <code>drover.managed=true</code> and <code>drover.name=&lt;name&gt;</code> labels to an image to make it visible here.</td></tr>`
				: images.map(imageRow)}</tbody>
		</table>
	</section>`;
}
