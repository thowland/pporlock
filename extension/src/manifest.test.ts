/**
 * The manifest is a security surface, not configuration.
 *
 * Every permission is a real cost, and MV3 permissions are the difference
 * between an extension that can read one loopback port and one that can read
 * every page you visit. REQ EXT-001.
 */
import { describe, expect, it } from 'vitest';
import manifest from './manifest.config';

describe('manifest', () => {
  it('is MV3', () => {
    expect(manifest.manifest_version).toBe(3);
  });

  it('requests exactly the permissions it needs and no more', () => {
    expect(new Set(manifest.permissions)).toEqual(
      new Set(['proxy', 'storage', 'tabs', 'alarms', 'webRequest', 'notifications']),
    );
  });

  it('does NOT request webRequestBlocking — interception is the daemon’s job', () => {
    expect(manifest.permissions).not.toContain('webRequestBlocking');
  });

  it('does NOT request declarativeNetRequest', () => {
    expect(manifest.permissions).not.toContain('declarativeNetRequest');
  });

  it('restricts host permissions to loopback, on any port', () => {
    // Any port, because the control port is configurable and an extension
    // pinned to 8081 is broken for anyone who changes it. Still loopback only:
    // broad host permissions would let the extension read every page.
    for (const host of manifest.host_permissions ?? []) {
      expect(host).toMatch(/^http:\/\/(127\.0\.0\.1|localhost)\/\*$/);
    }
  });

  it('does not take broad host access at install', () => {
    // The OI-2 spike established that per-tab attribution genuinely requires
    // <all_urls>: coverage was 0% with loopback-only permissions and 100% with
    // it. Rather than take it at install, it is optional and requested when the
    // user asks for the feature — so installing pporlock prompts for nothing
    // broad, and everything except per-tab counts works without it.
    expect(manifest.host_permissions).not.toContain('<all_urls>');
    expect(manifest.optional_host_permissions).toContain('<all_urls>');
  });

  it('grants no permission to any host that is not this machine', () => {
    for (const host of manifest.host_permissions ?? []) {
      const url = new URL(host.replace('/*', '/'));
      expect(['127.0.0.1', 'localhost']).toContain(url.hostname);
      expect(url.protocol).toBe('http:');
    }
  });

  it('requests no host permission over <all_urls>', () => {
    expect(manifest.host_permissions).not.toContain('<all_urls>');
  });

  it('declares the warning content script', () => {
    // REQ EXT-020. It matches all URLs because a modification warning must be
    // able to appear on any page, and it is inert without the optional host
    // grant: it only ever renders what the service worker sends it.
    const scripts = manifest.content_scripts ?? [];
    expect(scripts).toHaveLength(1);
    expect(scripts[0]?.js).toEqual(['src/content/banner.ts']);
    expect(scripts[0]?.run_at).toBe('document_idle');
  });

  it('runs its background as a module service worker', () => {
    expect(manifest.background).toMatchObject({ type: 'module' });
  });

  it('registers the DevTools panel', () => {
    // REQ EXT-013 — the designated primary debugging affordance, not optional.
    expect(manifest.devtools_page).toBeTruthy();
  });

  it('declares a popup and an options page', () => {
    expect(manifest.action?.default_popup).toBeTruthy();
    expect(manifest.options_page).toBeTruthy();
  });
});
