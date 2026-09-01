/**
 * The guides dialog.
 *
 * The module library used to say "author it here" and then leave you with a
 * YAML editor and no indication that a tutorial, a cookbook and a full
 * reference existed. Documentation nothing points at is documentation nobody
 * finds, so what is asserted here is that the pointer is present, complete, and
 * carries the warning that has to accompany every authoring surface.
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { GuidesDialog } from './GuidesDialog';
import { GUIDES, docUrl } from '../../lib/about';

describe('GuidesDialog', () => {
  it('lists every authoring guide, in reading order', () => {
    render(<GuidesDialog onClose={vi.fn()} />);
    const titles = screen.getAllByRole('link').map((a) => a.textContent);
    // Order matters: someone who has never written a module and opens the
    // reference first concludes the system is harder than it is.
    expect(titles).toEqual(GUIDES.map((g) => g.title));
  });

  it('links each guide to its document on GitHub', () => {
    render(<GuidesDialog onClose={vi.fn()} />);
    const hrefs = screen.getAllByRole('link').map((a) => a.getAttribute('href'));
    for (const guide of GUIDES) {
      expect(hrefs).toContain(docUrl(guide.file));
    }
  });

  it('says what a module is on disk, without leaving the page', () => {
    render(<GuidesDialog onClose={vi.fn()} />);
    // The one thing everybody has to look up once, and the reason the dialog
    // is not merely a list of links.
    const text = document.body.textContent ?? '';
    expect(text).toContain('module.yaml');
    expect(text).toContain('rules.yaml');
    expect(text).toContain('hooks.py');
    expect(text).toContain('assets/');
  });

  it('warns that modules are unsandboxed and that dry run runs them', () => {
    render(<GuidesDialog onClose={vi.fn()} />);
    // Required of every authoring surface: the trust model is that module code
    // is trusted, which is only honest if it is stated where modules are made.
    expect(screen.getByRole('note').textContent).toMatch(/unsandboxed/);
    expect(screen.getByRole('note').textContent).toMatch(/dry run/i);
  });

  it('is a dialog, so the library beneath it stays put', () => {
    render(<GuidesDialog onClose={vi.fn()} />);
    expect(screen.getByRole('dialog')).toBeTruthy();
  });
});
