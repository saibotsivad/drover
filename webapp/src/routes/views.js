import { Router } from 'express';
import { layout } from '../views/layout.js';
import { html } from '../views/render.js';

function send(res, page) {
	res.type('html').send(page.toString());
}

function stub(message) {
	return html`<section class="stub">
		<p>${message}</p>
	</section>`;
}

export function createViewsRouter() {
	const router = Router();

	router.get('/containers', (req, res) => {
		send(res, layout({
			title: 'Containers',
			activePath: '/views/containers',
			body: stub('Container list — coming soon.'),
		}));
	});

	router.get('/containers/:id', (req, res) => {
		send(res, layout({
			title: `Container ${req.params.id}`,
			activePath: '/views/containers',
			body: html`<section class="stub">
				<h2>Container ${req.params.id}</h2>
				<p>Container detail — coming soon.</p>
			</section>`,
		}));
	});

	router.get('/images', (req, res) => {
		send(res, layout({
			title: 'Images',
			activePath: '/views/images',
			body: stub('Image list — coming soon.'),
		}));
	});

	router.get('/launch', (req, res) => {
		send(res, layout({
			title: 'Launch',
			activePath: '/views/launch',
			body: stub('Launch form — coming soon.'),
		}));
	});

	return router;
}
