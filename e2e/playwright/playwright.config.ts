import { defineConfig, devices } from '@playwright/test';

const webappUrl = process.env.WEBAPP_URL ?? 'http://localhost:9091';

export default defineConfig({
	testDir: './tests',
	outputDir: './results/test-results',
	fullyParallel: false,
	forbidOnly: !!process.env.CI,
	retries: 0,
	workers: 1,
	reporter: [
		['list'],
		['html', { outputFolder: './results/html-report', open: 'never' }],
	],
	use: {
		baseURL: webappUrl,
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure',
		video: 'retain-on-failure',
	},
	projects: [
		{
			name: 'chromium',
			use: { ...devices['Desktop Chrome'] },
		},
	],
});
