export class ConfigError extends Error {}

export function loadConfig(env = process.env) {
	const orchestratorUrl = (env.DROVER_ORCHESTRATOR_URL || '').trim();
	if (!orchestratorUrl) {
		throw new ConfigError('DROVER_ORCHESTRATOR_URL is required');
	}
	try {
		new URL(orchestratorUrl);
	} catch {
		throw new ConfigError(`DROVER_ORCHESTRATOR_URL is not a valid URL: ${orchestratorUrl}`);
	}

	const apiKey = env.DROVER_API_KEY ? String(env.DROVER_API_KEY) : null;

	const portRaw = (env.PORT || '8080').trim();
	const port = Number.parseInt(portRaw, 10);
	if (!Number.isInteger(port) || port < 1 || port > 65535) {
		throw new ConfigError(`PORT is not a valid port number: ${portRaw}`);
	}

	const logLevel = (env.DROVER_LOG_LEVEL || 'info').trim().toLowerCase();

	return { orchestratorUrl, apiKey, port, logLevel };
}
