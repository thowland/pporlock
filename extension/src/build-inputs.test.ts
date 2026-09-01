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
import manifest from './manifest.config';
import { FLOWER_PNG } from './background/icon';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
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

/**
 * Every `*.html` literal anywhere in the source, wherever it is used.
 *
 * The generalisation of the panel bug. `panel.html` was lost because it was a
 * string; so is `ABOUT_PAGE`, handed to `chrome.runtime.getURL()`, and so will
 * be the next one. Rather than teach this test about each new way to name a
 * page, it holds every page name the source mentions to the same rule: if the
 * code can navigate there, the build has to emit it.
 *
 * Test files are excluded — a test may legitimately name a page that does not
 * exist in order to assert what happens then.
 */
function htmlLiteralsInSource(): string[] {
  const found = new Set<string>();
  const walk = (dir: string): void => {
    // Test-only, walking the extension's own source tree from a path computed
    // at import. No input reaches it.
    // eslint-disable-next-line security/detect-non-literal-fs-filename
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const path = resolve(dir, entry.name);
      if (entry.isDirectory()) {
        walk(path);
      } else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
        // eslint-disable-next-line security/detect-non-literal-fs-filename
        for (const match of readFileSync(path, 'utf8').matchAll(/['"]([^'"]+\.html)['"]/g)) {
          const page = match[1] as string;
          // An external URL is somebody else's page; only ours must be built.
          if (!/^[a-z]+:\/\//.test(page)) found.add(page);
        }
      }
    }
  };
  walk(resolve(EXT_ROOT, 'src'));
  return [...found];
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

  it('names at least one page in a string, so this test is not vacuous', () => {
    expect(htmlLiteralsInSource().length).toBeGreaterThan(0);
  });

  it.each(htmlLiteralsInSource())('the page %s named in source is built', (page) => {
    // A page the code can open but the build never emitted is a blank tab with
    // no error in any console — the exact shape of OI-28.
    expect([...rollupInputs(), ...manifestPages()]).toContain(page);
  });

  it('the options page is still declared, which is why it works', () => {
    // Pinned as the control. options.html was already listed explicitly; if
    // that ever stops being true this test fails alongside the panel one,
    // which is the signal that the discovery rule itself changed.
    expect(rollupInputs()).toContain('src/popup/options.html');
  });
});

/**
 * Icon files are shipped artefacts, not source.
 *
 * OI-33's lesson: a test that reads what the manifest *says* proves nothing
 * about what a clone gets. Chrome silently falls back to a generic puzzle piece
 * for a missing icon and reports nothing, so a manifest naming a file that was
 * never committed looks exactly like an extension nobody bothered to brand.
 */
describe('the artwork the manifest names', () => {
  const declared = (): string[] => [
    ...Object.values(manifest.icons ?? {}),
    ...Object.values(manifest.action.default_icon ?? {}),
  ];

  it('declares an icon at every size Chrome asks for', () => {
    // 16 toolbar, 32 Windows, 48 extensions page, 128 store and install prompt.
    for (const size of ['16', '32', '48', '128']) {
      expect(Object.keys(manifest.icons ?? {})).toContain(size);
    }
  });

  it.each([...new Set(declared())])('ships %s', (file) => {
    // `public/` is copied to the package root, so a manifest path of
    // `icons/x.png` is `public/icons/x.png` in the repository.
    // eslint-disable-next-line security/detect-non-literal-fs-filename
    expect(existsSync(resolve(EXT_ROOT, 'public', file))).toBe(true);
  });

  it('ships the artwork the service worker composites the status lamps onto', () => {
    // Fetched by URL at runtime (icon.ts), which is another string the bundler
    // cannot see. Without it the lamps silently never appear.
    // eslint-disable-next-line security/detect-non-literal-fs-filename
    expect(existsSync(resolve(EXT_ROOT, 'public', FLOWER_PNG))).toBe(true);
  });

  it('ships the icon the fail-safe notification names', () => {
    // The notification fires at the worst moment there is — the proxy just
    // turned itself off — and chrome.notifications rejects outright on a
    // missing iconUrl, so a wrong path here means silence.
    const worker = read('src/background/index.ts');
    const iconUrl = /iconUrl:\s*'([^']+)'/.exec(worker)?.[1];
    expect(iconUrl).toBeDefined();
    // eslint-disable-next-line security/detect-non-literal-fs-filename
    expect(existsSync(resolve(EXT_ROOT, 'public', iconUrl as string))).toBe(true);
  });
});
