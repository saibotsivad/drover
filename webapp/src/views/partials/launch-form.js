import { html } from '../render.js';

export function launchFormPage({ images = [], values = {}, error = null } = {}) {
	const v = {
		image: values.image ?? '',
		label: values.label ?? '',
		env: values.env ?? '',
		timeout_seconds: values.timeout_seconds ?? 300,
		privileged: Boolean(values.privileged),
	};

	const imageOptions = images.length > 0
		? html`<select id="image" name="image" required>
			<option value="" disabled ${v.image ? '' : 'selected'}>Pick an image…</option>
			${images.map((img) => html`<option
				value="${img.name}"
				${img.name === v.image ? 'selected' : ''}
			>${img.name}</option>`)}
		</select>`
		: html`<input
			type="text"
			id="image"
			name="image"
			value="${v.image}"
			placeholder="e.g. python-runner"
			required
		/>`;

	return html`<section id="launch-form">
		<div class="page-header">
			<h2>Launch container</h2>
			<a class="btn btn-secondary" href="/views/containers">Cancel</a>
		</div>
		${error}
		<form
			class="launch-form"
			hx-post="/actions/containers"
			hx-target="this"
			hx-swap="outerHTML"
		>
			<label for="image">Image</label>
			${imageOptions}

			<label for="label">Label <span class="muted">(optional)</span></label>
			<input type="text" id="label" name="label" value="${v.label}" maxlength="1024" />

			<label for="env">Environment <span class="muted">(one <code>KEY=VALUE</code> per line)</span></label>
			<textarea id="env" name="env" rows="6" spellcheck="false">${v.env}</textarea>

			<label for="timeout_seconds">Timeout (seconds)</label>
			<input
				type="number"
				id="timeout_seconds"
				name="timeout_seconds"
				value="${v.timeout_seconds}"
				min="1"
				max="86400"
				required
			/>

			<label class="checkbox">
				<input
					type="checkbox"
					id="privileged"
					name="privileged"
					value="true"
					${v.privileged ? 'checked' : ''}
				/>
				Privileged
			</label>

			<div class="form-actions">
				<button type="submit" class="btn btn-primary">Launch</button>
			</div>
		</form>
	</section>`;
}
