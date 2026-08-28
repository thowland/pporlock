// Google Analytics (gtag.js and legacy ga) stub.
(function () {
  window.dataLayer = window.dataLayer || [];
  if (typeof window.gtag !== 'function') {
    window.gtag = function () { window.dataLayer.push(arguments); };
  }
  if (typeof window.ga !== 'function') {
    window.ga = function () {};
    window.ga.q = [];
    window.ga.l = Date.now();
    window.ga.getAll = function () { return []; };
    window.ga.create = function () { return { get: function () {}, set: function () {}, send: function () {} }; };
  }
  window.GoogleAnalyticsObject = 'ga';
})();
