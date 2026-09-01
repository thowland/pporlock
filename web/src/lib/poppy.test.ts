/**
 * The mark in the header is the mark on the toolbar.
 *
 * The point of putting the poppy in the header is that it is the *same* poppy —
 * a user who has learned to find pporlock by its icon should recognise the page
 * as the same thing. Two copies of an SVG is exactly the arrangement where that
 * quietly stops being true: someone tweaks a petal in one of them, both files
 * still look like a poppy, and nothing anywhere disagrees.
 *
 * They are two files rather than one because the web UI and the extension are
 * separate builds that must not reach into each other's trees — a Vite import
 * across package boundaries couples two build graphs to save one copy. So the
 * copy is allowed and the divergence is not.
 *
 * Byte equality rather than a parsed comparison: the artwork has no meaningful
 * normal form, and "these are the same file" is the claim actually being made.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const REPO_ROOT = resolve(import.meta.dirname, '../../..');

/** Test-only; both paths are literals resolved against the repository root. */
function read(relative: string): string {
  const path = resolve(REPO_ROOT, relative);
  // eslint-disable-next-line security/detect-non-literal-fs-filename
  return readFileSync(path, 'utf8');
}

const WEB = 'web/public/poppy.svg';
const EXTENSION = 'extension/public/icons/poppy.svg';

describe('the poppy mark', () => {
  it('is the same file the extension ships', () => {
    expect(read(WEB)).toBe(read(EXTENSION));
  });

  it('is really artwork, so the comparison above is not comparing two blanks', () => {
    const svg = read(WEB);
    expect(svg).toContain('<svg');
    // Four petals, drawn as one path rotated about the centre.
    expect(svg.match(/<path/g)?.length).toBe(4);
    expect(svg.length).toBeGreaterThan(500);
  });

  it('carries no script and no external reference', () => {
    // It is served from the same origin as the UI and rendered in an <img>,
    // which cannot run script — but the UI's standing rule is that nothing it
    // serves reaches off the machine, and an SVG is a document.
    const svg = read(WEB);
    expect(svg).not.toMatch(/<script/i);
    expect(svg).not.toMatch(/https?:\/\/(?!www\.w3\.org)/i);
  });
});
