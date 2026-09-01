/**
 * Help.
 *
 * Prose is not usually worth testing, and most of this file is not testing
 * prose. What it pins is the two claims the help makes that can silently stop
 * being true:
 *
 *   - every extension error state the user can hit has an entry here, with
 *     something to do about it
 *   - the lamp legend describes three lamps, in the same colours the extension
 *     actually draws
 *
 * Both are things a reader consults when they are already stuck, so a gap costs
 * more here than almost anywhere else in the UI.
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HelpView } from './HelpView';
import { EXTENSION_ERRORS } from '../../lib/extension-errors';
import { HELP_DOCS, LAMP_COLORS, docUrl } from '../../lib/about';

describe('HelpView', () => {
  it('documents every extension error state, with its code', () => {
    render(<HelpView onAbout={vi.fn()} />);
    const table = screen.getByRole('table');
    for (const error of EXTENSION_ERRORS) {
      // The code is what the popup shows as a tooltip, so it is the string
      // someone will search this page for.
      expect(within(table).getByText(error.code)).toBeTruthy();
      expect(within(table).getByText(error.title)).toBeTruthy();
    }
  });

  it('gives every error at least one step to follow', () => {
    render(<HelpView onAbout={vi.fn()} />);
    const rows = within(screen.getByRole('table')).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(EXTENSION_ERRORS.length);
    for (const row of rows) {
      expect(within(row).getAllByRole('listitem').length).toBeGreaterThan(0);
    }
  });

  it('explains all three lamps in the colours the extension draws', () => {
    const { container } = render(<HelpView onAbout={vi.fn()} />);
    const lamps = container.querySelectorAll('.lamp');
    expect(lamps).toHaveLength(3);
    // A legend whose green is merely nearby the real green teaches the wrong
    // green, which is worse than no legend.
    // jsdom rewrites a hex background to rgb(), so compare in that form.
    const rgb = (hex: string) =>
      `rgb(${[1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)).join(', ')})`;
    const backgrounds = [...lamps].map((l) => (l as HTMLElement).style.background);
    for (const colour of Object.values(LAMP_COLORS)) {
      expect(backgrounds).toContain(rgb(colour));
    }
  });

  it('says the grey lamp means traffic is going direct', () => {
    render(<HelpView onAbout={vi.fn()} />);
    // The one sentence in this view that has operational consequences: a user
    // who reads grey as "starting up" will keep browsing and wonder why the
    // table is empty.
    expect(document.body.textContent).toMatch(/going direct/);
  });

  it('warns that a dry run executes module code', () => {
    render(<HelpView onAbout={vi.fn()} />);
    // Every authoring surface has to say this (SECURITY.md); the help is the
    // surface where someone first learns dry run exists.
    expect(document.body.textContent).toMatch(/Dry run executes module Python/);
  });

  it('links to the further reading, on GitHub', () => {
    render(<HelpView onAbout={vi.fn()} />);
    const hrefs = screen.getAllByRole('link').map((a) => a.getAttribute('href'));
    for (const doc of HELP_DOCS) {
      expect(hrefs).toContain(docUrl(doc.file));
    }
  });

  it('opens every outbound link with rel=noreferrer', () => {
    render(<HelpView onAbout={vi.fn()} />);
    // This page holds a bearer token in the same origin; nothing leaves it
    // carrying a referrer or a live opener back into it.
    for (const link of screen.getAllByRole('link')) {
      expect(link.getAttribute('rel')).toBe('noreferrer');
    }
  });

  it('offers a way through to about', async () => {
    const onAbout = vi.fn();
    render(<HelpView onAbout={onAbout} />);
    await userEvent.click(screen.getByRole('button', { name: 'About pporlock' }));
    expect(onAbout).toHaveBeenCalled();
  });

  it('gives its contents list buttons rather than anchors', () => {
    render(<HelpView onAbout={vi.fn()} />);
    // An in-page `#section` href would overwrite the route hash and navigate
    // out of the very view it is a table of contents for.
    const toc = screen.getByRole('navigation', { name: 'Help contents' });
    expect(within(toc).queryAllByRole('link')).toHaveLength(0);
    expect(within(toc).getAllByRole('button').length).toBeGreaterThan(3);
  });
});
