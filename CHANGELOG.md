# Changelog

## v0.3.7

- **One path decision per connection attempt**: the candidate loop now calls
  `establish_connection` with `max_attempts=1`. Under Home Assistant, the
  bluetooth client wrapper rescores every available path (RSSI + failure
  counts) on each connect attempt, so a multi-attempt call could silently
  fail over to a stronger-signal — possibly unbonded — proxy mid-call
  (observed in the field: an unbonded proxy captured the BRC1H's single
  central slot through SMP auth timeouts, keeping the thermostat unreachable
  for over 20 minutes). Retries and the failover decision now stay with the
  candidate loop. Note: `bleak-retry-connector`'s `ble_device_callback`
  parameter is vestigial in 4.6.0 (declared but never read) and cannot pin
  the path.
- **`connected_source` is reset on disconnect and cleanup** so callers never
  read a stale path after the connection that used it is gone.

## v0.3.6

- **Typed errors**: new `MadokaError` base class; `PairingRequiredError`
  (carries the attempted proxy sources) raised when every path refuses the
  authenticated bond; `DeviceUnreachableError` when no BLE path sees the
  device. `ConnectionException` now subclasses `MadokaError`.
  `is_pairing_error()` classifies auth/bond failures from error text
  (ATT error 0x05 "Insufficient authentication", insufficient encryption,
  pairing failed, BlueZ authentication errors); a `pair()` timeout is treated
  as a pairing failure at the call site (unanswered prompt on the thermostat
  screen). Typed errors propagate out of `start()` so callers can react;
  background reconnects record them in `connection.last_error` instead of
  crashing.
- **Candidate-list connect API**: `Controller(..., candidates_callback=...)` —
  the caller supplies an ordered list of `BLEDevice` paths (preferred proxy
  first); each is tried in order with per-path pairing classification, and
  `connection.connected_source` reports the proxy that served the winning
  connection. Enables sticky-proxy behavior in Home Assistant. Without the
  callback, behavior is unchanged (legacy single-device path).
- **Connection robustness**: a failed candidate's late disconnect callback can
  no longer clobber a live connection or spawn a competing reconnect (the
  BRC1H accepts a single central); failed paths are disconnected before trying
  the next one; background tasks are tracked and quiesced by `cleanup()`
  (no reconnect racing an unload); `last_error` is cleared on any successful
  connect.
- **Explicit pairing-timeout message** (#3): a `pair()` timeout now says
  "confirm the pairing prompt on the thermostat screen (required once per
  Bluetooth proxy)".
- `ConnectionStatus` is now exported at package level.
- Test suite: 16 → 46 tests (error taxonomy, classifier, candidate loop,
  propagation, cleanup quiescing).

## v0.3.5

- Re-pair on every reconnect: `_paired` is reset on disconnect/cleanup so a
  dropped link (or the HA Reconnect button) recovers cleanly instead of
  failing with "Insufficient authentication" (the bond is stored per
  Bluetooth proxy). Validated on hardware.

## v0.3.4

- PyPI-ready metadata; first release published as `pymadoka-ng`.

## v0.3.0–0.3.3

- HA-native BLE path (bleak + bleak-retry-connector), explicit `pair()`,
  retries, cancellation fixes, lean packaging, first tests + CI.
  See release notes for details.
