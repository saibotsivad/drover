import { createApp } from './app.js';
import { ConfigError, loadConfig } from './config.js';
import { createLogger } from './logger.js';

function main() {
	const bootstrapLogger = createLogger({ level: process.env.DROVER_LOG_LEVEL });

	let config;
	try {
		config = loadConfig(process.env);
	} catch (err) {
		if (err instanceof ConfigError) {
			bootstrapLogger.error('config_invalid', { error: err.message });
			process.exit(1);
		}
		throw err;
	}

	const logger = createLogger({ level: config.logLevel });
	const app = createApp({ config, logger });

	const server = app.listen(config.port, () => {
		logger.info('listening', {
			port: config.port,
			orchestrator_url: config.orchestratorUrl,
			api_key_present: Boolean(config.apiKey),
		});
	});

	for (const sig of ['SIGINT', 'SIGTERM']) {
		process.on(sig, () => {
			logger.info('shutdown', { signal: sig });
			server.close(() => process.exit(0));
		});
	}
}

main();
