import { describe, expect, it } from 'vitest';
import { parseFrame } from './events';

describe('parseFrame', () => {
  it('parses id, event, and data', () => {
    const frame = parseFrame('id: 7\nevent: flow.completed\ndata: {"a":1}');
    expect(frame).toEqual({ id: '7', event: 'flow.completed', data: '{"a":1}' });
  });

  it('strips exactly one leading space per SSE', () => {
    expect(parseFrame('data: value')?.data).toBe('value');
    expect(parseFrame('data:  value')?.data).toBe(' value');
  });

  it('joins multi-line data', () => {
    expect(parseFrame('data: one\ndata: two')?.data).toBe('one\ntwo');
  });

  it('returns null for a heartbeat comment', () => {
    // Heartbeats keep an idle stream alive; they are not events.
    expect(parseFrame(': heartbeat')).toBeNull();
    expect(parseFrame(': connected')).toBeNull();
  });

  it('returns null for an empty frame', () => {
    expect(parseFrame('')).toBeNull();
  });

  it('treats a bare field name as that field with an empty value', () => {
    // Per the SSE spec: a line with no colon is the field name, value empty.
    expect(parseFrame('data')).toEqual({ data: '' });
  });

  it('ignores unknown fields', () => {
    expect(parseFrame('retry: 500\ndata: x')).toEqual({ data: 'x' });
  });
});
