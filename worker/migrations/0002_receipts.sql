-- Processor receipts (spec 4.5): the highest one per processor and device.
-- Shown to the device only; nothing in the relay depends on them.

CREATE TABLE receipts (
  device_id    TEXT NOT NULL REFERENCES devices (device_id),
  processor_id TEXT NOT NULL,
  seq          INTEGER NOT NULL,
  doc          TEXT NOT NULL,
  sig          TEXT NOT NULL,
  received_at  TEXT NOT NULL,
  PRIMARY KEY (device_id, processor_id)
);
