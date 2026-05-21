import { expect, test, type APIRequestContext } from '@playwright/test';

const ORCHESTRATOR_URL = process.env.ORCHESTRATOR_URL ?? 'http://localhost:8000';
const DROVER_API_KEY = process.env.DROVER_API_KEY ?? 'drover-e2e-test-key';

const AUTH_HEADERS = { Authorization: `Bearer ${DROVER_API_KEY}` };

interface ContainerResponse {
	id: string;
	status: string;
}

async function getContainer(request: APIRequestContext, id: string): Promise<ContainerResponse> {
	const res = await request.get(`${ORCHESTRATOR_URL}/containers/${id}`, { headers: AUTH_HEADERS });
	expect(res.ok(), `GET /containers/${id} returned ${res.status()}`).toBeTruthy();
	return (await res.json()) as ContainerResponse;
}

async function waitForContainerStatus(
	request: APIRequestContext,
	id: string,
	targetStatus: string,
	timeoutMs: number,
): Promise<ContainerResponse> {
	const deadline = Date.now() + timeoutMs;
	let last: ContainerResponse | null = null;
	while (Date.now() < deadline) {
		last = await getContainer(request, id);
		if (last.status === targetStatus) return last;
		await new Promise((resolve) => setTimeout(resolve, 500));
	}
	throw new Error(
		`Container ${id} did not reach status "${targetStatus}" within ${timeoutMs}ms; last status was "${last?.status ?? 'unknown'}"`,
	);
}

async function createPrivilegedContainer(request: APIRequestContext): Promise<string> {
	const res = await request.post(`${ORCHESTRATOR_URL}/containers`, {
		headers: { ...AUTH_HEADERS, 'Content-Type': 'application/json' },
		data: { image: 'builder', privileged: true },
	});
	expect(res.status(), `POST /containers returned ${res.status()}`).toBe(201);
	const body = (await res.json()) as ContainerResponse;
	return body.id;
}

async function stopContainerViaApi(request: APIRequestContext, id: string): Promise<void> {
	const res = await request.post(`${ORCHESTRATOR_URL}/containers/${id}/stop`, { headers: AUTH_HEADERS });
	expect(res.ok(), `POST /containers/${id}/stop returned ${res.status()}`).toBeTruthy();
}

async function destroyContainerViaApi(request: APIRequestContext, id: string): Promise<void> {
	await request.delete(`${ORCHESTRATOR_URL}/containers/${id}`, { headers: AUTH_HEADERS });
}

test.describe.serial('container detail Start button', () => {
	let containerId: string;

	test.afterAll(async ({ request }) => {
		if (containerId) await destroyContainerViaApi(request, containerId);
	});

	test('Start button appears when stopped, and resumes the container when clicked', async ({ page, request }) => {
		containerId = await createPrivilegedContainer(request);
		await waitForContainerStatus(request, containerId, 'running', 30_000);

		// While running, the detail page shows Stop/Destroy but no Start.
		await page.goto(`/views/containers/${containerId}`);
		await expect(page.locator('#container-detail')).toBeVisible();
		await expect(page.locator('.btn-start')).toHaveCount(0);
		await expect(page.locator('.btn-stop')).toHaveCount(1);

		// Stop via the API (the UI Stop button on the detail page lives in
		// its own code path; this test is about the Start button).
		await stopContainerViaApi(request, containerId);
		await waitForContainerStatus(request, containerId, 'stopped', 30_000);

		// After reload the detail page should now show the Start button and
		// no Stop button.
		await page.reload();
		await expect(page.locator('.btn-start')).toHaveCount(1);
		await expect(page.locator('.btn-stop')).toHaveCount(0);
		await expect(page.locator('.status-stopped')).toBeVisible();

		// Click Start. The action route returns HX-Redirect back to the
		// detail page; htmx swaps it to a real navigation.
		await Promise.all([
			page.waitForURL(new RegExp(`/views/containers/${containerId}(?:[?#]|$)`)),
			page.locator('.btn-start').click(),
		]);

		// The orchestrator's resume call returns synchronously with status
		// `running`. Poll the API to confirm the underlying state change,
		// then reload the page to assert the UI now reflects it.
		await waitForContainerStatus(request, containerId, 'running', 30_000);
		await page.reload();
		await expect(page.locator('.btn-start')).toHaveCount(0);
		await expect(page.locator('.btn-stop')).toHaveCount(1);
		await expect(page.locator('.status-running')).toBeVisible();
	});
});
