// Meta/Facebook pixel stub. fbq is called with a string command; pages also
// read fbq.queue and fbq.loaded, so both exist.
(function () {
  if (typeof window.fbq === 'function') return;
  var fbq = function () { fbq.queue.push(arguments); };
  fbq.queue = [];
  fbq.loaded = true;
  fbq.version = '2.0';
  fbq.push = fbq;
  fbq.callMethod = null;
  window.fbq = fbq;
  window._fbq = fbq;
})();
