import { strict as assert } from 'node:assert';
import { EventEmitter } from 'node:events';
import { test } from 'node:test';
import { Writable } from 'node:stream';
import { createLogger } from '../src/logger.js';

class CapturingStream extends Writable {
	constructor() {
		super();
		this.chunks = [];
	}
	_write(chunk, _enc, cb) {
		this.chunks.push(chunk.toString('utf8'));
		cb();
	}
	get text() {
		return this.chunks.join('');
	}
	get records() {
		return this.text
			.split('\n')
			.filter(Boolean)
			.map((line) => JSON.parse(line));
	}
}

function makeStreams() {
	return { stream: new CapturingStream(), errorStream: new CapturingStream() };
}

test('emits structured JSON lines for info/warn/error', () => {
	const { stream, errorStream } = makeStreams();
	const logger = createLogger({ level: 'debug', stream, errorStream });
	logger.info('hello', { foo: 'bar' });
	logger.warn('careful');
	logger.error('boom', { code: 500 });

	const records = [...stream.records, ...errorStream.records];
	assert.equal(records.length, 3);
	assert.equal(records[0].level, 'info');
	assert.equal(records[0].msg, 'hello');
	assert.equal(records[0].foo, 'bar');
	assert.match(records[0].ts, /^\d{4}-\d{2}-\d{2}T/);
	assert.equal(records[1].level, 'warn');
	assert.equal(records[2].level, 'error');
	assert.equal(records[2].code, 500);
});

test('respects log level threshold', () => {
	const { stream, errorStream } = makeStreams();
	const logger = createLogger({ level: 'warn', stream, errorStream });
	logger.debug('skip');
	logger.info('skip');
	logger.warn('keep');
	logger.error('keep');

	assert.equal(stream.records.length, 1);
	assert.equal(errorStream.records.length, 1);
	assert.equal(stream.records[0].msg, 'keep');
	assert.equal(errorStream.records[0].msg, 'keep');
});

test('routes errors to the error stream', () => {
	const { stream, errorStream } = makeStreams();
	const logger = createLogger({ level: 'debug', stream, errorStream });
	logger.info('to-stdout');
	logger.error('to-stderr');
	assert.equal(stream.records.length, 1);
	assert.equal(errorStream.records.length, 1);
});

function fakeReqRes({ method = 'POST', url = '/workers', headers = {}, body, status = 200 } = {}) {
	const req = { method, url, originalUrl: url, headers, body };
	const res = new EventEmitter();
	res.statusCode = status;
	return { req, res };
}

test('request logger never includes request bodies or auth headers', () => {
	const { stream, errorStream } = makeStreams();
	const logger = createLogger({ level: 'debug', stream, errorStream });
	const { req, res } = fakeReqRes({
		method: 'POST',
		url: '/api/orchestrator/containers',
		headers: {
			authorization: 'Bearer super-secret-token-value',
			cookie: 'session=cookie-value',
		},
		body: { env: { DROVER_AUTO_GIT_TOKEN: 'ghp_supersecretvalue' } },
		status: 201,
	});

	logger.requestMiddleware(req, res, () => {});
	res.emit('finish');

	const text = stream.text + errorStream.text;
	assert.ok(text.length > 0, 'expected at least one log line');
	assert.equal(text.includes('super-secret-token-value'), false, 'authorization token leaked');
	assert.equal(text.includes('Bearer'), false, 'authorization scheme leaked');
	assert.equal(text.includes('ghp_supersecretvalue'), false, 'request body secret leaked');
	assert.equal(text.includes('DROVER_AUTO_GIT_TOKEN'), false, 'request body field name leaked');
	assert.equal(text.includes('cookie-value'), false, 'cookie value leaked');

	const record = stream.records.find((r) => r.msg === 'request');
	assert.ok(record, 'expected a request log record');
	assert.equal(record.method, 'POST');
	assert.equal(record.path, '/api/orchestrator/containers');
	assert.equal(record.status, 201);
	assert.equal(typeof record.duration_ms, 'number');
});

test('request logger does not include response bodies', () => {
	const { stream, errorStream } = makeStreams();
	const logger = createLogger({ level: 'debug', stream, errorStream });
	const { req, res } = fakeReqRes({ method: 'GET', url: '/health', status: 200 });
	res.body = { healthy: true, secretInResponse: 'leaked-if-logged' };

	logger.requestMiddleware(req, res, () => {});
	res.emit('finish');

	const text = stream.text + errorStream.text;
	assert.equal(text.includes('leaked-if-logged'), false, 'response body leaked');
	assert.equal(text.includes('secretInResponse'), false, 'response body field name leaked');
});
