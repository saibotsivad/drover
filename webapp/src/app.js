import express from 'express';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createOrchestratorClient } from './orchestrator.js';
import { ORCHESTRATOR_PREFIX, createOrchestratorProxy } from './proxy.js';
import { createActionsRouter } from './routes/actions.js';
import { createHealthRouter } from './routes/health.js';
import { createViewsRouter } from './routes/views.js';
import { layout } from './views/layout.js';
import { html } from './views/render.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = path.resolve(__dirname, '..', 'public');

export function createApp({ config, logger, orchestrator }) {
	const orchestratorClient = orchestrator ?? createOrchestratorClient({
		baseUrl: config.orchestratorUrl,
		apiKey: config.apiKey,
	});

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

	app.get('/', (_req, res) => {
		res.type('html').send(layout({
			title: 'Home',
			body: html`<section>
				<p>Drover management UI.</p>
				<p>Use the navigation above to browse containers and images, or to launch a new container.</p>
			</section>`,
		}).toString());
	});

	app.use('/views', createViewsRouter({ orchestrator: orchestratorClient }));
	app.use('/actions', createActionsRouter({ orchestrator: orchestratorClient }));
	app.use(express.static(PUBLIC_DIR, { index: false, fallthrough: true }));

	return app;
}
