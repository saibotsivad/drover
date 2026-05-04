import { createProxyMiddleware } from 'http-proxy-middleware';

export const ORCHESTRATOR_PREFIX = '/api/orchestrator';

const SILENT_LOGGER = { info() {}, warn() {}, error() {} };

export function createOrchestratorProxy({ target, apiKey, logger }) {
	return createProxyMiddleware({
		target,
		changeOrigin: true,
		logger: SILENT_LOGGER,
		on: {
			proxyReq: (proxyReq) => {
				if (apiKey) {
					proxyReq.setHeader('Authorization', `Bearer ${apiKey}`);
				}
			},
			error: (err, _req, res) => {
				logger.error('proxy_error', { error: err.message });
				if (!res || typeof res.writeHead !== 'function') return;
				if (res.headersSent || res.writableEnded) return;
				res.writeHead(502, { 'Content-Type': 'application/json' });
				res.end(JSON.stringify({ error: 'orchestrator_unreachable' }));
			},
		},
	});
}
