import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { ConfigError, loadConfig } from '../src/config.js';

test('loadConfig returns defaults when only orchestrator URL is set', () => {
	const config = loadConfig({ DROVER_ORCHESTRATOR_URL: 'http://orchestrator:8000' });
	assert.equal(config.orchestratorUrl, 'http://orchestrator:8000');
	assert.equal(config.apiKey, null);
	assert.equal(config.port, 8080);
	assert.equal(config.logLevel, 'info');
});

test('loadConfig reads optional values', () => {
	const config = loadConfig({
		DROVER_ORCHESTRATOR_URL: 'http://orchestrator:8000',
		DROVER_API_KEY: 'secret-token',
		PORT: '9999',
		DROVER_LOG_LEVEL: 'DEBUG',
	});
	assert.equal(config.apiKey, 'secret-token');
	assert.equal(config.port, 9999);
	assert.equal(config.logLevel, 'debug');
});

test('loadConfig throws when orchestrator URL is missing', () => {
	assert.throws(() => loadConfig({}), (err) => err instanceof ConfigError);
});

test('loadConfig throws when orchestrator URL is empty/whitespace', () => {
	assert.throws(() => loadConfig({ DROVER_ORCHESTRATOR_URL: '   ' }), (err) => err instanceof ConfigError);
});

test('loadConfig throws when orchestrator URL is malformed', () => {
	assert.throws(
		() => loadConfig({ DROVER_ORCHESTRATOR_URL: 'not a url' }),
		(err) => err instanceof ConfigError,
	);
});

test('loadConfig throws when PORT is not a valid port', () => {
	assert.throws(
		() => loadConfig({ DROVER_ORCHESTRATOR_URL: 'http://x', PORT: '0' }),
		(err) => err instanceof ConfigError,
	);
	assert.throws(
		() => loadConfig({ DROVER_ORCHESTRATOR_URL: 'http://x', PORT: '70000' }),
		(err) => err instanceof ConfigError,
	);
	assert.throws(
		() => loadConfig({ DROVER_ORCHESTRATOR_URL: 'http://x', PORT: 'abc' }),
		(err) => err instanceof ConfigError,
	);
});
