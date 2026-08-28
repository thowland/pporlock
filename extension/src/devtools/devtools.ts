/**
 * DevTools page. Registers the panel and does nothing else.
 *
 * It runs once per inspected tab and is the only place a panel can be created
 * from, so it is deliberately tiny.
 */
chrome.devtools.panels.create('pporlock', '', 'src/devtools/panel.html');
