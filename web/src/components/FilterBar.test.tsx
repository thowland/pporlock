import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { FilterBar } from './FilterBar';

const noop = () => {};

describe('FilterBar', () => {
  it('reports a host filter', async () => {
    const onChange = vi.fn();
    render(
      <FilterBar
        filter={{}}
        onChange={onChange}
        paused={false}
        heldCount={0}
        onTogglePause={noop}
        onClear={noop}
      />,
    );
    await userEvent.type(screen.getByLabelText('Filter by host'), 'a');
    expect(onChange).toHaveBeenCalledWith({ host: 'a' });
  });

  it('removes a filter when its field is cleared', async () => {
    const onChange = vi.fn();
    render(
      <FilterBar
        filter={{ host: 'a' }}
        onChange={onChange}
        paused={false}
        heldCount={0}
        onTogglePause={noop}
        onClear={noop}
      />,
    );
    await userEvent.clear(screen.getByLabelText('Filter by host'));
    expect(onChange).toHaveBeenCalledWith({});
  });

  it('toggles the modified chip on', async () => {
    const onChange = vi.fn();
    render(
      <FilterBar
        filter={{}}
        onChange={onChange}
        paused={false}
        heldCount={0}
        onTogglePause={noop}
        onClear={noop}
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: 'modified' }));
    expect(onChange).toHaveBeenCalledWith({ modified: true });
  });

  it('toggles the modified chip off', async () => {
    const onChange = vi.fn();
    render(
      <FilterBar
        filter={{ modified: true }}
        onChange={onChange}
        paused={false}
        heldCount={0}
        onTogglePause={noop}
        onClear={noop}
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: 'modified' }));
    expect(onChange).toHaveBeenCalledWith({});
  });

  it('reflects pressed state for accessibility', () => {
    render(
      <FilterBar
        filter={{ blocked: true }}
        onChange={noop}
        paused={false}
        heldCount={0}
        onTogglePause={noop}
        onClear={noop}
      />,
    );
    expect(screen.getByRole('button', { name: 'blocked' }).getAttribute('aria-pressed')).toBe(
      'true',
    );
  });

  it('shows how many rows are held while paused', () => {
    render(
      <FilterBar
        filter={{}}
        onChange={noop}
        paused
        heldCount={17}
        onTogglePause={noop}
        onClear={noop}
      />,
    );
    expect(screen.getByText(/resume \(17 held\)/)).toBeTruthy();
  });

  it('invokes pause and clear', async () => {
    const onTogglePause = vi.fn();
    const onClear = vi.fn();
    render(
      <FilterBar
        filter={{}}
        onChange={noop}
        paused={false}
        heldCount={0}
        onTogglePause={onTogglePause}
        onClear={onClear}
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: 'pause' }));
    await userEvent.click(screen.getByRole('button', { name: 'clear' }));
    expect(onTogglePause).toHaveBeenCalled();
    expect(onClear).toHaveBeenCalled();
  });
});
