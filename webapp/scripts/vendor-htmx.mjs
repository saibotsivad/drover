#!/usr/bin/env node
import { copyFile, mkdir, stat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const SRC = path.join(ROOT, 'node_modules', 'htmx.org', 'dist', 'htmx.min.js');
const DST = path.join(ROOT, 'public', 'vendor', 'htmx.min.js');

async function main() {
	let info;
	try {
		info = await stat(SRC);
	} catch {
		throw new Error(
			`htmx.min.js not found at ${path.relative(ROOT, SRC)}. ` +
			`Run "npm install" first.`,
		);
	}

	await mkdir(path.dirname(DST), { recursive: true });
	await copyFile(SRC, DST);
	process.stdout.write(`copied ${path.relative(ROOT, SRC)} → ${path.relative(ROOT, DST)} (${info.size} bytes)\n`);
}

main().catch((err) => {
	process.stderr.write(`vendor-htmx: ${err.message}\n`);
	process.exit(1);
});
