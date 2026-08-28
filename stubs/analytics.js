// Generic analytics stub.
//
// Replaces a tracker that pages call synchronously. A page that calls
// analytics.track() on a script which failed to load throws, and the throw
// often breaks unrelated rendering; one that loads this proceeds normally.
(function () {
  var noop = function () {};
  var chainable = function () { return api; };
  var api = {
    initialize: noop, load: noop, ready: function (fn) { if (typeof fn === 'function') fn(); },
    track: noop, page: noop, identify: noop, group: noop, alias: noop, reset: noop,
    trackLink: noop, trackForm: noop, on: noop, off: noop, once: noop,
    user: function () { return { id: function () { return null; }, traits: function () { return {}; } }; },
    debug: chainable, addSourceMiddleware: chainable, setAnonymousId: noop,
  };
  window.analytics = window.analytics || api;
})();
