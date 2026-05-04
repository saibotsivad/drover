const ESCAPE_MAP = {
	'&': '&amp;',
	'<': '&lt;',
	'>': '&gt;',
	'"': '&quot;',
	"'": '&#39;',
};

function escapeChar(c) {
	return ESCAPE_MAP[c];
}

export function escape(value) {
	return String(value).replace(/[&<>"']/g, escapeChar);
}

export class SafeHtml {
	constructor(value) {
		this.value = String(value);
	}
	toString() {
		return this.value;
	}
}

export function safe(value) {
	return new SafeHtml(value);
}

function renderValue(value) {
	if (value === null || value === undefined || value === false) return '';
	if (value instanceof SafeHtml) return value.value;
	if (Array.isArray(value)) return value.map(renderValue).join('');
	return escape(value);
}

export function html(strings, ...values) {
	let out = '';
	for (let i = 0; i < strings.length; i++) {
		out += strings[i];
		if (i < values.length) out += renderValue(values[i]);
	}
	return new SafeHtml(out);
}
