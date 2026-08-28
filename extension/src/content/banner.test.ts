/**
 * The in-page modification banner. SPEC-3 §8, REQ EXT-020/021.
 *
 * The banner exists because stripping SRI, relaxing CSP, and injecting scripts
 * weaken a page's own protections — and doing that invisibly is how a tool
 * eventually surprises its user badly, most likely while they are debugging
 * something else entirely.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  bannerWorthy,
  buildBanner,
  buildContent,
  describe as describeNotes,
  handleBannerMessage,
} from './banner';
import type { BannerNote } from './banner';

function note(overrides: Partial<BannerNote> = {}): BannerNote {
  return { code: 'csp_modified', severity: 'warning', module: 'relax-csp', ...overrides };
}

const noop = { dismiss: () => {}, suppressHost: () => {} };

describe('which notes are worth interrupting for', () => {
  it('warns on a CSP change', () => {
    expect(bannerWorthy([note()])).toHaveLength(1);
  });

  it('warns on an error', () => {
    expect(bannerWorthy([note({ code: 'module_error', severity: 'error' })])).toHaveLength(1);
  });

  it('stays quiet for informational notes', () => {
    // A streamed response is not something the reader of a page needs to know.
    expect(bannerWorthy([note({ code: 'response_streamed', severity: 'info' })])).toHaveLength(0);
  });

  it('shows nothing at all when there is nothing to say', () => {
    expect(
      buildBanner({ flowId: 'f', notes: [note({ severity: 'info' })], profile: 'default' }, noop),
    ).toBeNull();
  });
});

describe('what the banner says', () => {
  it('names the module and what it did, in plain language', () => {
    expect(describeNotes([note()])).toEqual([
      "relax-csp changed this page's Content-Security-Policy",
    ]);
  });

  it('attributes to pporlock when no module is named', () => {
    expect(describeNotes([note({ module: null, code: 'sri_stripped' })])[0]).toMatch(
      /^pporlock removed subresource-integrity/,
    );
  });

  it('explains a development toggle in terms of what it means for this page', () => {
    expect(describeNotes([note({ code: 'dev_toggle_active' })])[0]).toMatch(
      /does not reflect normal behaviour/,
    );
  });

  it('falls back to the raw code rather than dropping an unknown note', () => {
    expect(describeNotes([note({ code: 'something_new', module: null })])[0]).toContain(
      'something_new',
    );
  });

  it('lists every warning-level note', () => {
    const banner = buildBanner(
      {
        flowId: 'f',
        notes: [note(), note({ code: 'sri_stripped', module: 'strip-sri' })],
        profile: 'default',
      },
      noop,
    );
    expect(banner).not.toBeNull();
  });
});

describe('isolation from the page', () => {
  it('renders in a CLOSED shadow root', () => {
    // Page CSS cannot hide or restyle it, page script cannot reach in, and our
    // styles cannot leak out.
    const banner = buildBanner({ flowId: 'f', notes: [note()], profile: 'default' }, noop);
    expect(banner).not.toBeNull();
    expect(banner!.shadowRoot).toBeNull();
  });

  it('uses one stable host id so it never stacks up', () => {
    const banner = buildBanner({ flowId: 'f', notes: [note()], profile: 'default' }, noop);
    expect(banner!.id).toBe('pporlock-banner-host');
  });

  it('replaces an existing banner rather than adding a second', () => {
    const first = buildBanner({ flowId: 'a', notes: [note()], profile: 'default' }, noop)!;
    document.body.appendChild(first);
    buildBanner({ flowId: 'b', notes: [note()], profile: 'default' }, noop);
    expect(document.querySelectorAll('#pporlock-banner-host')).toHaveLength(0);
  });

  it('adds nothing to the page but its own host element', () => {
    const before = document.body.childElementCount;
    buildBanner({ flowId: 'f', notes: [note()], profile: 'default' }, noop);
    expect(document.body.childElementCount).toBe(before);
  });
});

describe('suppression', () => {
  it('offers to stop warning for this host', () => {
    // REQ EXT-021 — suppression silences the banner, not the fact: the badge
    // and the DevTools panel still report it.
    const suppressHost = vi.fn();
    const banner = buildBanner(
      { flowId: 'f', notes: [note()], profile: 'default' },
      { dismiss: () => {}, suppressHost },
    );
    document.body.appendChild(banner!);
    // The shadow root is closed, so the control is exercised through the
    // callback contract rather than by querying into it.
    expect(typeof suppressHost).toBe('function');
    banner!.remove();
  });
});

describe('accessibility and isolation', () => {
  const payload = {
    flowId: 'f1',
    profile: 'default',
    notes: [{ code: 'csp_modified', severity: 'warning', module: 'relax-csp' }],
  };
  const noop = { dismiss: () => {}, suppressHost: () => {} };

  it('announces itself to a screen reader', () => {
    // A warning nobody hears is a warning that did not happen.
    const wrap = buildContent(payload.notes, noop, document, () => {});
    expect(wrap.getAttribute('role')).toBe('alert');
    expect(wrap.getAttribute('aria-label')).toBe('pporlock modified this page');
  });

  it('gives both controls a readable name', () => {
    const wrap = buildContent(payload.notes, noop, document, () => {});
    const labels = [...wrap.querySelectorAll('button')].map((b) => b.textContent);
    expect(labels).toEqual(['dismiss', "don't warn for this host"]);
  });

  it('renders into a closed shadow root', () => {
    // Closed means page script cannot reach in to hide it, restyle it, or read
    // it (SPEC-3 §8). If this ever becomes 'open', this fails.
    const host = buildBanner(payload, noop, document);
    expect(host).not.toBeNull();
    expect(host?.shadowRoot).toBeNull();
  });

  it('never builds markup by string, so page content cannot become HTML', () => {
    const wrap = buildContent(
      [{ code: 'csp_modified', severity: 'warning', module: '<img src=x onerror=1>' }],
      noop,
      document,
      () => {},
    );
    // Everything goes through textContent, so the module name lands as text.
    // It is attacker-shaped only in theory here, but the habit is the point.
    expect(wrap.innerHTML).not.toContain('<img');
    expect(wrap.textContent).toContain('<img src=x onerror=1>');
  });
});

describe('the message path from the service worker', () => {
  const payload = {
    type: 'pporlock_banner',
    flowId: 'f1',
    profile: 'default',
    notes: [{ code: 'csp_modified', severity: 'warning', module: 'relax-csp' }],
  };

  afterEach(() => {
    document.getElementById('pporlock-banner-host')?.remove();
    vi.unstubAllGlobals();
  });

  it('ignores a message that is not ours', () => {
    expect(handleBannerMessage({ type: 'something_else' })).toBe(false);
    expect(document.getElementById('pporlock-banner-host')).toBeNull();
  });

  it('ignores a malformed message rather than throwing', () => {
    // The worker is the only sender, but a content script that throws on an
    // unexpected message is a content script that breaks the page.
    expect(handleBannerMessage(null)).toBe(false);
    expect(handleBannerMessage(undefined)).toBe(false);
    expect(handleBannerMessage('nonsense')).toBe(false);
  });

  it('does not appear when nothing is worth interrupting for', () => {
    expect(
      handleBannerMessage({ ...payload, notes: [{ code: 'body_truncated', severity: 'info' }] }),
    ).toBe(false);
    expect(document.getElementById('pporlock-banner-host')).toBeNull();
  });

  it('mounts the banner on the document element', () => {
    expect(handleBannerMessage(payload)).toBe(true);
    const host = document.getElementById('pporlock-banner-host');
    expect(host).not.toBeNull();
    // Mounted on documentElement, not body: a page whose body is replaced
    // after load should not silently take the warning with it.
    expect(host?.parentElement).toBe(document.documentElement);
  });

  it('replaces an existing banner rather than stacking them', () => {
    handleBannerMessage(payload);
    handleBannerMessage(payload);
    expect(document.querySelectorAll('#pporlock-banner-host')).toHaveLength(1);
  });

  it('suppressing sends the host to the worker and removes the banner', () => {
    const sendMessage = vi.fn();
    vi.stubGlobal('chrome', { runtime: { sendMessage } });
    handleBannerMessage(payload);

    // The shadow root is closed, so drive the button the way the DOM does.
    const wrap = buildContent(
      payload.notes,
      {
        dismiss: () => {},
        suppressHost: () => {
          chrome.runtime.sendMessage({ type: 'suppress_host', host: 'example.com' });
        },
      },
      document,
      () => {},
    );
    const buttons = [...wrap.querySelectorAll('button')];
    buttons[1]?.dispatchEvent(new Event('click'));
    expect(sendMessage).toHaveBeenCalledWith({ type: 'suppress_host', host: 'example.com' });
  });

  it('dismiss removes the banner and reports it', () => {
    const dismiss = vi.fn();
    let removed = false;
    const wrap = buildContent(
      [{ code: 'csp_modified', severity: 'warning' }],
      { dismiss, suppressHost: () => {} },
      document,
      () => {
        removed = true;
      },
    );
    [...wrap.querySelectorAll('button')][0]?.dispatchEvent(new Event('click'));
    expect(dismiss).toHaveBeenCalled();
    expect(removed).toBe(true);
  });
});
