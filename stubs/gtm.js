// Google Tag Manager stub.
//
// GTM's own snippet pushes onto window.dataLayer before this loads, so the
// array must survive; pages read back from it. Everything else is inert.
(function () {
  window.dataLayer = window.dataLayer || [];
  window.google_tag_manager = window.google_tag_manager || {};
  if (typeof window.gtag !== 'function') {
    window.gtag = function () { window.dataLayer.push(arguments); };
  }
})();
