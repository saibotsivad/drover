const LEVELS = { debug: 10, info: 20, warn: 30, error: 40 };

function resolveLevel(name) {
	const value = LEVELS[String(name || '').toLowerCase()];
	return value ?? LEVELS.info;
}

export function createLogger({ level = 'info', stream = process.stdout, errorStream = process.stderr, now = () => new Date() } = {}) {
	const threshold = resolveLevel(level);

	function emit(levelName, message, fields) {
		const levelValue = LEVELS[levelName];
		if (levelValue < threshold) return;
		const record = { ts: now().toISOString(), level: levelName, msg: message };
		if (fields && typeof fields === 'object') Object.assign(record, fields);
		const line = JSON.stringify(record) + '\n';
		const target = levelValue >= LEVELS.error ? errorStream : stream;
		target.write(line);
	}

	const logger = {
		debug: (msg, fields) => emit('debug', msg, fields),
		info: (msg, fields) => emit('info', msg, fields),
		warn: (msg, fields) => emit('warn', msg, fields),
		error: (msg, fields) => emit('error', msg, fields),
	};

	logger.requestMiddleware = function requestLogger(req, res, next) {
		const start = process.hrtime.bigint();
		res.on('finish', () => {
			const durationMs = Number(process.hrtime.bigint() - start) / 1_000_000;
			logger.info('request', {
				method: req.method,
				path: req.originalUrl || req.url,
				status: res.statusCode,
				duration_ms: Math.round(durationMs * 1000) / 1000,
			});
		});
		next();
	};

	return logger;
}
