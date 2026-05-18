import { expect, test } from '@playwright/test';

const ORCHESTRATOR_URL = process.env.ORCHESTRATOR_URL ?? 'http://localhost:8000';
const DROVER_API_KEY = process.env.DROVER_API_KEY ?? 'drover-e2e-test-key';

interface OrchestratorImage {
	name: string;
}

test.describe('images list view', () => {
	test('renders one row per orchestrator image', async ({ page, request }) => {
		const apiResponse = await request.get(`${ORCHESTRATOR_URL}/images`, {
			headers: { Authorization: `Bearer ${DROVER_API_KEY}` },
		});
		expect(apiResponse.ok(), `GET /images returned ${apiResponse.status()}`).toBeTruthy();
		const images = (await apiResponse.json()) as OrchestratorImage[];
		expect(Array.isArray(images)).toBeTruthy();
		expect(images.length, 'orchestrator should report at least one drover-managed image').toBeGreaterThan(0);

		await page.goto('/views/images');

		const imagesList = page.locator('#images-list');
		await expect(imagesList).toBeVisible();

		for (const image of images) {
			await expect(
				imagesList.locator(`#image-${cssEscape(image.name)}`),
				`row for image "${image.name}" should be present`,
			).toBeVisible();
		}

		await expect(page.locator('tbody#image-rows tr')).toHaveCount(images.length);

		// In the e2e stack the only managed image is `builder`; assert it
		// explicitly so a misconfigured stack fails loudly here rather than
		// in the launch test downstream.
		await expect(page.locator('#image-builder')).toBeVisible();
	});
});

// Image names in this stack are already safe CSS identifiers (slug
// characters only), but escape defensively so an arbitrary name from
// the orchestrator can't break the selector.
function cssEscape(value: string): string {
	if (typeof (globalThis as { CSS?: { escape?: (v: string) => string } }).CSS?.escape === 'function') {
		return (globalThis as unknown as { CSS: { escape: (v: string) => string } }).CSS.escape(value);
	}
	return value.replace(/[^a-zA-Z0-9_-]/g, (ch) => `\\${ch}`);
}
