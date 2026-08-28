/**
 * A deliberately awkward `module.yaml`.
 *
 * It exercises the things a naive round-trip loses: comments between rules, a
 * blank line, single-element lists, a quoted scalar that does not need quoting,
 * a status range string, presence-only header criteria, and action keys the
 * rule builder does not model. Any of those coming back changed is a bug.
 */
export const FIXTURE_MODULE_YAML = `name: block-vendors
version: 1.2.0
pporlock_api: "1"
description: Suppresses common analytics vendors
author: th
enabled: true
priority: 100

rules:
  # First match wins for short-circuit actions (REQ MOD-012).
  - name: block-analytics-vendor
    match:
      host: "*.analytics-vendor.example"
      path: "^/collect"
      method: [GET, POST]
      dest: script
      query:
        tid: "^UA-"
      request_headers:
        referer: "^https://target\\\\."
        x-requested-with:
    action: block
    mode: stub
    stub: auto

  # An inline stub spec: the builder has no form for this and must not eat it.
  - name: block-with-inline-stub
    match:
      host: tracker.example
      method: [POST]
    action: block
    stub:
      status: 200
      content_type: application/javascript
      body: "window.analytics={track(){}};"

  - name: strip-csp-on-html
    enabled: false
    match:
      status: [200, "300-399"]
      content_type: "text/html"
    action: body
    transform: strip_csp

  - name: add-debug-header
    match:
      host: target.example
    action: headers
    request:
      set:
        x-pporlock: "1"
      remove:
        - if-none-match
    response:
      add:
        x-debug: on

  - name: send-to-fixture
    match:
      host: cdn.example.com
      path: "^/a/analytics\\\\.js$"
    action: redirect
    to:
      scheme: http
      host: 127.0.0.1
      port: 8099
      path: /stub.js

config:
  vendor_list: []
`;
