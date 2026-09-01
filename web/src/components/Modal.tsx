/**
 * A modal dialog (SPEC-2 §11, REQ WUI-015).
 *
 * The first one this UI has needed. Everything else that could have been a
 * modal — the module report, a module's settings — is deliberately an inline
 * panel, because those are things you read *against* the table beside them.
 * A documentation index is the opposite: you are leaving the page for a moment
 * and coming back, and the page underneath should stay exactly where it was.
 *
 * Built rather than borrowed, and small enough to be read in one go:
 *
 *   - `role="dialog"` with `aria-modal`, labelled by its own heading
 *   - Escape closes it, and so does a click on the backdrop
 *   - focus moves in on open and returns to the control that opened it on close
 *   - Tab is trapped, because a modal you can tab out of behind is worse than
 *     no modal at all for anyone not using a mouse
 *
 * The trap is the part worth having a test for: it is easy to write, easy to
 * get subtly wrong, and impossible to notice by clicking around.
 */
import { useCallback, useEffect, useId, useRef, type ReactNode } from 'react';

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const panel = useRef<HTMLDivElement | null>(null);
  const opener = useRef<Element | null>(null);
  const headingId = useId();

  const focusable = useCallback(
    (): HTMLElement[] => Array.from(panel.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []),
    [],
  );

  // Remember where focus came from, move it in, and put it back on unmount.
  // Returning focus matters more than it sounds: without it, closing a dialog
  // opened from a table row drops a keyboard user at the top of the document.
  useEffect(() => {
    opener.current = document.activeElement;
    (focusable()[0] ?? panel.current)?.focus();
    return () => {
      (opener.current as HTMLElement | null)?.focus?.();
    };
  }, [focusable]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose();
        return;
      }
      if (event.key !== 'Tab') return;
      const items = focusable();
      const first = items[0];
      const last = items[items.length - 1];
      if (first === undefined || last === undefined) return;
      // Wrap at both ends. Without the shift-Tab half, focus escapes backwards
      // — which is the half that is always missing when this is done by hand.
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [focusable, onClose]);

  return (
    // The backdrop closes on its own click only — a click that started inside
    // the panel and ended on the backdrop (a drag over text) must not close it.
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        ref={panel}
        tabIndex={-1}
      >
        <header className="modal-head">
          <h2 id={headingId}>{title}</h2>
          <button type="button" className="action" onClick={onClose}>
            close
          </button>
        </header>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}
