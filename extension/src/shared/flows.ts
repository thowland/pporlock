/**
 * Flow shapes the extension reads.
 *
 * Re-exported from the generated contract types so nothing here describes a
 * wire shape by hand (SPEC-0 §1.1).
 */
export type {
  FlowPassthrough,
  FlowRecord,
  FlowRequest,
  FlowResponse,
  Provenance,
  ProvenanceEntry,
  ProvenanceNote,
} from '@contracts/types';

export interface FlowPage {
  flows: import('@contracts/types').FlowRecord[];
  next_cursor: string | null;
  total_estimate: number;
}

export interface FlowQuery {
  tab_id?: number;
  limit?: number;
  modified?: boolean;
  blocked?: boolean;
  host?: string;
}
