/** Chrome proxy control. SPEC-3 §4. */
import { describe, expect, it } from 'vitest';
import { ProxyController, bypassList, fixedServerConfig, pacScript } from './proxy';
import { FakeProxyApi } from '../test/fakes';

describe('bypassList', () => {
  it('always bypasses loopback', () => {
    const list = bypassList();
    expect(list).toContain('127.0.0.1');
    expect(list).toContain('localhost');
    expect(list).toContain('[::1]');
  });

  it('bypasses the control origin including its port', () => {
    // Without this the extension's own API calls — and the fail-safe health
    // check — would route through the proxy, so a dead proxy would look
    // identical to a dead daemon (SPEC-3 §4.4 rule 5).
    expect(bypassList('http://127.0.0.1:8081')).toContain('127.0.0.1:8081');
  });

  it('tolerates a malformed control origin', () => {
    expect(bypassList('not a url')).toContain('127.0.0.1');
  });

  it('does not duplicate an entry already present', () => {
    const list = bypassList('http://localhost');
    expect(list.filter((e) => e === 'localhost')).toHaveLength(1);
  });
});

describe('fixedServerConfig', () => {
  it('points at the proxy over http', () => {
    const config = fixedServerConfig({ host: '127.0.0.1', port: 8080 });
    expect(config.mode).toBe('fixed_servers');
    expect(config.rules?.singleProxy).toEqual({
      scheme: 'http',
      host: '127.0.0.1',
      port: 8080,
    });
  });

  it('carries the bypass list', () => {
    const config = fixedServerConfig({ host: '127.0.0.1', port: 8080 }, 'http://127.0.0.1:8081');
    expect(config.rules?.bypassList).toContain('127.0.0.1:8081');
  });
});

describe('pacScript', () => {
  it('proxies everything when no hosts are named', () => {
    const script = pacScript([], { host: '127.0.0.1', port: 8080 });
    expect(script).toContain('PROXY 127.0.0.1:8080');
  });

  it('sends loopback direct', () => {
    expect(pacScript(['a.example'], { host: '127.0.0.1', port: 8080 })).toContain('DIRECT');
  });

  it('embeds the include list safely as JSON', () => {
    const script = pacScript(['*.example.com'], { host: '127.0.0.1', port: 8080 });
    expect(script).toContain('["*.example.com"]');
  });
});

describe('ProxyController', () => {
  it('reports whether we hold the proxy', async () => {
    const api = new FakeProxyApi();
    const controller = new ProxyController(api);
    expect((await controller.status()).ours).toBe(false);

    await controller.enable({ host: '127.0.0.1', port: 8080 });
    expect((await controller.status()).ours).toBe(true);
  });

  it('detects that another extension holds it', async () => {
    // The popup must not show an enabled state the extension does not own.
    const api = new FakeProxyApi();
    api.level = 'controlled_by_other_extensions';
    expect(await new ProxyController(api).isControllable()).toBe(false);
  });

  it('detects enterprise policy control', async () => {
    const api = new FakeProxyApi();
    api.level = 'controlled_by_policy';
    const controller = new ProxyController(api);
    expect(await controller.isControllable()).toBe(false);
    expect((await controller.status()).level).toBe('controlled_by_policy');
  });

  it('treats a failing get as unknown rather than throwing', async () => {
    const api = new FakeProxyApi();
    api.get = async () => {
      throw new Error('no');
    };
    expect((await new ProxyController(api).status()).level).toBe('unknown');
  });

  it('enable applies a fixed-server configuration', async () => {
    const api = new FakeProxyApi();
    await new ProxyController(api).enable({ host: '127.0.0.1', port: 8080 });
    expect(api.config?.mode).toBe('fixed_servers');
  });

  it('enablePac applies a PAC script', async () => {
    const api = new FakeProxyApi();
    await new ProxyController(api).enablePac(['a.example'], { host: '127.0.0.1', port: 8080 });
    expect(api.config?.mode).toBe('pac_script');
  });

  it('disable clears the configuration', async () => {
    const api = new FakeProxyApi();
    const controller = new ProxyController(api);
    await controller.enable({ host: '127.0.0.1', port: 8080 });
    await controller.disable();
    expect(api.config).toBeNull();
  });

  it('surfaces a refused set', async () => {
    const api = new FakeProxyApi();
    api.failOnSet = true;
    await expect(
      new ProxyController(api).enable({ host: '127.0.0.1', port: 8080 }),
    ).rejects.toThrow();
  });
});

describe('scoped PAC mode (REQ EXT-003)', () => {
  const target = { host: '127.0.0.1', port: 8080 };

  it('proxies a listed host and sends the rest direct', () => {
    const script = pacScript(['*.example.com'], target);
    expect(script).toContain('shExpMatch');
    expect(script).toContain('PROXY 127.0.0.1:8080');
    expect(script).toContain('return "DIRECT"');
  });

  it('proxies everything when the list is empty', () => {
    // An empty scope list is ambiguous, and the safe reading is "no narrowing
    // asked for" rather than "proxy nothing" — which would look like the proxy
    // silently failing.
    const script = pacScript([], target);
    expect(script).toMatch(/include\.length === 0\) return "PROXY 127\.0\.0\.1:8080"/);
  });

  it('always sends loopback direct so the control API is reachable', () => {
    // Same rule as the fixed-server bypass list: routing the control origin
    // through the proxy makes a dead proxy indistinguishable from a dead
    // daemon, and the fail-safe depends on telling them apart.
    const script = pacScript(['*.example.com'], target);
    expect(script).toContain('host === "127.0.0.1"');
    expect(script).toContain('isPlainHostName(host)');
  });

  it('escapes interpolated values rather than splicing them into source', () => {
    // A PAC script is executable source. Both the host list and the proxy
    // target are JSON-encoded, so a quote in either stays inside its string
    // literal instead of closing it.
    const script = pacScript(['a"; return "PROXY evil:1'], { host: 'x"; //', port: 1 });
    expect(script).toContain(JSON.stringify(['a"; return "PROXY evil:1']));
    expect(script).toContain(JSON.stringify('PROXY x"; //:1'));
    // The payload never appears with its quotes unescaped, which is what
    // "closed the literal" would look like.
    expect(script).not.toContain('return "PROXY evil:1');
  });

  it('enablePac installs a pac_script config, not fixed_servers', async () => {
    const calls: { value: chrome.proxy.ProxyConfig; scope: string }[] = [];
    const controller = new ProxyController({
      set: async (c) => {
        calls.push(c);
      },
      clear: async () => {},
      get: async () => ({ levelOfControl: 'controllable_by_this_extension' }),
    });
    await controller.enablePac(['example.com'], target);
    expect(calls[0]?.value.mode).toBe('pac_script');
    expect(calls[0]?.value.pacScript?.data).toContain('example.com');
  });
});
