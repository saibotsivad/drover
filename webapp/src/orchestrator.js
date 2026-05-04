export class OrchestratorError extends Error {
	constructor(message, { status = null, body = null, cause = null } = {}) {
		super(message);
		this.name = 'OrchestratorError';
		this.status = status;
		this.body = body;
		if (cause) this.cause = cause;
	}
}

export class OrchestratorUnreachableError extends OrchestratorError {
	constructor(cause) {
		super(`orchestrator unreachable: ${cause?.message ?? 'unknown error'}`, { cause });
		this.name = 'OrchestratorUnreachableError';
	}
}

export class OrchestratorHttpError extends OrchestratorError {
	constructor(status, body) {
		super(`orchestrator returned HTTP ${status}`, { status, body });
		this.name = 'OrchestratorHttpError';
	}
}

function buildHeaders(apiKey, extra = {}) {
	const headers = { Accept: 'application/json', ...extra };
	if (apiKey) headers.Authorization = `Bearer ${apiKey}`;
	return headers;
}

async function readBody(res) {
	const contentType = res.headers.get('content-type') || '';
	if (contentType.includes('application/json')) {
		try {
			return await res.json();
		} catch {
			return null;
		}
	}
	try {
		return await res.text();
	} catch {
		return null;
	}
}

async function request(method, baseUrl, apiKey, pathAndQuery, { body, headers } = {}) {
	const url = `${baseUrl.replace(/\/$/, '')}${pathAndQuery}`;
	const init = { method, headers: buildHeaders(apiKey, headers) };
	if (body !== undefined) {
		init.body = typeof body === 'string' ? body : JSON.stringify(body);
		init.headers['Content-Type'] = init.headers['Content-Type'] || 'application/json';
	}

	let res;
	try {
		res = await fetch(url, init);
	} catch (err) {
		throw new OrchestratorUnreachableError(err);
	}

	if (!res.ok) {
		const errBody = await readBody(res);
		throw new OrchestratorHttpError(res.status, errBody);
	}

	if (res.status === 204) return null;
	return readBody(res);
}

export function createOrchestratorClient({ baseUrl, apiKey = null }) {
	if (!baseUrl) throw new Error('createOrchestratorClient requires baseUrl');
	return {
		getJson: (pathAndQuery) => request('GET', baseUrl, apiKey, pathAndQuery),
		postJson: (pathAndQuery, body) => request('POST', baseUrl, apiKey, pathAndQuery, { body }),
		del: (pathAndQuery) => request('DELETE', baseUrl, apiKey, pathAndQuery),
	};
}
