-- Session database schema — SPEC-1 §6.3, REQ CAP-020.
--
-- One file per session. That is the point: copying, deleting, or handing a
-- session to a dry run is a single filesystem operation, with no shared
-- database to corrupt and no cross-session query anyone has to be prevented
-- from writing.
--
-- Everything written here has already been through the Redactor (REQ CAP-045).
-- No statement in this file is where that is enforced — it is enforced in
-- session.py, before the parameters reach any INSERT — but it is the reason
-- these columns can be plain TEXT and BLOB with no access control on top.

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS flows (
  flow_id              TEXT PRIMARY KEY,
  seq                  INTEGER NOT NULL,
  kind                 TEXT NOT NULL,
  started_at           TEXT NOT NULL,
  completed_at         TEXT,
  tab_id               INTEGER,
  method               TEXT,
  scheme               TEXT,
  host                 TEXT,
  port                 INTEGER,
  path                 TEXT,
  query                TEXT,
  url                  TEXT,
  dest                 TEXT,
  http_version         TEXT,
  status               INTEGER,
  reason               TEXT,
  content_type         TEXT,
  req_headers          TEXT NOT NULL,
  resp_headers         TEXT,
  req_body             BLOB,
  req_body_encoding    TEXT,
  req_body_truncated   INTEGER,
  resp_body            BLOB,
  resp_body_encoding   TEXT,
  resp_body_truncated  INTEGER,
  timing               TEXT,
  provenance           TEXT NOT NULL,
  modified             INTEGER,
  blocked              INTEGER,
  streamed             INTEGER,
  ws_closed            INTEGER,
  ws_close_code        INTEGER,
  passthrough_host     TEXT,
  passthrough_ip       TEXT,
  passthrough_pattern  TEXT,
  passthrough_reason   TEXT
);

CREATE INDEX IF NOT EXISTS flows_host   ON flows(host);
CREATE INDEX IF NOT EXISTS flows_seq    ON flows(seq);
CREATE INDEX IF NOT EXISTS flows_status ON flows(status);
CREATE INDEX IF NOT EXISTS flows_tab    ON flows(tab_id);

CREATE TABLE IF NOT EXISTS ws_messages (
  flow_id          TEXT NOT NULL,
  idx              INTEGER NOT NULL,
  ts               TEXT NOT NULL,
  direction        TEXT,
  opcode           TEXT,
  size             INTEGER,
  payload          BLOB,
  payload_encoding TEXT,
  truncated        INTEGER,
  PRIMARY KEY (flow_id, idx)
);

-- Notes are denormalised out of the provenance JSON so "show me every flow
-- where CSP was stripped" is an index lookup rather than a scan that parses
-- every provenance blob in the file.
CREATE TABLE IF NOT EXISTS notes (
  flow_id  TEXT NOT NULL,
  code     TEXT NOT NULL,
  severity TEXT NOT NULL,
  module   TEXT,
  message  TEXT,
  detail   TEXT
);

CREATE INDEX IF NOT EXISTS notes_code ON notes(code);
CREATE INDEX IF NOT EXISTS notes_flow ON notes(flow_id);
