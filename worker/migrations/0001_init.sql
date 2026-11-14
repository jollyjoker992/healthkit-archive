-- healthkit-archive relay schema, version 1.
-- R2 holds the ciphertext; D1 holds the allowlist, grants and the batch index.
-- A row in `batches` is the commit point of an upload (spec 4.3 step 10).

CREATE TABLE users (
  user_id    TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  status     TEXT NOT NULL CHECK (status IN ('active', 'purged')),
  created_at TEXT NOT NULL,
  purged_at  TEXT
);

CREATE TABLE devices (
  device_id      TEXT PRIMARY KEY,
  user_id        TEXT NOT NULL REFERENCES users (user_id),
  pub            TEXT NOT NULL,
  name           TEXT NOT NULL,
  status         TEXT NOT NULL CHECK (status IN ('active', 'retired', 'purged')),
  enrolled_at    TEXT NOT NULL,
  retired_at     TEXT,
  purged_at      TEXT,
  -- Derived from `batches` on every upload (src/uploads.ts, step 10).
  contiguous_seq INTEGER NOT NULL DEFAULT 0,
  max_seq        INTEGER NOT NULL DEFAULT 0,
  -- Set by the scheduled purge job once every object of a purged device is deleted.
  -- Batches are only recorded while the device is active, so none can appear later.
  objects_deleted_at TEXT
);
CREATE INDEX devices_by_user ON devices (user_id);

CREATE TABLE invites_used (
  nonce     TEXT PRIMARY KEY,
  device_id TEXT NOT NULL,
  response  TEXT NOT NULL,
  used_at   TEXT NOT NULL
);

CREATE TABLE grants (
  device_id   TEXT NOT NULL REFERENCES devices (device_id),
  grant_seq   INTEGER NOT NULL,
  doc         TEXT NOT NULL,
  sig         TEXT NOT NULL,
  sha256      TEXT NOT NULL,
  recipients  TEXT NOT NULL,  -- JSON array of processor ids, in grant order
  received_at TEXT NOT NULL,
  PRIMARY KEY (device_id, grant_seq)
);

CREATE TABLE grant_processors (
  device_id    TEXT NOT NULL,
  grant_seq    INTEGER NOT NULL,
  processor_id TEXT NOT NULL,
  sign_pub     TEXT NOT NULL,
  PRIMARY KEY (device_id, grant_seq, processor_id)
);
CREATE INDEX grant_processors_by_processor ON grant_processors (processor_id);

CREATE TABLE batches (
  device_id         TEXT NOT NULL REFERENCES devices (device_id),
  seq               INTEGER NOT NULL,
  manifest          TEXT NOT NULL,
  sig               TEXT NOT NULL,
  manifest_sha256   TEXT NOT NULL,
  ciphertext_sha256 TEXT NOT NULL,
  size              INTEGER NOT NULL,
  object_key        TEXT NOT NULL,
  received_at       TEXT NOT NULL,
  -- Set by the purge job when the R2 object is deleted; the row is kept so that
  -- idempotent repeats and `state` stay correct after a purge.
  object_deleted_at TEXT,
  PRIMARY KEY (device_id, seq)
);
CREATE INDEX batches_by_received ON batches (device_id, received_at);
