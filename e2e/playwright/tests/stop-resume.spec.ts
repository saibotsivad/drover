import { expect, test, type APIRequestContext } from '@playwright/test';

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

async function createPrivilegedWorker(request: APIRequestContext): Promise<string> {
	const res = await request.post(`${ORCHESTRATOR_URL}/workers`, {
		headers: { ...AUTH_HEADERS, 'Content-Type': 'application/json' },
		data: { image: 'builder', privileged: true },
	});
	expect(res.status(), `POST /workers returned ${res.status()}`).toBe(201);
	const body = (await res.json()) as WorkerResponse;
	return body.id;
}

async function stopWorkerViaApi(request: APIRequestContext, id: string): Promise<void> {
	const res = await request.post(`${ORCHESTRATOR_URL}/workers/${id}/stop`, { headers: AUTH_HEADERS });
	expect(res.ok(), `POST /workers/${id}/stop returned ${res.status()}`).toBeTruthy();
}

async function destroyWorkerViaApi(request: APIRequestContext, id: string): Promise<void> {
	await request.delete(`${ORCHESTRATOR_URL}/workers/${id}`, { headers: AUTH_HEADERS });
}

test.describe.serial('worker detail Resume button', () => {
	let workerId: string;

	test.afterAll(async ({ request }) => {
		if (workerId) await destroyWorkerViaApi(request, workerId);
	});

	test('Resume button appears when stopped, and brings the worker back to running when clicked', async ({ page, request }) => {
		workerId = await createPrivilegedWorker(request);
		await waitForWorkerStatus(request, workerId, 'running', 30_000);

		// While running, the detail page shows Stop/Destroy but no Resume.
		await page.goto(`/views/workers/${workerId}`);
		await expect(page.locator('#worker-detail')).toBeVisible();
		await expect(page.locator('.btn-resume')).toHaveCount(0);
		await expect(page.locator('.btn-stop')).toHaveCount(1);

		// Stop via the API (the UI Stop button on the detail page lives in
		// its own code path; this test is about the Resume button).
		await stopWorkerViaApi(request, workerId);
		await waitForWorkerStatus(request, workerId, 'stopped', 30_000);

		// After reload the detail page should now show the Resume button and
		// no Stop button.
		await page.reload();
		await expect(page.locator('.btn-resume')).toHaveCount(1);
		await expect(page.locator('.btn-stop')).toHaveCount(0);
		await expect(page.locator('.status-stopped')).toBeVisible();

		// Click Resume. The action route returns HX-Redirect back to the
		// detail page; htmx swaps it to a real navigation.
		await Promise.all([
			page.waitForURL(new RegExp(`/views/workers/${workerId}(?:[?#]|$)`)),
			page.locator('.btn-resume').click(),
		]);

		// After resume the orchestrator returns immediately with status
		// `resuming` and only flips to `running` once the worker agent
		// reconnects and sends ready.  Poll the API for the full
		// transition, then reload the page to assert the UI matches.
		await waitForWorkerStatus(request, workerId, 'running', 30_000);
		await page.reload();
		await expect(page.locator('.btn-resume')).toHaveCount(0);
		await expect(page.locator('.btn-stop')).toHaveCount(1);
		await expect(page.locator('.status-running')).toBeVisible();
	});
});
