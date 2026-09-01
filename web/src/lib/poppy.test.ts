/**
 * Every poppy in this repository is the same poppy.
 *
 * The mark is now in four places — the extension's toolbar icon, the web UI's
 * header and favicon, and the README — and the whole point of putting it in all
 * of them is that a user who has learned to find pporlock by its icon
 * recognises the page and the project page as the same thing. Copies of an SVG
 * are exactly the arrangement where that quietly stops being true: someone
 * tweaks a petal in one of them, every file still looks like a poppy, and
 * nothing anywhere disagrees.
 *
 * They are copies rather than one file because the three consumers are
 * independent — the web UI and the extension are separate builds that must not
 * reach into each other's trees, and GitHub renders the README from the
 * repository, not from anyone's `dist`. A Vite import across package boundaries
 * couples two build graphs to save a 1.5 kB file. So the copy is allowed and
 * the divergence is not.
 *
 * The set is *discovered* rather than listed, so a fifth copy added later is
 * covered by this without anyone remembering to come back here.
 *
 * Byte equality rather than a parsed comparison: the artwork has no meaningful
 * normal form, and "these are the same file" is the claim actually being made.
 */
import { describe, expect, it } from 'vitest';
import { readdirSync, readFileSync } from 'node:fs';
import { relative, resolve } from 'node:path';

const REPO_ROOT = resolve(import.meta.dirname, '../../..');

/** Directories with no source of truth in them, and a lot of files. */
const SKIP = new Set(['node_modules', '.git', 'dist', 'coverage', 'test-results', '.venv']);

/** Test-only; the walk starts at the repository root resolved at import. */
function findPoppies(dir: string = REPO_ROOT): string[] {
  const found: string[] = [];
  // eslint-disable-next-line security/detect-non-literal-fs-filename
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (SKIP.has(entry.name)) continue;
    const path = resolve(dir, entry.name);
    if (entry.isDirectory()) found.push(...findPoppies(path));
    else if (entry.name === 'poppy.svg') found.push(relative(REPO_ROOT, path));
  }
  return found.sort();
}

function read(relativePath: string): string {
  const path = resolve(REPO_ROOT, relativePath);
  // eslint-disable-next-line security/detect-non-literal-fs-filename
  return readFileSync(path, 'utf8');
}

const copies = findPoppies();

describe('the poppy mark', () => {
  it('is in every place that shows it', () => {
    // Named so a deletion is a failure rather than a silently smaller set: the
    // comparison below passes trivially on one file, or on none.
    expect(copies).toEqual([
      'docs/images/poppy.svg',
      'extension/public/icons/poppy.svg',
      'web/public/poppy.svg',
    ]);
  });

  it.each(copies)('%s is identical to the others', (copy) => {
    expect(read(copy)).toBe(read(copies[0] as string));
  });

  it('is really artwork, so the comparison above is not comparing blanks', () => {
    const svg = read(copies[0] as string);
    expect(svg).toContain('<svg');
    // Four petals, drawn as one path rotated about the centre.
    expect(svg.match(/<path/g)?.length).toBe(4);
    expect(svg.length).toBeGreaterThan(500);
  });

  it('carries no script and no external reference', () => {
    // It is served from the same origin as the UI and rendered in an <img>,
    // which cannot run script — but the UI's standing rule is that nothing it
    // serves reaches off the machine, and an SVG is a document. GitHub
    // sanitises what it renders; this repository does not rely on that.
    const svg = read(copies[0] as string);
    expect(svg).not.toMatch(/<script/i);
    expect(svg).not.toMatch(/https?:\/\/(?!www\.w3\.org)/i);
  });

  it('is the file the README actually points at', () => {
    // A README image is rendered by GitHub from a path in the repository, and a
    // broken one shows as the project's first impression. Nothing else in the
    // build checks it: the README is not compiled, linted or served.
    const readme = read('README.md');
    const referenced = [...readme.matchAll(/src="([^"]*poppy\.svg)"/g)].map((m) => m[1] as string);
    expect(referenced.length).toBeGreaterThan(0);
    for (const path of referenced) {
      expect(copies).toContain(path);
    }
  });
});
