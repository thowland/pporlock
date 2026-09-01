/**
 * The one-thing-only inline code renderer.
 *
 * Worth a test because the failure mode is silent and ugly rather than loud: a
 * mis-split leaves literal backticks in the interface, or — worse — swallows
 * the rest of a sentence into a code span, which reads as a rendering bug in a
 * page whose whole job is to be trusted while the user is stuck.
 */
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Ticked } from './Ticked';
import { EXTENSION_ERRORS } from '../../lib/extension-errors';

describe('Ticked', () => {
  it('renders a backticked span as code', () => {
    render(<Ticked text="Start it with `pporlock run`, then retry." />);
    expect(screen.getByText('pporlock run').tagName).toBe('CODE');
  });

  it('keeps the surrounding prose', () => {
    const { container } = render(<Ticked text="Run `pporlock pair` and enter the code." />);
    expect(container.textContent).toBe('Run pporlock pair and enter the code.');
  });

  it('handles more than one span', () => {
    render(<Ticked text="`a` then `b`" />);
    expect(screen.getByText('a').tagName).toBe('CODE');
    expect(screen.getByText('b').tagName).toBe('CODE');
  });

  it('leaves text with no backticks entirely alone', () => {
    const { container } = render(<Ticked text="Nothing to do — it clears itself." />);
    expect(container.querySelector('code')).toBeNull();
    expect(container.textContent).toBe('Nothing to do — it clears itself.');
  });

  it('leaves an unbalanced backtick as written', () => {
    // The alternative is swallowing the rest of the sentence into a code span,
    // which looks like a broken page rather than a typo in a string.
    const { container } = render(<Ticked text="a ` b" />);
    expect(container.querySelector('code')).toBeNull();
    expect(container.textContent).toBe('a ` b');
  });

  it('leaves no literal backtick anywhere in the shipped error catalogue', () => {
    // The catalogue is written as plain strings so it can be diffed against the
    // extension's source; this is what stops that decision leaking into the UI.
    for (const error of EXTENSION_ERRORS) {
      for (const text of [error.cause, ...error.fix]) {
        const { container } = render(<Ticked text={text} />);
        expect(container.textContent).not.toContain('`');
      }
    }
  });
});
