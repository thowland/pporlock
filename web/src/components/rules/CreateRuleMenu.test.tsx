import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CreateRuleMenu } from './CreateRuleMenu';
import { ApiClient } from '../../api/client';
import { makeFlow } from '../../test/factories';

afterEach(() => vi.restoreAllMocks());

describe('CreateRuleMenu  # REQ WUI-008', () => {
  it('reaches a pre-filled rule in two clicks', async () => {
    const client = new ApiClient('http://127.0.0.1:8081');
    vi.spyOn(client, 'suggestRule').mockResolvedValue({
      rule: { name: 'suggested', action: 'block', match: { host: 'cdn.example.com' } },
      module: null,
    });
    const onRule = vi.fn();
    const flow = makeFlow();
    render(<CreateRuleMenu api={client} flow={flow} onRule={onRule} />);

    await userEvent.click(screen.getByLabelText(`Create rule from flow ${flow.flow_id}`));
    await userEvent.click(screen.getByRole('menuitem', { name: 'Block' }));

    expect(client.suggestRule).toHaveBeenCalledWith(flow.flow_id, 'block');
    expect(onRule).toHaveBeenCalledWith({
      name: 'suggested',
      action: 'block',
      match: { host: 'cdn.example.com' },
    });
  });

  it('offers all four intents from SPEC-2 §7.4', async () => {
    const client = new ApiClient('http://127.0.0.1:8081');
    render(<CreateRuleMenu api={client} flow={makeFlow()} onRule={vi.fn()} />);
    await userEvent.click(screen.getByRole('button', { name: /Create rule from flow/ }));
    expect(screen.getAllByRole('menuitem').map((item) => item.textContent)).toEqual([
      'Block',
      'Map local',
      'Redirect',
      'Edit headers',
    ]);
  });

  it('falls back to the local derivation when the daemon cannot suggest', async () => {
    const client = new ApiClient('http://127.0.0.1:8081');
    vi.spyOn(client, 'suggestRule').mockRejectedValue(new Error('404'));
    const onRule = vi.fn();
    render(<CreateRuleMenu api={client} flow={makeFlow()} onRule={onRule} />);

    await userEvent.click(screen.getByRole('button', { name: /Create rule from flow/ }));
    await userEvent.click(screen.getByRole('menuitem', { name: 'Redirect' }));
    expect(onRule).toHaveBeenCalledWith(
      expect.objectContaining({ action: 'redirect', name: 'redirect-cdn-example-com' }),
    );
  });

  it('closes the menu once an intent is chosen, and toggles shut', async () => {
    const client = new ApiClient('http://127.0.0.1:8081');
    vi.spyOn(client, 'suggestRule').mockResolvedValue({ rule: { name: 'r', action: 'block' } });
    render(<CreateRuleMenu api={client} flow={makeFlow()} onRule={vi.fn()} />);
    const trigger = screen.getByRole('button', { name: /Create rule from flow/ });
    await userEvent.click(trigger);
    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    await userEvent.click(trigger);
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
  });

  it('does not select the row underneath it', async () => {
    const client = new ApiClient('http://127.0.0.1:8081');
    vi.spyOn(client, 'suggestRule').mockResolvedValue({ rule: { name: 'r', action: 'block' } });
    const onRowClick = vi.fn();
    render(
      <div onClick={onRowClick}>
        <CreateRuleMenu api={client} flow={makeFlow()} onRule={vi.fn()} />
      </div>,
    );
    await userEvent.click(screen.getByRole('button', { name: /Create rule from flow/ }));
    await userEvent.click(screen.getByRole('menuitem', { name: 'Block' }));
    expect(onRowClick).not.toHaveBeenCalled();
  });
});
