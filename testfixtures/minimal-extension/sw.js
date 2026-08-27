// Harness probe service worker. Signals that MV3 loading worked.
self.__pporlock_probe = true;
chrome.storage.local.set({ probe: 'loaded' });
