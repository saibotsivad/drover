import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { SafeHtml, escape, html, safe } from '../src/views/render.js';

test('escape transforms html-significant characters', () => {
	assert.equal(escape('<script>alert("x")</script>'), '&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;');
	assert.equal(escape("it's"), 'it&#39;s');
	assert.equal(escape('a & b'), 'a &amp; b');
});

test('html tag escapes interpolated values', () => {
	const evil = '<img src=x onerror=alert(1)>';
	const out = html`<p>${evil}</p>`.toString();
	assert.equal(out, '<p>&lt;img src=x onerror=alert(1)&gt;</p>');
});

test('html tag does not double-escape nested SafeHtml', () => {
	const inner = html`<strong>${'safe & sound'}</strong>`;
	const outer = html`<div>${inner}</div>`.toString();
	assert.equal(outer, '<div><strong>safe &amp; sound</strong></div>');
});

test('safe() wraps trusted strings without escaping', () => {
	const trusted = safe('<em>raw</em>');
	const out = html`<p>${trusted}</p>`.toString();
	assert.equal(out, '<p><em>raw</em></p>');
});

test('html renders arrays by joining rendered values', () => {
	const items = ['<a>', 'b', '<c>'];
	const out = html`<ul>${items.map((i) => html`<li>${i}</li>`)}</ul>`.toString();
	assert.equal(out, '<ul><li>&lt;a&gt;</li><li>b</li><li>&lt;c&gt;</li></ul>');
});

test('html omits null, undefined, and false', () => {
	const out = html`<p>${null}${undefined}${false}${0}${''}</p>`.toString();
	assert.equal(out, '<p>0</p>');
});

test('html returns a SafeHtml instance', () => {
	assert.ok(html`<p>x</p>` instanceof SafeHtml);
});
