import express from 'express';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHealthRouter } from './routes/health.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = path.resolve(__dirname, '..', 'public');

export function createApp({ logger }) {
	const app = express();
	app.disable('x-powered-by');

	app.use(logger.requestMiddleware);
	app.use(createHealthRouter());
	app.use(express.static(PUBLIC_DIR, { index: 'index.html', fallthrough: true }));

	return app;
}
