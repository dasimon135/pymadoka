# Changelog

## v0.3.12

- **Pairing is no longer attempted on a path the caller did not sanction.**
  New `allowed_sources_callback` on `Controller`/`Connection`: it returns the
  proxy sources the device may pair through, and it is checked against the
  path the backend *actually* used, read after `establish_connection` and
  before `pair()`. This closes the hole v0.3.11 could only report: filtering
  the candidate list cannot control where a connect lands, because habluetooth
  keeps just the address and re-scores every path itself. A connection routed
  somewhere unsanctioned is now dropped without pairing, so no
  numeric-comparison prompt ever appears on the thermostat screen — the
  failure mode that put eight prompts on one BRC1H in an hour, through a proxy
  that was not in its allowed list at all.

  Fails open at every step, deliberately: no callback, a callback that raises,
  an empty allowed set, or a path the backend cannot name all pair as before.
  The guard can only ever remove a pairing opportunity, so a broken policy
  must never be the reason a device cannot connect.

- **New `PairingRequiredError` reason: `"unbonded_path"`.** Raised when every
  path chosen was unsanctioned for `UNBONDED_PATH_ROUNDS` (3) consecutive
  rounds, with `evidence` naming the proxy the connection keeps landing on. It
  is a statement about ROUTING, not about a bond: nothing was refused and
  nothing timed out, because nothing was attempted. Consumers must not evict a
  bond on it — the remedy is to pair with the named proxy deliberately.
  `PathVerdict` gains the matching `"unbonded"` value.

## v0.3.11

- **`connected_source` and the per-path evidence now name the path Home
  Assistant actually used, not the candidate we offered.** habluetooth's
  `HaBleakClientWrapper` keeps only the *address* of the `BLEDevice` handed to
  `establish_connection` and re-picks a scanner by RSSI on every connect, so
  the candidate has always been an intention rather than a fact — and the two
  disagree in practice (a thermostat was observed pairing through a proxy that
  had been filtered out of its candidate list entirely). Recording the
  intention as fact is what let a proxy that never carried a session be marked
  as holding a bond, and a refusal be charged to a proxy that was never in the
  conversation. The wrapper publishes the winning scanner once the link is up,
  which is exactly when a bond rejection is raised — by `client.pair()`, after
  `establish_connection` has already returned — so the case that matters is
  attributable.

- **A failure to connect at all is now charged to nobody.** Before a link
  exists nothing names the path, so `PairingRequiredError.evidence` carries a
  `None` key for that attempt and `_rejected_sources` records nothing.
  `tried_sources` still lists what was aimed at, because that is the only thing
  worth telling a human. Consumers already skip falsy sources — but one that
  falls back to `tried_sources` when `evidence` names no proven path must stop
  doing so, or it will re-introduce the guess.

  Deliberate consequence: retained refusal proof can only be applied to a path
  that produced a link, so a device whose connections fail *before*
  establishing reaches a rejection verdict more slowly. Consumers are expected
  to carry a verdict-independent brake for that case.

## v0.3.10

- **`PairingRequiredError` now says WHY it was raised.** The same class was
  raised from two epistemically different places with the same attributes and
  a byte-identical message claiming the device *"refused the authenticated
  bond"*: a path that explicitly rejected the bond (proof — a human must
  re-pair), and every path merely timing out for `PAIRING_TIMEOUT_ROUNDS`
  rounds (an inference — on a bonded path a pairing timeout means congestion).
  Consumers could only tell them apart through an undocumented side channel.
  New attributes:
  - `reason`: `"rejected"` (default, so existing constructor calls keep their
    meaning) or `"timeout_streak"`.
  - `timeout_rounds`: consecutive all-timed-out rounds behind the verdict;
    `0` for a rejection.
  - `evidence`: per-source verdict (`"rejected"` / `"timeout"` /
    `"transient"`), so a consumer can attribute a refusal to a SPECIFIC proxy
    even when several paths were tried.

  The `"timeout_streak"` message no longer claims a refusal: it reports that
  pairing did not complete within the budget on any path and suggests a
  re-pair only if it persists.
- **The ambiguity streak is reset at the streak verdict too.** Only the
  rejection branch reset it, so after an accusation the counter stayed past
  the threshold and the very next classified-timeout round re-raised
  immediately (`4 >= 3`) — including the user-initiated retry that follows,
  which deserves a fresh budget.
- **The fallback single-device path forgives the streak on success.** Only the
  candidate loop did, so a device that recovered through the fallback kept a
  stale streak armed.
- **Proven rejections survive a mixed round.** `every_path_failed_auth` was
  computed from this round alone, so two proven rejections plus one
  transiently-failing third proxy took the transient branch and the proof was
  discarded — one flapping proxy could postpone a legitimate conclusion
  forever. A source that explicitly refused the bond is now remembered until
  it authenticates again. Conservative by construction: only PROVEN
  rejections are retained, never timeouts and never plain transient failures,
  so it never becomes easier to convict a healthy device.
- **Public streak API**, so consumers stop reaching for
  `_pairing_timeout_rounds`: `reset_pairing_timeout_rounds()`,
  `resume_pairing_timeout_rounds(rounds)` (restore a streak carried across
  Connection rebuilds, clamped to >= 0) alongside the existing
  `pairing_timeout_rounds` property, plus a `rejected_sources` view.

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
