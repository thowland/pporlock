import { useState } from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { RuleBuilder } from './RuleBuilder';
import { readRules } from '../../lib/module-yaml';
import { emptyDraft, ruleToDraft, type RuleDraft } from '../../lib/rule-draft';
import { FIXTURE_MODULE_YAML } from '../../test/module-fixture';

const RULES = readRules(FIXTURE_MODULE_YAML);

/** Renders the builder as the editor does: controlled, with state held above. */
function Harness({ initial }: { initial: RuleDraft }) {
  const [draft, setDraft] = useState<RuleDraft>(initial);
  return <RuleBuilder draft={draft} onChange={setDraft} moduleName="block-vendors" />;
}

describe('RuleBuilder  # REQ WUI-007', () => {
  it('mirrors SPEC-0 §5.3 match criteria as named controls  # REQ WUI-015', () => {
    render(<RuleBuilder draft={emptyDraft()} onChange={vi.fn()} />);
    for (const label of [
      'Rule name',
      'Rule enabled',
      'Host',
      'Path',
      'Method',
      'Destination',
      'Status',
      'Content type',
      'Action',
    ]) {
      expect(screen.getByLabelText(label)).toBeTruthy();
    }
  });

  it('shows a live preview of the emitted YAML', () => {
    render(<Harness initial={ruleToDraft(RULES[2]!)} />);
    const preview = screen.getByLabelText('Emitted YAML preview');
    expect(preview.textContent).toContain('name: strip-csp-on-html');
    expect(preview.textContent).toContain('transform: strip_csp');
  });

  it('updates the preview as the form is edited', async () => {
    render(<Harness initial={emptyDraft()} />);
    await userEvent.type(screen.getByLabelText('Rule name'), 'my-rule');
    await userEvent.type(screen.getByLabelText('Host'), 'x.example');
    expect(screen.getByLabelText('Emitted YAML preview').textContent).toContain('name: my-rule');
    expect(screen.getByLabelText('Emitted YAML preview').textContent).toContain('host: x.example');
  });

  it('swaps the parameter panel with the action', async () => {
    render(<Harness initial={emptyDraft()} />);
    expect(screen.getByLabelText('Block mode')).toBeTruthy();
    await userEvent.selectOptions(screen.getByLabelText('Action'), 'map_local');
    expect(screen.getByLabelText('File')).toBeTruthy();
    await userEvent.selectOptions(screen.getByLabelText('Action'), 'redirect');
    expect(screen.getByLabelText('Redirect host')).toBeTruthy();
    await userEvent.selectOptions(screen.getByLabelText('Action'), 'body');
    expect(screen.getByLabelText('Transform')).toBeTruthy();
    await userEvent.selectOptions(screen.getByLabelText('Action'), 'headers');
    expect(screen.getByRole('button', { name: 'Add request add' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Add response remove' })).toBeTruthy();
  });

  it('edits redirect parameters into the emitted rule', async () => {
    render(<Harness initial={ruleToDraft(RULES[4]!)} />);
    const port = screen.getByLabelText('Redirect port');
    await userEvent.clear(port);
    await userEvent.type(port, '9000');
    expect(screen.getByLabelText('Emitted YAML preview').textContent).toContain('port: 9000');
  });

  it('adds and removes query criteria', async () => {
    render(<Harness initial={emptyDraft()} />);
    await userEvent.click(screen.getByRole('button', { name: 'Add Query criterion' }));
    await userEvent.type(screen.getByLabelText('Query criterion key 1'), 'tid');
    await userEvent.type(screen.getByLabelText('Query criterion value 1'), '^UA-');
    expect(screen.getByLabelText('Emitted YAML preview').textContent).toContain('tid: ^UA-');

    await userEvent.click(screen.getByRole('button', { name: 'Remove Query criterion 1' }));
    expect(screen.queryByLabelText('Query criterion key 1')).toBeNull();
  });

  it('marks a header criterion as presence-only', async () => {
    render(<Harness initial={ruleToDraft(RULES[0]!)} />);
    const rows = screen.getAllByLabelText(/Request header criterion key/);
    expect(rows).toHaveLength(2);
    const presence = screen.getByLabelText('Request header criterion 2 presence only');
    expect(presence).toHaveProperty('checked', true);
    await userEvent.click(presence);
    expect(screen.getByLabelText('Emitted YAML preview').textContent).not.toContain(
      'x-requested-with: null',
    );
  });

  it('edits header add/set/remove lists', async () => {
    render(<Harness initial={ruleToDraft(RULES[3]!)} />);
    await userEvent.click(screen.getByRole('button', { name: 'Add response remove' }));
    await userEvent.type(screen.getByLabelText('response remove 1'), 'etag');
    expect(screen.getByLabelText('Emitted YAML preview').textContent).toContain('- etag');
    await userEvent.click(screen.getByRole('button', { name: 'Remove response remove 1' }));
    expect(screen.queryByLabelText('response remove 1')).toBeNull();
  });

  it('keeps unmodelled keys visible and editable rather than dropping them', async () => {
    render(<Harness initial={ruleToDraft(RULES[1]!)} />);
    const extra = screen.getByLabelText('Other keys as YAML');
    expect((extra as HTMLTextAreaElement).value).toContain('window.analytics=');
    expect(screen.getByLabelText('Emitted YAML preview').textContent).toContain('stub:');
  });

  it('holds invalid YAML in the escape hatch instead of refusing keystrokes', async () => {
    render(<Harness initial={emptyDraft()} />);
    const extra = screen.getByLabelText('Other keys as YAML');
    // `[` and `{` are user-event modifiers; doubling them types the literal.
    await userEvent.type(extra, 'a: [[1,');
    expect((extra as HTMLTextAreaElement).value).toBe('a: [1,');
    expect(screen.getByText('Not valid YAML — not applied yet.')).toBeTruthy();

    await userEvent.type(extra, ']');

    expect(screen.queryByText('Not valid YAML — not applied yet.')).toBeNull();
    expect(screen.getByLabelText('Emitted YAML preview').textContent).toContain('a:');
  });

  it('toggles the enabled flag into the emitted rule', async () => {
    render(<Harness initial={emptyDraft()} />);
    await userEvent.click(screen.getByLabelText('Rule enabled'));
    expect(screen.getByLabelText('Emitted YAML preview').textContent).toContain('enabled: false');
  });
});
