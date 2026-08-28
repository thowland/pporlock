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
      new Set(['proxy', 'storage', 'tabs', 'alarms', 'webRequest']),
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

  it('declares no content scripts in this sprint', () => {
    // The in-page warning banner (REQ EXT-020) lands in Sprint 15. Until then
    // there is no reason to hold the permission that makes it possible.
    expect(manifest.content_scripts).toBeUndefined();
  });

  it('runs its background as a module service worker', () => {
    expect(manifest.background).toMatchObject({ type: 'module' });
  });

  it('declares a popup and an options page', () => {
    expect(manifest.action?.default_popup).toBeTruthy();
    expect(manifest.options_page).toBeTruthy();
  });
});
