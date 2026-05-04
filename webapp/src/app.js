import express from 'express';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { ORCHESTRATOR_PREFIX, createOrchestratorProxy } from './proxy.js';
import { createHealthRouter } from './routes/health.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = path.resolve(__dirname, '..', 'public');

export function createApp({ config, logger }) {
	const app = express();
	app.disable('x-powered-by');

	app.use(logger.requestMiddleware);
	app.use(createHealthRouter());
	app.use(
		ORCHESTRATOR_PREFIX,
		createOrchestratorProxy({
			target: config.orchestratorUrl,
			apiKey: config.apiKey,
			logger,
		}),
	);
	app.use(express.static(PUBLIC_DIR, { index: 'index.html', fallthrough: true }));

	return app;
}
