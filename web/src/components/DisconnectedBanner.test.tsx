import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { DisconnectedBanner } from './DisconnectedBanner';

describe('DisconnectedBanner', () => {
  it('renders nothing when connected', () => {
    const { container } = render(<DisconnectedBanner connection="connected" onRetry={() => {}} />);
    expect(container.firstChild).toBeNull();
  });

  it('names the command that diagnoses the problem', () => {
    // REQ WUI-013 — a disconnected state that does not say what to do next is
    // only marginally better than an empty table.
    render(<DisconnectedBanner connection="disconnected" onRetry={() => {}} />);
    expect(screen.getByText('pporlock doctor')).toBeTruthy();
    expect(screen.getByText('pporlock run')).toBeTruthy();
  });

  it('distinguishes unauthorized from unreachable', () => {
    render(<DisconnectedBanner connection="unauthorized" onRetry={() => {}} />);
    expect(screen.getByText(/Not authorized/)).toBeTruthy();
  });

  it('offers a retry', async () => {
    const onRetry = vi.fn();
    render(<DisconnectedBanner connection="disconnected" onRetry={onRetry} />);
    await userEvent.click(screen.getByRole('button', { name: 'retry' }));
    expect(onRetry).toHaveBeenCalled();
  });
});
