/**
 * The provenance view. SPEC-2 §6.3, REQ CAP-013.
 *
 * The completeness tests are the important ones: a note code or outcome with no
 * rendering would appear as a bare enum value, which is exactly the silence
 * this view exists to remove.
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import {
  NEGATIVE_OUTCOMES,
  NOTE_LABEL,
  OUTCOME_LABEL,
  PHASE_LABEL,
  ProvenanceView,
} from './ProvenanceView';
import type { Provenance, ProvenanceEntry, ProvenanceNote } from '../../api/types';

/** Every value the daemon can emit (SPEC-0 §4.2–§4.4). */
const ALL_PHASES = [
  'clienthello',
  'request_short_circuit',
  'request_headers',
  'buffering_decision',
  'response_headers',
  'response_body',
  'websocket',
] as const;

const ALL_OUTCOMES = [
  'applied',
  'no_change',
  'skipped_streamed',
  'skipped_budget',
  'skipped_short_circuit',
  'skipped_disabled',
  'error',
] as const;

const ALL_NOTE_CODES = [
  'response_streamed',
  'transform_budget_exceeded',
  'module_quarantined',
  'map_local_missing',
  'csp_modified',
  'sri_stripped',
  'script_injected',
  'dev_toggle_active',
  'body_truncated',
  'module_error',
  'passthrough_excluded',
  'attribution_missing',
  'module_deprecation',
] as const;

function entry(overrides: Partial<ProvenanceEntry> = {}): ProvenanceEntry {
  return {
    seq: 0,
    phase: 'request_short_circuit',
    module: 'block-vendors',
    rule_id: 'block-vendors:2',
    rule_name: 'block-analytics-vendor',
    action: 'block',
    outcome: 'applied',
    duration_ms: 0.3,
    detail: {},
    ...overrides,
  } as ProvenanceEntry;
}

function note(overrides: Partial<ProvenanceNote> = {}): ProvenanceNote {
  return {
    code: 'csp_modified',
    severity: 'warning',
    message: 'removed CSP',
    module: 'relax-csp',
    detail: {},
    ...overrides,
  } as ProvenanceNote;
}

function provenance(overrides: Partial<Provenance> = {}): Provenance {
  return {
    profile: 'default',
    evaluated_modules: [],
    entries: [],
    notes: [],
    total_ms: 1.5,
    short_circuited_by: null,
    ...overrides,
  } as Provenance;
}

describe('completeness — nothing the daemon emits may render as a bare enum', () => {
  it.each(ALL_PHASES)('phase %s has a label', (phase) => {
    expect(PHASE_LABEL[phase]).toBeTruthy();
  });

  it.each(ALL_OUTCOMES)('outcome %s has a label', (outcome) => {
    expect(OUTCOME_LABEL[outcome]).toBeTruthy();
  });

  it.each(ALL_NOTE_CODES)('note code %s has a plain-language meaning', (code) => {
    expect(NOTE_LABEL[code]).toBeTruthy();
  });

  it.each(ALL_PHASES)('phase %s renders its section', (phase) => {
    render(<ProvenanceView provenance={provenance({ entries: [entry({ phase })] })} />);
    expect(screen.getByText(PHASE_LABEL[phase]!)).toBeTruthy();
  });

  it.each(ALL_OUTCOMES)('outcome %s renders its label', (outcome) => {
    render(<ProvenanceView provenance={provenance({ entries: [entry({ outcome })] })} />);
    expect(screen.getByText(OUTCOME_LABEL[outcome]!)).toBeTruthy();
  });

  it.each(ALL_NOTE_CODES)('note code %s renders its meaning', (code) => {
    render(<ProvenanceView provenance={provenance({ notes: [note({ code })] })} />);
    expect(screen.getByText(NOTE_LABEL[code]!)).toBeTruthy();
  });

  it('every non-applied outcome is classified as negative', () => {
    // A skipped or errored rule must never read as a success.
    for (const outcome of ALL_OUTCOMES) {
      if (outcome === 'applied' || outcome === 'no_change') continue;
      expect(NEGATIVE_OUTCOMES.has(outcome)).toBe(true);
    }
  });
});

describe('ProvenanceView', () => {
  it('says so plainly when nothing matched', () => {
    render(<ProvenanceView provenance={provenance()} />);
    expect(screen.getByText(/No rule matched this flow/)).toBeTruthy();
  });

  it('handles a flow with no provenance at all', () => {
    render(<ProvenanceView provenance={undefined} />);
    expect(screen.getByText(/No provenance recorded/)).toBeTruthy();
  });

  it('names the module, the rule, and the rule id', () => {
    render(<ProvenanceView provenance={provenance({ entries: [entry()] })} />);
    expect(screen.getByText('block-vendors')).toBeTruthy();
    expect(screen.getByText('block-analytics-vendor')).toBeTruthy();
    expect(screen.getByText('block-vendors:2')).toBeTruthy();
  });

  it('shows the action and duration', () => {
    render(<ProvenanceView provenance={provenance({ entries: [entry()] })} />);
    expect(screen.getByText('block')).toBeTruthy();
    expect(screen.getByText('0.30ms')).toBeTruthy();
  });

  it('expands the detail block a rule recorded', () => {
    render(
      <ProvenanceView
        provenance={provenance({
          entries: [entry({ detail: { derived_from_dest: 'script', synthesized_status: 200 } })],
        })}
      />,
    );
    expect(screen.getByText('derived_from_dest')).toBeTruthy();
    expect(screen.getByText('script')).toBeTruthy();
  });

  it('calls out the rule that short-circuited the flow', () => {
    // "An earlier rule ate it" is the most common confusion when debugging a
    // rule set, so it is stated rather than inferred.
    render(
      <ProvenanceView
        provenance={provenance({
          entries: [entry()],
          short_circuited_by: 'block-vendors:2',
        })}
      />,
    );
    expect(screen.getByText(/short-circuited the flow/)).toBeTruthy();
  });

  it('does not call out a rule that did not short-circuit', () => {
    render(<ProvenanceView provenance={provenance({ entries: [entry()] })} />);
    expect(screen.queryByText(/short-circuited the flow/)).toBeNull();
  });

  it('orders phases by pipeline order, not by arrival', () => {
    render(
      <ProvenanceView
        provenance={provenance({
          entries: [
            entry({ seq: 0, phase: 'response_body', rule_id: 'a:0' }),
            entry({ seq: 1, phase: 'request_short_circuit', rule_id: 'b:0' }),
          ],
        })}
      />,
    );
    const headings = screen.getAllByRole('heading', { level: 4 }).map((h) => h.textContent);
    expect(headings).toEqual([PHASE_LABEL['request_short_circuit'], PHASE_LABEL['response_body']]);
  });

  it('puts errors above warnings above info', () => {
    render(
      <ProvenanceView
        provenance={provenance({
          notes: [
            note({ code: 'response_streamed', severity: 'info' }),
            note({ code: 'module_error', severity: 'error' }),
            note({ code: 'csp_modified', severity: 'warning' }),
          ],
        })}
      />,
    );
    const codes = screen
      .getAllByText(/^(response_streamed|module_error|csp_modified)$/)
      .map((n) => n.textContent);
    expect(codes).toEqual(['module_error', 'csp_modified', 'response_streamed']);
  });

  it('shows the note severity as a class so styling is not colour-only', () => {
    const { container } = render(
      <ProvenanceView provenance={provenance({ notes: [note({ severity: 'error' })] })} />,
    );
    expect(container.querySelector('.prov-note.sev-error')).toBeTruthy();
  });

  it('marks a negative outcome distinctly', () => {
    const { container } = render(
      <ProvenanceView
        provenance={provenance({ entries: [entry({ outcome: 'skipped_streamed' })] })}
      />,
    );
    expect(container.querySelector('.prov-entry.negative')).toBeTruthy();
  });

  it('does not mark an applied outcome as negative', () => {
    const { container } = render(
      <ProvenanceView provenance={provenance({ entries: [entry()] })} />,
    );
    expect(container.querySelector('.prov-entry.negative')).toBeNull();
  });

  it('shows the profile and total time', () => {
    render(<ProvenanceView provenance={provenance({ profile: 'ad-blocking' })} />);
    expect(screen.getByText('ad-blocking')).toBeTruthy();
    expect(screen.getByText('1.50ms')).toBeTruthy();
  });

  it('links a module so it can be opened', () => {
    const onOpenModule = vi.fn();
    render(
      <ProvenanceView
        provenance={provenance({ entries: [entry()] })}
        onOpenModule={onOpenModule}
      />,
    );
    return userEvent.click(screen.getByText('block-vendors')).then(() => {
      expect(onOpenModule).toHaveBeenCalledWith('block-vendors');
    });
  });

  it('renders an engine-produced entry with no module', () => {
    render(
      <ProvenanceView
        provenance={provenance({ entries: [entry({ module: '', rule_id: '', rule_name: null })] })}
      />,
    );
    expect(screen.getByText('engine')).toBeTruthy();
  });

  it('renders a note with no detail', () => {
    render(<ProvenanceView provenance={provenance({ notes: [note({ detail: {} })] })} />);
    expect(screen.getByText('csp_modified')).toBeTruthy();
  });

  it('shows note detail when present', () => {
    render(
      <ProvenanceView
        provenance={provenance({ notes: [note({ detail: { pattern: '*.chase.com' } })] })}
      />,
    );
    expect(screen.getByText('*.chase.com')).toBeTruthy();
  });
});
