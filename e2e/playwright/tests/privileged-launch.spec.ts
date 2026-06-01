import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

const ORCHESTRATOR_URL = process.env.ORCHESTRATOR_URL ?? 'http://localhost:8000';
const DROVER_API_KEY = process.env.DROVER_API_KEY ?? 'drover-e2e-test-key';

const AUTH_HEADERS = { Authorization: `Bearer ${DROVER_API_KEY}` };

interface WorkerResponse {
	id: string;
	status: string;
}

async function getWorker(request: APIRequestContext, id: string): Promise<WorkerResponse> {
	const res = await request.get(`${ORCHESTRATOR_URL}/workers/${id}`, { headers: AUTH_HEADERS });
	expect(res.ok(), `GET /workers/${id} returned ${res.status()}`).toBeTruthy();
	return (await res.json()) as WorkerResponse;
}

async function waitForWorkerStatus(
	request: APIRequestContext,
	id: string,
	targetStatus: string,
	timeoutMs: number,
): Promise<WorkerResponse> {
	const deadline = Date.now() + timeoutMs;
	let last: WorkerResponse | null = null;
	while (Date.now() < deadline) {
		last = await getWorker(request, id);
		if (last.status === targetStatus) return last;
		await new Promise((resolve) => setTimeout(resolve, 500));
	}
	throw new Error(
		`Worker ${id} did not reach status "${targetStatus}" within ${timeoutMs}ms; last status was "${last?.status ?? 'unknown'}"`,
	);
}

async function listLogFiles(request: APIRequestContext, id: string): Promise<string[]> {
	const res = await request.get(`${ORCHESTRATOR_URL}/workers/${id}/logs/files`, {
		headers: AUTH_HEADERS,
	});
	if (!res.ok()) return [];
	const body = await res.json();
	return Array.isArray(body) ? (body as string[]) : [];
}

async function waitForLogFile(
	request: APIRequestContext,
	id: string,
	filename: string,
	timeoutMs: number,
): Promise<void> {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		const files = await listLogFiles(request, id);
		if (files.includes(filename)) return;
		await new Promise((resolve) => setTimeout(resolve, 500));
	}
	throw new Error(`Log file "${filename}" did not appear for worker ${id} within ${timeoutMs}ms`);
}

async function waitForExecComplete(page: Page, timeoutMs: number): Promise<void> {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if (await page.locator('#exec-detail .status-complete').count() > 0) return;
		await new Promise((resolve) => setTimeout(resolve, 500));
		await page.reload();
	}
	throw new Error(`Exec did not reach status "complete" within ${timeoutMs}ms`);
}

async function waitForLogViewerAttached(page: Page, timeoutMs: number): Promise<void> {
	// The worker detail page renders #log-viewer only when the
	// configured log source has non-empty content. We reload the page
	// between polls because logs are fetched server-side on render.
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if (await page.locator('#log-viewer').count() > 0) return;
		await new Promise((resolve) => setTimeout(resolve, 500));
		await page.reload();
	}
	throw new Error(`#log-viewer did not attach within ${timeoutMs}ms`);
}

// Tests 2–5 share a single launched worker. The `e2e/run.sh down` step
// already cleans up every `drover.managed=true` container, so no afterAll
// teardown is needed here — matches the existing bash suite.
test.describe.serial('privileged worker UI flows', () => {
	let workerId: string;

	test('launches a privileged worker and execs a shell echo', async ({ page, request }) => {
		await page.goto('/views/launch');
		await expect(page.locator('#launch-form')).toBeVisible();

		await page.locator('#image').selectOption('builder');
		await page.locator('#privileged').check();
		await page.locator('#env').fill('DROVER_TEST_VAR=hello_playwright');

		// HX-Redirect arrives as a 201 with a header — htmx triggers
		// window.location, so wait on the URL change explicitly.
		await Promise.all([
			page.waitForURL(/\/views\/workers\/[^/]+$/),
			page.locator('#launch-form button[type="submit"]').click(),
		]);

		const match = page.url().match(/\/views\/workers\/([^/?#]+)/);
		expect(match, `launch should redirect to /views/workers/<id>, got ${page.url()}`).not.toBeNull();
		workerId = match![1];

		await waitForWorkerStatus(request, workerId, 'running', 30_000);
		await page.reload();
		await expect(page.locator('#worker-detail')).toBeVisible();

		await page.locator('.exec-input-form textarea[name="command"]').fill('echo $DROVER_TEST_VAR');
		await page.locator('.exec-input-form button[type="submit"]').click();

		// Wait for the first real (keyed) command row to attach. The
		// keyed-id pattern filters out the <tr class="empty"> placeholder
		// that hangs around until the htmx swap completes.
		const firstCommandRow = page.locator('#command-rows tr[id^="command-"]').first();
		await expect(firstCommandRow).toBeAttached({ timeout: 15_000 });

		const commandRowId = await firstCommandRow.getAttribute('id');
		expect(commandRowId).toMatch(/^command-/);
		const commandId = commandRowId!.slice('command-'.length);

		await firstCommandRow.locator('a').first().click();
		await page.waitForURL(new RegExp(`/views/workers/${workerId}/execs/${commandId}`));

		await waitForExecComplete(page, 15_000);

		await expect(page.locator('pre#exec-output')).toContainText('hello_playwright');
		await expect(page.locator('#exec-meta')).toContainText('0');
	});

	test('exec docker container ls confirms privileged docker socket access', async ({ page }) => {
		expect(workerId, 'workerId must be set by the launch test').toBeTruthy();

		// Capture the existing top command-id so we can detect the new row
		// when it arrives — the table is newest-first, so the new exec
		// becomes the first matching tr after submission.
		await page.goto(`/views/workers/${workerId}`);
		const existingTopRow = page.locator('#command-rows tr[id^="command-"]').first();
		const existingTopId = (await existingTopRow.count()) > 0
			? await existingTopRow.getAttribute('id')
			: null;

		await page.locator('.exec-input-form textarea[name="command"]').fill('docker container ls');
		await page.locator('.exec-input-form button[type="submit"]').click();

		// Wait until the first matching row has a different id than the
		// pre-existing top row (i.e. the new exec is in place).
		await expect
			.poll(async () => {
				const row = page.locator('#command-rows tr[id^="command-"]').first();
				if ((await row.count()) === 0) return null;
				return await row.getAttribute('id');
			}, { timeout: 15_000 })
			.not.toBe(existingTopId);

		const newTopRow = page.locator('#command-rows tr[id^="command-"]').first();
		const newRowId = await newTopRow.getAttribute('id');
		const commandId = newRowId!.slice('command-'.length);

		await newTopRow.locator('a').first().click();
		await page.waitForURL(new RegExp(`/views/workers/${workerId}/execs/${commandId}`));

		await waitForExecComplete(page, 30_000);

		await expect(page.locator('pre#exec-output')).toContainText('CONTAINER ID');
		await expect(page.locator('#exec-meta')).toContainText('0');
	});

	test('launched worker appears in the workers list', async ({ page }) => {
		expect(workerId, 'workerId must be set by the launch test').toBeTruthy();

		await page.goto('/views/workers');
		await expect(page.locator('#workers-list')).toBeVisible();

		const row = page.locator(`#workers-list tbody#worker-rows #worker-${cssEscape(workerId)}`);
		await expect(row).toBeVisible();

		const statusBadge = row.locator('.status-running, .status-initializing');
		await expect(statusBadge).toHaveCount(1);
	});

	test('log viewer renders content for each log source', async ({ page, request }) => {
		expect(workerId, 'workerId must be set by the launch test').toBeTruthy();

		// 1. Live source (default).
		await page.goto(`/views/workers/${workerId}`);
		await waitForLogViewerAttached(page, 10_000);
		await expect(page.locator('pre#log-viewer')).toContainText('Connecting to /var/run/drover/sockets/orchestrator.sock');

		// 2. File source — wait for 0.log to be available before switching.
		await waitForLogFile(request, workerId, '0.log', 15_000);
		await page.reload();
		await page.locator('select.log-source-select').selectOption('file:0.log');
		await page.waitForURL(/log_source=file%3A0\.log/);
		await waitForLogViewerAttached(page, 10_000);
		await expect(page.locator('pre#log-viewer')).toContainText('Connecting to');

		// 3. Orchestrator source — endpoint substring-matches on the
		// worker ID, so at least one orchestrator log line mentioning
		// the worker must have been written by now. Verify the API
		// is healthy first so a 503 (orchestrator could not detect its
		// own Docker container id — see _detect_own_container_id) fails
		// with a clearer message than the UI's empty-state placeholder.
		await waitForOrchestratorLogContent(request, workerId, 15_000);
		await page.locator('select.log-source-select').selectOption('orchestrator');
		await page.waitForURL(/log_source=orchestrator/);
		await waitForLogViewerAttached(page, 10_000);
		await expect(page.locator('pre#log-viewer')).toContainText(workerId);
	});
});

async function waitForOrchestratorLogContent(
	request: APIRequestContext,
	id: string,
	timeoutMs: number,
): Promise<void> {
	const deadline = Date.now() + timeoutMs;
	let lastStatus = 0;
	while (Date.now() < deadline) {
		const res = await request.get(`${ORCHESTRATOR_URL}/workers/${id}/logs/orchestrator`, {
			headers: AUTH_HEADERS,
		});
		lastStatus = res.status();
		if (res.status() === 503) {
			throw new Error(
				`GET /workers/${id}/logs/orchestrator returned 503 — the orchestrator could not detect its own Docker container id. ` +
				'Check orchestrator startup logs for "Could not detect orchestrator\'s own Docker container ID" and verify the cgroup layout.',
			);
		}
		if (res.ok()) {
			const body = await res.text();
			if (body.length > 0) return;
		}
		await new Promise((resolve) => setTimeout(resolve, 500));
	}
	throw new Error(
		`Orchestrator log endpoint never returned non-empty content for worker ${id} within ${timeoutMs}ms (last status ${lastStatus}).`,
	);
}

function cssEscape(value: string): string {
	if (typeof (globalThis as { CSS?: { escape?: (v: string) => string } }).CSS?.escape === 'function') {
		return (globalThis as unknown as { CSS: { escape: (v: string) => string } }).CSS.escape(value);
	}
	return value.replace(/[^a-zA-Z0-9_-]/g, (ch) => `\\${ch}`);
}
