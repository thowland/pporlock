/**
 * Every HTML page the extension can navigate to must actually be built.
 *
 * OI-28. `devtools.ts` registers the panel with
 * `chrome.devtools.panels.create(..., 'src/devtools/panel.html')`, and that path
 * appears nowhere else — not in the manifest, not in an import. CRXJS discovers
 * pages by walking the manifest, so nothing knew `panel.html` existed and it was
 * never emitted.
 *
 * The build succeeded. The extension loaded. Chrome created a DevTools tab named
 * "pporlock" pointing at a file that was not there, and rendered it blank with
 * no error in any console. Every test passed throughout, because every test
 * imported the panel's *source* — which is present and correct — rather than
 * asking whether the build produces it.
 *
 * A page referenced only from a string inside JavaScript is invisible to every
 * tool in the chain. This test is the one thing that can see it.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const EXT_ROOT = resolve(import.meta.dirname, '..');

function read(relative: string): string {
  /* Test-only. Every argument is a literal written in this file, resolved
     against the extension root computed at import; no input reaches it. */
  // eslint-disable-next-line security/detect-non-literal-fs-filename
  return readFileSync(resolve(EXT_ROOT, relative), 'utf8');
}

/** Paths passed to chrome.devtools.panels.create — the invisible kind. */
function panelPages(): string[] {
  const source = read('src/devtools/devtools.ts');
  const pattern = /panels\.create\(\s*[^,]+,\s*[^,]+,\s*['"]([^'"]+)['"]/g;
  return [...source.matchAll(pattern)].map((m) => m[1] as string);
}

/** HTML entry points declared to the bundler. */
function rollupInputs(): string[] {
  const config = read('vite.config.ts');
  const block = config.slice(config.indexOf('rollupOptions'));
  return [...block.matchAll(/['"]([^'"]+\.html)['"]/g)].map((m) => m[1] as string);
}

/** HTML pages the manifest points at; CRXJS finds these on its own. */
function manifestPages(): string[] {
  const config = read('src/manifest.config.ts');
  return [...config.matchAll(/['"]([^'"]+\.html)['"]/g)].map((m) => m[1] as string);
}

describe('build inputs', () => {
  it('registers at least one devtools panel, so this test is not vacuous', () => {
    expect(panelPages().length).toBeGreaterThan(0);
  });

  it.each(panelPages())('the panel page %s is an entry point the build knows about', (page) => {
    // Either declared to rollup, or reachable from the manifest. Neither means
    // it does not get built, and a DevTools panel pointing at a missing page
    // is blank rather than broken — no error, nothing in a console.
    const known = [...rollupInputs(), ...manifestPages()];
    expect(known).toContain(page);
  });

  it('the options page is still declared, which is why it works', () => {
    // Pinned as the control. options.html was already listed explicitly; if
    // that ever stops being true this test fails alongside the panel one,
    // which is the signal that the discovery rule itself changed.
    expect(rollupInputs()).toContain('src/popup/options.html');
  });
});
