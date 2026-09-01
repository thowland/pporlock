/**
 * Every document the UI offers to open must exist in the repository.
 *
 * OI-33's lesson applied to links. A guide link is checked by nobody: it opens
 * in a new tab, so a 404 is delivered to the user rather than to the developer,
 * and it is delivered at the moment they were trying to learn how to write a
 * module. Renaming a doc is a one-line change that breaks these silently.
 *
 * This checks the working tree, which is a weaker claim than "checked into
 * git" — but `test_toolchain.py` already holds the packaged file list to the
 * committed tree, and these are files that have been in `docs/` since Sprint 0.
 */
import { describe, expect, it } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { parseRoute } from './router';
import { docUrl, GUIDES, HELP_DOCS, HOMEPAGE, ISSUES, LICENSE } from './about';

const REPO_ROOT = resolve(import.meta.dirname, '../../..');

const everyGuide = [...GUIDES, ...HELP_DOCS];

describe('the documents the UI links to', () => {
  it('links to more than a couple, so the checks below mean something', () => {
    expect(everyGuide.length).toBeGreaterThan(5);
  });

  it.each(everyGuide.map((g) => g.file))('%s is in the repository', (file) => {
    // eslint-disable-next-line security/detect-non-literal-fs-filename
    expect(existsSync(resolve(REPO_ROOT, file))).toBe(true);
  });

  it('builds a GitHub URL that includes the repository path', () => {
    expect(docUrl('docs/module-cookbook.md')).toBe(
      'https://github.com/thowland/pporlock/blob/master/docs/module-cookbook.md',
    );
  });

  it('gives every guide a title and a reason to open it', () => {
    for (const guide of everyGuide) {
      expect(guide.title.length).toBeGreaterThan(0);
      // A link list with no blurbs makes the reader open all six to find one.
      expect(guide.blurb.length).toBeGreaterThan(40);
    }
  });

  it('names one project URL, one issues URL and the licence', () => {
    expect(HOMEPAGE).toBe('https://github.com/thowland/pporlock');
    expect(ISSUES.startsWith(HOMEPAGE)).toBe(true);
    expect(LICENSE).toBe('GPL-3.0-or-later');
  });

  it('agrees with the licence the package declares', async () => {
    // The SPDX identifier appears in five package manifests and the LICENSE
    // file; `make version-check` keeps the versions honest but not this.
    const pkg = (await import('../../package.json')) as unknown as {
      default: { license: string; homepage: string };
    };
    expect(pkg.default.license).toBe(LICENSE);
    expect(pkg.default.homepage).toBe(HOMEPAGE);
  });
});

/**
 * The deep links the extension holds into this app.
 *
 * `extension/src/shared/about.ts` hard-codes `/#/help` and `/#/about` and
 * appends them to the daemon's control origin. Nothing compiles the two
 * together — they are separate builds — so a rename here would ship a working
 * web UI and an about page whose two most useful links land on the traffic
 * table. This is the same shape as OI-16: a published contract nobody codes
 * against.
 */
describe('the routes the extension links into', () => {
  const extensionAbout = (): string => {
    // Test-only, a literal path resolved against this file's own location.
    const path = resolve(import.meta.dirname, '../../../extension/src/shared/about.ts');
    // eslint-disable-next-line security/detect-non-literal-fs-filename
    return readFileSync(path, 'utf8');
  };

  const linked = (): string[] =>
    [...extensionAbout().matchAll(/WEB_UI_[A-Z]+ = '([^']+)'/g)].map((m) => m[1] as string);

  it('found the extension’s links at all', () => {
    expect(linked().length).toBeGreaterThan(1);
  });

  it.each(linked())('%s resolves to a real view, not the traffic fallback', (path) => {
    // The router answers `traffic` for anything it does not recognise, so
    // "falls back" is exactly what a broken deep link looks like.
    expect(parseRoute(path.replace(/^\//, ''))).not.toEqual({ view: 'traffic' });
  });
});
