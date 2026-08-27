import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { FlowTable } from './FlowTable';
import { makeFlow, makeProvenance } from '../test/factories';

describe('FlowTable', () => {
  it('renders a row per flow', () => {
    render(
      <FlowTable
        flows={[makeFlow({ flow_id: 'a' }), makeFlow({ flow_id: 'b' })]}
        connected
        hasFilter={false}
      />,
    );
    expect(screen.getAllByRole('row')).toHaveLength(3); // header + 2
  });

  it('shows host, path, status and size', () => {
    render(<FlowTable flows={[makeFlow()]} connected hasFilter={false} />);
    expect(screen.getByText('cdn.example.com')).toBeTruthy();
    expect(screen.getByText('/a/analytics.js')).toBeTruthy();
    expect(screen.getByText('200')).toBeTruthy();
    expect(screen.getByText('4.7k')).toBeTruthy();
  });

  it('shows proxy overhead per flow so the cost is visible, not inferred', () => {
    render(<FlowTable flows={[makeFlow()]} connected hasFilter={false} />);
    expect(screen.getByText('1.5')).toBeTruthy();
  });

  describe('flags — how you scan a hundred flows for the one that broke', () => {
    it('marks a blocked flow', () => {
      render(<FlowTable flows={[makeFlow({ blocked: true })]} connected hasFilter={false} />);
      expect(screen.getByText('BLK')).toBeTruthy();
    });

    it('marks a modified flow', () => {
      render(<FlowTable flows={[makeFlow({ modified: true })]} connected hasFilter={false} />);
      expect(screen.getByText('MOD')).toBeTruthy();
    });

    it('marks a streamed response, whose transforms were unavailable', () => {
      const flow = makeFlow();
      flow.response!.streamed = true;
      render(<FlowTable flows={[flow]} connected hasFilter={false} />);
      expect(screen.getByText('STR')).toBeTruthy();
    });

    it('marks a tunneled connection', () => {
      const flow = makeFlow({
        kind: 'passthrough',
        request: undefined,
        response: undefined,
        passthrough: {
          host: 'www.chase.com',
          ip: null,
          pattern: '*.chase.com',
          reason: 'financial',
        },
      });
      render(<FlowTable flows={[flow]} connected hasFilter={false} />);
      expect(screen.getByText('TUN')).toBeTruthy();
      expect(screen.getByText('www.chase.com')).toBeTruthy();
    });

    it('raises a warning marker for warning-severity notes', () => {
      const flow = makeFlow({
        provenance: makeProvenance({
          notes: [{ code: 'csp_modified', severity: 'warning', message: 'removed CSP' }],
        }),
      });
      render(<FlowTable flows={[flow]} connected hasFilter={false} />);
      expect(screen.getByText('!')).toBeTruthy();
    });

    it('raises an error marker for error-severity notes', () => {
      const flow = makeFlow({
        provenance: makeProvenance({
          notes: [{ code: 'module_error', severity: 'error', message: 'raised' }],
        }),
      });
      render(<FlowTable flows={[flow]} connected hasFilter={false} />);
      expect(screen.getByText('✕')).toBeTruthy();
    });

    it('error wins over warning when both are present', () => {
      const flow = makeFlow({
        provenance: makeProvenance({
          notes: [
            { code: 'csp_modified', severity: 'warning', message: 'w' },
            { code: 'module_error', severity: 'error', message: 'e' },
          ],
        }),
      });
      render(<FlowTable flows={[flow]} connected hasFilter={false} />);
      expect(screen.getByText('✕')).toBeTruthy();
      expect(screen.queryByText('!')).toBeNull();
    });

    it('hides the unattributed marker while nothing is attributed', () => {
      // A flag on every row conveys nothing and trains the eye to ignore it.
      // Attribution does not exist until Sprint 6.
      render(<FlowTable flows={[makeFlow({ tab_id: null })]} connected hasFilter={false} />);
      expect(screen.queryByText('?')).toBeNull();
    });

    it('shows the unattributed marker once attribution is working', () => {
      render(
        <FlowTable
          flows={[makeFlow({ flow_id: 'a', tab_id: 7 }), makeFlow({ flow_id: 'b', tab_id: null })]}
          connected
          hasFilter={false}
        />,
      );
      expect(screen.getByText('?')).toBeTruthy();
    });
  });

  describe('empty states', () => {
    it('distinguishes disconnected from quiet', () => {
      // REQ WUI-013 — this must never require inference.
      render(<FlowTable flows={[]} connected={false} hasFilter={false} />);
      expect(screen.getByText(/Not connected to the daemon/)).toBeTruthy();
    });

    it('says so when a filter is hiding everything', () => {
      render(<FlowTable flows={[]} connected hasFilter />);
      expect(screen.getByText(/No flows match this filter/)).toBeTruthy();
    });

    it('says it is waiting when connected and unfiltered', () => {
      render(<FlowTable flows={[]} connected hasFilter={false} />);
      expect(screen.getByText(/Waiting for traffic/)).toBeTruthy();
    });
  });
});
