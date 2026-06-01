import { html } from './render.js';

const NAV_ITEMS = [
	{ href: '/views/workers', label: 'Workers' },
	{ href: '/views/images', label: 'Images' },
	{ href: '/views/launch', label: 'Launch' },
];

export function layout({ title = 'Drover', activePath = null, body }) {
	return html`<!doctype html>
<html lang="en">
	<head>
		<meta charset="utf-8" />
		<meta name="viewport" content="width=device-width, initial-scale=1" />
		<title>${title} — Drover</title>
		<link rel="stylesheet" href="/css/styles.css" />
		<script src="/vendor/htmx.min.js" defer></script>
	</head>
	<body>
		<header>
			<h1><a href="/">Drover</a></h1>
			<nav>${NAV_ITEMS.map(
				(item) => html`<a
					href="${item.href}"
					class="${item.href === activePath ? 'active' : ''}"
				>${item.label}</a>`,
			)}</nav>
		</header>
		<main id="content">${body}</main>
	</body>
</html>
`;
}
