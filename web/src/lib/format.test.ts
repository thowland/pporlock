import { describe, expect, it } from 'vitest';
import { formatBytes, formatMs, formatTime, shortType, statusClass } from './format';

describe('formatBytes', () => {
  it.each([
    [0, '0'],
    [512, '512'],
    [1024, '1.0k'],
    [1536, '1.5k'],
    [1024 * 1024, '1.0M'],
    [4 * 1024 * 1024, '4.0M'],
  ])('formats %i as %s', (input, expected) => {
    expect(formatBytes(input)).toBe(expected);
  });

  it('renders an em dash for absent sizes', () => {
    expect(formatBytes(null)).toBe('—');
    expect(formatBytes(undefined)).toBe('—');
  });
});

describe('formatMs', () => {
  it('keeps sub-millisecond precision', () => {
    expect(formatMs(0.42)).toBe('0.42');
  });

  it('drops to one decimal above a millisecond', () => {
    expect(formatMs(12.34)).toBe('12.3');
  });

  it('switches to seconds above a second', () => {
    expect(formatMs(2500)).toBe('2.50s');
  });

  it('renders an em dash for absent timings', () => {
    expect(formatMs(null)).toBe('—');
  });
});

describe('formatTime', () => {
  it('renders clock time with milliseconds', () => {
    expect(formatTime('2026-08-27T14:03:22.417Z')).toMatch(/^\d{2}:\d{2}:\d{2}\.\d{3}$/);
  });

  it('passes an unparseable value through rather than showing NaN', () => {
    expect(formatTime('not a date')).toBe('not a date');
  });
});

describe('statusClass', () => {
  it.each([
    [200, 'status-2xx'],
    [204, 'status-2xx'],
    [301, 'status-3xx'],
    [404, 'status-4xx'],
    [500, 'status-5xx'],
  ])('maps %i to %s', (status, expected) => {
    expect(statusClass(status)).toBe(expected);
  });

  it('handles a flow with no response', () => {
    expect(statusClass(null)).toBe('status-none');
    expect(statusClass(undefined)).toBe('status-none');
  });
});

describe('shortType', () => {
  it('strips content-type parameters', () => {
    expect(shortType('text/html; charset=utf-8')).toBe('html');
  });

  it('shortens the common prefixes that carry no information', () => {
    expect(shortType('application/javascript')).toBe('javascript');
    expect(shortType('application/json')).toBe('json');
  });

  it('leaves other types intact', () => {
    expect(shortType('image/png')).toBe('image/png');
  });

  it('renders an em dash when absent', () => {
    expect(shortType(null)).toBe('—');
  });
});
