#!/usr/bin/env node
import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const LOCK_PATH = path.join(ROOT, 'vendor.lock.json');
const OUT_PATH = path.join(ROOT, 'public', 'vendor', 'htmx.min.js');

async function main() {
	const lock = JSON.parse(await readFile(LOCK_PATH, 'utf8'));
	const entry = lock.htmx;
	if (!entry) throw new Error('vendor.lock.json missing "htmx" entry');
	const { version, source, sha256 } = entry;
	if (!version || !source || !sha256) {
		throw new Error('vendor.lock.json "htmx" entry must have version, source, sha256');
	}

	const url = source.replaceAll('${version}', version);
	process.stdout.write(`fetching htmx ${version} from ${url}\n`);

	const res = await fetch(url);
	if (!res.ok) throw new Error(`download failed: ${res.status} ${res.statusText}`);
	const buf = Buffer.from(await res.arrayBuffer());

	const actual = createHash('sha256').update(buf).digest('hex');
	if (actual !== sha256) {
		throw new Error(
			`sha256 mismatch for htmx ${version}\n` +
			`  expected: ${sha256}\n` +
			`  actual:   ${actual}\n` +
			`If you intended to update htmx, edit ${path.relative(ROOT, LOCK_PATH)}.`,
		);
	}

	await mkdir(path.dirname(OUT_PATH), { recursive: true });
	await writeFile(OUT_PATH, buf);
	process.stdout.write(`wrote ${path.relative(ROOT, OUT_PATH)} (${buf.length} bytes, sha256=${actual})\n`);
}

main().catch((err) => {
	process.stderr.write(`vendor-htmx: ${err.message}\n`);
	process.exit(1);
});
