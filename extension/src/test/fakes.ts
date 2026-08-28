/** Fakes for chrome APIs and the control API. Shared test infrastructure. */
import type { StorageArea } from '../shared/state';
import type { ProxyApi } from '../background/proxy';
import type { BadgeApi, BadgeView } from '../background/badge';

export class FakeStorage implements StorageArea {
  readonly data = new Map<string, unknown>();

  async get(keys: string | string[] | null): Promise<Record<string, unknown>> {
    if (keys === null) return Object.fromEntries(this.data);
    const list = Array.isArray(keys) ? keys : [keys];
    const out: Record<string, unknown> = {};
    for (const key of list) {
      // Test fake; keys come from the caller's own literals.
      // eslint-disable-next-line security/detect-object-injection
      if (this.data.has(key)) out[key] = this.data.get(key);
    }
    return out;
  }

  async set(items: Record<string, unknown>): Promise<void> {
    for (const [key, value] of Object.entries(items)) this.data.set(key, value);
  }

  async remove(keys: string | string[]): Promise<void> {
    for (const key of Array.isArray(keys) ? keys : [keys]) this.data.delete(key);
  }
}

export class FakeProxyApi implements ProxyApi {
  config: chrome.proxy.ProxyConfig | null = null;
  level = 'controllable_by_this_extension';
  setCalls = 0;
  clearCalls = 0;
  failOnSet = false;
  failOnClear = false;

  async set(config: { value: chrome.proxy.ProxyConfig }): Promise<void> {
    this.setCalls += 1;
    if (this.failOnSet) throw new Error('set refused');
    this.config = config.value;
    this.level = 'controlled_by_this_extension';
  }

  async clear(): Promise<void> {
    this.clearCalls += 1;
    if (this.failOnClear) throw new Error('clear refused');
    this.config = null;
    this.level = 'controllable_by_this_extension';
  }

  async get(): Promise<{ levelOfControl: string }> {
    return { levelOfControl: this.level };
  }
}

export class FakeBadgeApi implements BadgeApi {
  last: Partial<BadgeView> = {};

  async setBadgeText(details: { text: string }): Promise<void> {
    this.last.text = details.text;
  }

  async setBadgeBackgroundColor(details: { color: string }): Promise<void> {
    this.last.color = details.color;
  }

  async setTitle(details: { title: string }): Promise<void> {
    this.last.title = details.title;
  }
}
