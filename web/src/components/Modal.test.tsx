/**
 * The modal's accessibility behaviour (REQ WUI-015).
 *
 * This is the file that earns the component. The visible part — a box with a
 * close button — is obvious and would work if written badly; the parts that
 * make it usable without a mouse are invisible, easy to get subtly wrong, and
 * never noticed by clicking around. Both directions of the tab trap are
 * asserted because the shift-Tab half is the one that is always missing.
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { Modal } from './Modal';

function open(onClose = vi.fn()) {
  render(
    <Modal title="Writing modules" onClose={onClose}>
      <button type="button">first</button>
      <button type="button">second</button>
    </Modal>,
  );
  return onClose;
}

describe('Modal', () => {
  it('is a dialog labelled by its own heading', () => {
    open();
    const dialog = screen.getByRole('dialog');
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    expect(screen.getByRole('heading', { name: 'Writing modules' })).toBeTruthy();
  });

  it('closes on Escape', async () => {
    const onClose = open();
    await userEvent.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalled();
  });

  it('closes on a click on the backdrop', async () => {
    const onClose = open();
    const backdrop = screen.getByRole('dialog').parentElement as HTMLElement;
    await userEvent.click(backdrop);
    expect(onClose).toHaveBeenCalled();
  });

  it('does not close on a click inside the panel', async () => {
    const onClose = open();
    await userEvent.click(screen.getByText('first'));
    expect(onClose).not.toHaveBeenCalled();
  });

  it('moves focus into the dialog on open', () => {
    open();
    // Otherwise a keyboard user's next Tab lands somewhere in the page behind
    // the overlay, with no way to tell where they are.
    expect(document.activeElement?.textContent).toBe('close');
  });

  it('wraps focus forwards at the last control', async () => {
    open();
    screen.getByText('second').focus();
    await userEvent.tab();
    expect(document.activeElement?.textContent).toBe('close');
  });

  it('wraps focus backwards at the first control', async () => {
    open();
    screen.getByRole('button', { name: 'close' }).focus();
    await userEvent.tab({ shift: true });
    // The half that is always missing when a trap is written by hand: without
    // it, shift-Tab walks straight out of the dialog into the page behind.
    expect(document.activeElement?.textContent).toBe('second');
  });

  it('returns focus to whatever opened it', async () => {
    function Host() {
      const [openDialog, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>
            guides
          </button>
          {openDialog && (
            <Modal title="Writing modules" onClose={() => setOpen(false)}>
              <p>body</p>
            </Modal>
          )}
        </>
      );
    }
    render(<Host />);
    await userEvent.click(screen.getByText('guides'));
    await userEvent.keyboard('{Escape}');
    // Without this, closing a dialog opened from deep in a table drops the
    // keyboard user at the top of the document.
    expect(document.activeElement?.textContent).toBe('guides');
  });
});
