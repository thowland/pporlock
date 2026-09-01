/**
 * The about box.
 *
 * What matters here is not that it renders — it is that the four things a user
 * comes to an about box for are actually present, and that the two links which
 * depend on a *configurable* port are built from the configured one. A help
 * link hard-coded to 8081 works on the developer's machine and nowhere else,
 * which is the failure mode this project keeps meeting (OI-18, and the manifest
 * comment about pinning the control port).
 */
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { AboutView } from './AboutView';
import { HOMEPAGE, LICENSE, WEB_UI_ABOUT, WEB_UI_HELP } from '../shared/about';

const ORIGIN = 'http://127.0.0.1:9123';

function renderAbout(props: Partial<Parameters<typeof AboutView>[0]> = {}) {
  return render(
    <AboutView extensionVersion="1.2.3" daemonVersion="1.2.3" controlOrigin={ORIGIN} {...props} />,
  );
}

const hrefs = (): string[] => screen.getAllByRole('link').map((a) => a.getAttribute('href') ?? '');

describe('the about box', () => {
  it('says the tool decrypts traffic, rather than describing it coyly', () => {
    renderAbout();
    // An interception proxy whose about box does not say what it does to TLS is
    // a security problem dressed as a product.
    expect(document.body.textContent).toMatch(/decrypts/i);
  });

  it('warns that module code is unsandboxed', () => {
    renderAbout();
    expect(document.body.textContent).toMatch(/unsandboxed/i);
  });

  it('names the licence and links to it', () => {
    renderAbout();
    expect(screen.getByText(LICENSE)).toBeTruthy();
  });

  it('links to the project on GitHub', () => {
    renderAbout();
    expect(hrefs()).toContain(HOMEPAGE);
  });

  it('builds the web UI links from the configured control origin', () => {
    renderAbout();
    expect(hrefs()).toContain(`${ORIGIN}${WEB_UI_HELP}`);
    expect(hrefs()).toContain(`${ORIGIN}${WEB_UI_ABOUT}`);
  });

  it('follows a control origin the user changed', () => {
    renderAbout({ controlOrigin: 'http://localhost:8300' });
    expect(hrefs()).toContain(`http://localhost:8300${WEB_UI_HELP}`);
  });

  it('opens every outbound link with rel=noreferrer', () => {
    renderAbout();
    // The web UI holds a bearer token; nothing leaves this page carrying a
    // referrer or a live opener into it.
    for (const link of screen.getAllByRole('link')) {
      expect(link.getAttribute('rel')).toBe('noreferrer');
    }
  });

  it('explains all three lamps, because the icon is otherwise a puzzle', () => {
    renderAbout();
    const text = document.body.textContent ?? '';
    expect(text).toMatch(/green, top left/);
    expect(text).toMatch(/grey, top left/);
    expect(text).toMatch(/red, top right/);
  });

  it('shows both versions, since they are installed separately', () => {
    renderAbout({ extensionVersion: '0.11.0', daemonVersion: '0.10.0' });
    // OI-24: one number cannot stand for the pair, and the mismatch is the
    // ordinary case while developing.
    expect(document.body.textContent).toContain('extension 0.11.0');
    expect(document.body.textContent).toContain('daemon 0.10.0');
  });

  it('still renders when the daemon cannot be asked', () => {
    renderAbout({ daemonVersion: null });
    // An about box that will not appear because the daemon is down is useless
    // exactly when someone is trying to work out what they are running.
    expect(document.body.textContent).toContain('daemon not reachable');
    expect(hrefs()).toContain(HOMEPAGE);
  });
});
