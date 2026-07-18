# pymadoka-ng 0.3.6 — typed errors + candidate-list API

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Give callers (the daikin_madoka HA integration) typed connection
errors (`PairingRequiredError`, `DeviceUnreachableError`) and a way to control
*which BLE proxy path* is used for a connection, so the integration can
implement the sticky-proxy strategy and actionable repairs.

**Architecture:** New `pymadoka/errors.py` module with the exception taxonomy
and a pairing-failure classifier. `Connection._connect_via_ha()` gains a
candidate-loop mode: the caller supplies a callback returning an ordered list
of `BLEDevice`s (one per proxy path, preferred first); each is tried in order,
pairing failures are classified per candidate, and the winning path's `source`
is exposed. Foreground `start()` raises typed errors; the background reconnect
task catches them and records `last_error` instead of crashing.

**Tech Stack:** Python 3.11+, bleak / bleak-retry-connector (HA path), pytest
+ pytest-asyncio (existing suite in `tests/`).

**Design doc:** `daikin_madoka/docs/plans/2026-07-18-multi-proxy-robustness-design.md`

**Context notes for the implementer:**
- Repo: `c:\Users\dasim\repos\homeassistant\pymadoka` (GitHub `dasimon135/pymadoka`,
  PyPI dist name **pymadoka-ng**, import name stays `pymadoka`).
- Current branch is `fix/pairing-timeout-message` = open **PR #3** (adds
  `pairing_failure_message()` at `pymadoka/connection.py:27`). Merge it first
  (Task 0); everything below builds on top of it.
- In HA, `BLEDevice.details` is a dict whose `"source"` key is the MAC of the
  proxy/scanner that produced it. The habluetooth client wrapper routes the
  connection through that source's connector, which is what makes per-proxy
  path selection possible. `establish_connection()` retries may "freshen" the
  device to the best path and defeat our choice — keep `max_attempts=2` per
  candidate and verify behavior with mocks (watch-item from the design doc).
- All GitHub-facing text in English.
- PyPI upload uses the user's `~/.pypirc` token (same flow as 0.3.4/0.3.5).

---

### Task 0: Merge PR #3 and branch

**Step 1: Check PR #3 CI is green, then merge**

```bash
gh pr checks 3 --repo dasimon135/pymadoka
gh pr merge 3 --repo dasimon135/pymadoka --merge --delete-branch
```
Expected: merge succeeds (all checks passed).

**Step 2: Update local main and create the feature branch**

```bash
git checkout main && git pull
git checkout -b feat/v0.3.6-typed-errors
```

---

### Task 1: Exception taxonomy (`pymadoka/errors.py`)

**Files:**
- Create: `pymadoka/errors.py`
- Modify: `pymadoka/connection.py` (re-base `ConnectionException`)
- Modify: `pymadoka/__init__.py` (exports)
- Test: `tests/test_errors.py`

**Step 1: Write the failing test**

```python
# tests/test_errors.py
from pymadoka.errors import (
    MadokaError,
    PairingRequiredError,
    DeviceUnreachableError,
)
from pymadoka.connection import ConnectionException


def test_hierarchy():
    assert issubclass(ConnectionException, MadokaError)
    assert issubclass(PairingRequiredError, MadokaError)
    assert issubclass(DeviceUnreachableError, MadokaError)


def test_pairing_required_carries_context():
    err = PairingRequiredError(
        "F0:B3:1E:87:AF:FE", tried_sources=["AA:BB:CC:DD:EE:01", None]
    )
    assert err.address == "F0:B3:1E:87:AF:FE"
    assert err.tried_sources == ["AA:BB:CC:DD:EE:01", None]
    assert "F0:B3:1E:87:AF:FE" in str(err)


def test_device_unreachable_carries_address():
    err = DeviceUnreachableError("F0:B3:1E:87:AF:FE")
    assert err.address == "F0:B3:1E:87:AF:FE"
    assert "F0:B3:1E:87:AF:FE" in str(err)
```

**Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_errors.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pymadoka.errors'`

**Step 3: Implement**

```python
# pymadoka/errors.py
"""Typed errors so callers can distinguish failure modes.

The HA integration maps these to actionable repair issues:
PairingRequiredError -> "confirm the pairing prompt on the thermostat screen";
DeviceUnreachableError -> "device out of range / no proxy sees it".
"""
from __future__ import annotations

from typing import Optional, Sequence


class MadokaError(Exception):
    """Base class for all pymadoka errors."""


class PairingRequiredError(MadokaError):
    """Every attempted path refused the authenticated bond.

    tried_sources lists the proxy source MACs that were attempted
    (None entries = local adapter / unknown source).
    """

    def __init__(self, address: str, tried_sources: Optional[Sequence[Optional[str]]] = None):
        self.address = address
        self.tried_sources = list(tried_sources or [])
        via = ", ".join(str(s) for s in self.tried_sources) or "unknown"
        super().__init__(
            f"{address} refused the authenticated bond on every attempted "
            f"path (tried via: {via}) — confirm the pairing prompt on the "
            "thermostat screen"
        )


class DeviceUnreachableError(MadokaError):
    """No BLE path to the device (out of range / no proxy sees it)."""

    def __init__(self, address: str):
        self.address = address
        super().__init__(f"No BLE path to {address}: device not seen by any adapter/proxy")
```

In `pymadoka/connection.py`, replace the `ConnectionException` definition:

```python
from pymadoka.errors import MadokaError, PairingRequiredError, DeviceUnreachableError

class ConnectionException(MadokaError):
    """Generic connection/protocol failure (legacy name, kept for compat)."""
    pass
```

In `pymadoka/__init__.py`, add to imports and `__all__`:
`MadokaError`, `PairingRequiredError`, `DeviceUnreachableError`
(import from `.errors`).

**Step 4: Run tests**

Run: `python -m pytest tests/ -v`
Expected: all PASS (new + existing).

**Step 5: Commit**

```bash
git add pymadoka/errors.py pymadoka/connection.py pymadoka/__init__.py tests/test_errors.py
git commit -m "feat: typed error taxonomy (MadokaError, PairingRequiredError, DeviceUnreachableError)"
```

---

### Task 2: Pairing-failure classifier

**Files:**
- Modify: `pymadoka/errors.py`
- Test: `tests/test_errors.py`

**Step 1: Write the failing test** (append to `tests/test_errors.py`)

```python
import pytest
from bleak.exc import BleakError
from pymadoka.errors import is_pairing_error


@pytest.mark.parametrize(
    "exc,expected",
    [
        # ESPHome proxy GATT rejection (exact shape seen in HA logs)
        (BleakError(
            "Bluetooth GATT Error address=F0:B3:1E:87:AF:FE handle=515 "
            "error=5 description=Insufficient authentication"), True),
        # esp32_ble_client pairing failure (error 97 seen in HA logs)
        (BleakError("Pairing failed due to error: 97"), True),
        (BleakError("Insufficient Encryption"), True),
        # pair() timeout = prompt sitting unanswered on the screen
        (TimeoutError(), True),
        # NOT pairing problems:
        (BleakError("Device disconnected"), False),
        (BleakError("No backend with an available connection slot"), False),
        (ConnectionError("boom"), False),
    ],
)
def test_is_pairing_error(exc, expected):
    assert is_pairing_error(exc) is expected
```

**Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_errors.py -v -k pairing_error`
Expected: FAIL — `ImportError: cannot import name 'is_pairing_error'`

**Step 3: Implement** (append to `pymadoka/errors.py`)

```python
# Substrings (lowercased) that identify an authentication/bonding rejection
# in bleak / bleak-esphome error text. String matching is deliberate: the
# GATT status only survives as text through the proxy stack.
_PAIRING_ERROR_MARKERS = (
    "insufficient authentication",
    "insufficient encryption",
    "pairing failed",
    "authentication failed",
    "error=5 ",          # ATT error 0x05 = insufficient authentication
)


def is_pairing_error(exc: BaseException) -> bool:
    """True if the exception denotes a missing/refused authenticated bond.

    A TimeoutError from pair() counts: it almost always means the numeric
    comparison prompt is sitting unanswered on the thermostat screen.
    """
    if isinstance(exc, TimeoutError):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _PAIRING_ERROR_MARKERS)
```

**Step 4: Run tests**

Run: `python -m pytest tests/ -v`
Expected: all PASS.

**Step 5: Commit**

```bash
git add pymadoka/errors.py tests/test_errors.py
git commit -m "feat: is_pairing_error() classifier for auth/bond failures"
```

---

### Task 3: Candidates plumbing (constructor + `connected_source`)

**Files:**
- Modify: `pymadoka/connection.py` (`Connection.__init__`)
- Modify: `pymadoka/controller.py` (`Controller.__init__` pass-through)
- Test: `tests/test_candidates.py`

**Step 1: Write the failing test**

```python
# tests/test_candidates.py
from pymadoka import Controller


def test_candidates_callback_reaches_connection():
    marker = lambda: []  # noqa: E731
    ctrl = Controller("00:11:22:33:44:55", candidates_callback=marker)
    assert ctrl.connection.candidates_callback is marker
    assert ctrl.connection.connected_source is None


def test_candidates_callback_defaults_to_none():
    ctrl = Controller("00:11:22:33:44:55")
    assert ctrl.connection.candidates_callback is None
```

**Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_candidates.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument`

**Step 3: Implement**

`Connection.__init__` gains `candidates_callback=None` (after `name`), stores
`self.candidates_callback = candidates_callback`, `self.connected_source = None`,
`self.last_error = None`. `Controller.__init__` gains the same kwarg and passes
it through to `Connection(...)`. Docstrings: *"callback returning an ordered
list of BLEDevice candidates (preferred path first); when provided, the HA
connect path tries them in order instead of letting HA pick by RSSI."*

**Step 4: Run tests**

Run: `python -m pytest tests/ -v` — all PASS.

**Step 5: Commit**

```bash
git add pymadoka/connection.py pymadoka/controller.py tests/test_candidates.py
git commit -m "feat: candidates_callback plumbing + connected_source/last_error attributes"
```

---

### Task 4: Candidate loop in `_connect_via_ha`

The core. Current code (post-PR#3) at `pymadoka/connection.py:163-234`:
single device from `async_ble_device_from_address`, pair failure only logged.

**Files:**
- Modify: `pymadoka/connection.py`
- Test: `tests/test_candidates.py`

**Step 1: Write the failing tests** (append to `tests/test_candidates.py`)

Build a small mock kit at module level:

```python
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from bleak.exc import BleakError

from pymadoka.connection import Connection
from pymadoka.errors import PairingRequiredError, DeviceUnreachableError

AUTH_FAIL = BleakError(
    "Bluetooth GATT Error address=00:11:22:33:44:55 handle=515 "
    "error=5 description=Insufficient authentication"
)


def make_device(source):
    return SimpleNamespace(
        address="00:11:22:33:44:55", name="Daikin", details={"source": source}
    )


def make_client(pair_exc=None):
    client = AsyncMock()
    client.is_connected = True
    client.pair = AsyncMock(side_effect=pair_exc)
    client.start_notify = AsyncMock()
    client.disconnect = AsyncMock()
    return client


def make_connection(candidates):
    conn = Connection(
        "00:11:22:33:44:55", adapter=None, reconnect=False,
        hass=object(), candidates_callback=lambda: list(candidates),
    )
    return conn


@pytest.mark.asyncio
async def test_first_candidate_wins():
    good = make_client()
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(return_value=good)) as est:
        conn = make_connection([make_device("PROXY_A"), make_device("PROXY_B")])
        await conn._connect_via_ha()
    assert conn.connected_source == "PROXY_A"
    assert est.await_count == 1  # never touched PROXY_B


@pytest.mark.asyncio
async def test_auth_failure_falls_through_to_next_candidate():
    bad = make_client(pair_exc=AUTH_FAIL)
    good = make_client()
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(side_effect=[bad, good])):
        conn = make_connection([make_device("PROXY_A"), make_device("PROXY_B")])
        await conn._connect_via_ha()
    assert conn.connected_source == "PROXY_B"
    bad.disconnect.assert_awaited()  # failed path is not left half-open


@pytest.mark.asyncio
async def test_all_candidates_auth_fail_raises_pairing_required():
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(side_effect=[make_client(pair_exc=AUTH_FAIL),
                                      make_client(pair_exc=AUTH_FAIL)])):
        conn = make_connection([make_device("PROXY_A"), make_device("PROXY_B")])
        with pytest.raises(PairingRequiredError) as ei:
            await conn._connect_via_ha()
    assert ei.value.tried_sources == ["PROXY_A", "PROXY_B"]
    assert isinstance(conn.last_error, PairingRequiredError)


@pytest.mark.asyncio
async def test_empty_candidates_raises_unreachable():
    conn = make_connection([])
    with pytest.raises(DeviceUnreachableError):
        await conn._connect_via_ha()
    assert isinstance(conn.last_error, DeviceUnreachableError)
```

Note: `pytest-asyncio` may need adding to dev deps — check
`pyproject.toml` `[project.optional-dependencies]`; existing async tests
(`tests/test_setpoint.py`) show the current convention — mirror it.

**Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_candidates.py -v`
Expected: FAIL — candidate mode not implemented (`_connect_via_ha` calls
`async_ble_device_from_address`, imports homeassistant → ImportError in the
non-candidate branch is fine, tests only exercise the candidate branch).

**Step 3: Implement the candidate loop**

Restructure `_connect_via_ha` as: if `self.candidates_callback` is None →
existing single-device behavior, byte-for-byte (no regression for callers that
don't opt in). Otherwise:

```python
async def _connect_via_ha(self):
    if self.candidates_callback is None:
        return await self._connect_via_ha_single()   # extracted old body
    from bleak_retry_connector import establish_connection

    candidates = list(self.candidates_callback())
    if not candidates:
        self.last_error = DeviceUnreachableError(self.address)
        self.connection_status = ConnectionStatus.ABORTED
        raise self.last_error

    tried, auth_failures = [], 0
    for ble_device in candidates:
        source = None
        if isinstance(getattr(ble_device, "details", None), dict):
            source = ble_device.details.get("source")
        tried.append(source)
        client = None
        try:
            client = await establish_connection(
                BleakClient, ble_device, self.address,
                disconnected_callback=self.on_disconnect, max_attempts=2,
            )
            await asyncio.wait_for(client.pair(), timeout=8.0)
            await client.start_notify(NOTIFY_CHAR_UUID, self.notification_handler)
            await asyncio.sleep(1.5)   # let bond + subscription settle
            if not client.is_connected:
                raise ConnectionException(
                    f"{self.address} dropped the link right after connecting")
            self.client = client
            self._paired = True
            self.connected_source = source
            self.connection_status = ConnectionStatus.CONNECTED
            self.last_error = None
            self._retry_delay = 5.0
            logger.info(f"Connected to {self.address} ({self.name}) via {source or 'local adapter'}")
            return
        except CancelledError:
            if client is not None:
                asyncio.get_event_loop().create_task(self._disconnect_client(client))
            raise
        except Exception as e:  # noqa: BLE001
            if client is not None:
                asyncio.get_event_loop().create_task(self._disconnect_client(client))
            if is_pairing_error(e):
                auth_failures += 1
                logger.info(
                    f"{self.address}: path via {source or 'local adapter'} "
                    f"needs pairing ({pairing_failure_message(self.address, e)}), "
                    "trying next path")
            else:
                logger.warning(f"{self.address}: path via {source or 'local adapter'} failed: {e}")

    if auth_failures == len(candidates):
        self.last_error = PairingRequiredError(self.address, tried_sources=tried)
        self.connection_status = ConnectionStatus.ABORTED
        raise self.last_error
    # Mixed/transient failures: keep the outer start() loop retrying as today.
    await asyncio.sleep(self._retry_delay)
    self._retry_delay = min(self._retry_delay * 2, 60.0)
```

Also extract the current body into `_connect_via_ha_single()` unchanged.

**Step 4: Run the full suite**

Run: `python -m pytest tests/ -v`
Expected: all PASS (candidate tests + legacy tests untouched).

**Step 5: Commit**

```bash
git add pymadoka/connection.py tests/test_candidates.py pyproject.toml
git commit -m "feat: candidate-list connect loop with per-path pairing classification"
```

---

### Task 5: Typed errors must escape `start()` but not crash the background reconnect

`start()`'s loop catches `Exception` broadly (`connection.py:157`) — a raised
`PairingRequiredError` would be swallowed into ABORTED. And `on_disconnect`
fires `asyncio.create_task(self.start())` where a raise would be an unhandled
task exception.

**Files:**
- Modify: `pymadoka/connection.py` (`start`, `on_disconnect`)
- Test: `tests/test_candidates.py`

**Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_start_propagates_pairing_required():
    with patch("bleak_retry_connector.establish_connection",
               AsyncMock(return_value=make_client(pair_exc=AUTH_FAIL))):
        conn = make_connection([make_device("PROXY_A")])
        with pytest.raises(PairingRequiredError):
            await conn.start()


@pytest.mark.asyncio
async def test_background_reconnect_swallows_typed_errors(caplog):
    conn = make_connection([])
    conn.reconnect = True
    await conn._background_start()          # must NOT raise
    assert isinstance(conn.last_error, DeviceUnreachableError)
```

**Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_candidates.py -v -k "propagates or background"`
Expected: FAIL (`MadokaError` swallowed / `_background_start` missing).

**Step 3: Implement**

- In `start()`'s loop, add before the generic handler:
  ```python
  except MadokaError:
      raise          # typed errors are the caller's signal — never swallow
  ```
- Add:
  ```python
  async def _background_start(self):
      """start() wrapper for fire-and-forget reconnects: never raises."""
      try:
          await self.start()
      except MadokaError as e:
          logger.warning(f"Background reconnect for {self.address} gave up: {e}")
  ```
- In `on_disconnect`, replace `asyncio.create_task(self.start())` with
  `asyncio.create_task(self._background_start())`.

**Step 4: Run the full suite** — all PASS.

**Step 5: Commit**

```bash
git add pymadoka/connection.py tests/test_candidates.py
git commit -m "feat: typed errors propagate from start(), background reconnect records last_error"
```

---

### Task 6: Version bump, exports check, changelog

**Files:**
- Modify: `pyproject.toml` (version `0.3.5` → `0.3.6`)
- Modify: `CHANGELOG.md` (check filename — create if absent)

**Step 1: Bump + changelog**

CHANGELOG entry (English):

```markdown
## v0.3.6

- **Typed errors**: `MadokaError` base, `PairingRequiredError` (carries the
  attempted proxy sources), `DeviceUnreachableError`; `ConnectionException`
  now subclasses `MadokaError`. `is_pairing_error()` classifies auth/bond
  failures (GATT "Insufficient authentication", pairing error 97, pair timeout).
- **Candidate-list API**: `Controller(..., candidates_callback=...)` — the
  caller supplies an ordered list of `BLEDevice` paths (preferred proxy
  first); each is tried in order and `connection.connected_source` reports
  the winning proxy. Enables sticky-proxy behavior in Home Assistant.
- **Explicit pairing-timeout message** (PR #3): a pair() timeout now says
  "confirm the pairing prompt on the thermostat screen".
```

**Step 2: Full suite one last time**

Run: `python -m pytest tests/ -v` — all PASS.

**Step 3: Commit + PR**

```bash
git add -A && git commit -m "chore(release): v0.3.6"
git push -u origin feat/v0.3.6-typed-errors
gh pr create --repo dasimon135/pymadoka --title "0.3.6: typed errors + candidate-list connect API" \
  --body "<summary of the three bullets above>"
```
Wait for CI green → merge.

---

### Task 7: Publish 0.3.6 to PyPI + GitHub release

**Step 1: Build & upload** (from updated main)

```bash
git checkout main && git pull
python -m build
python -m twine upload dist/pymadoka_ng-0.3.6*
```
Expected: `View at https://pypi.org/project/pymadoka-ng/0.3.6/`
(Token comes from `~/.pypirc`; same flow as 0.3.4/0.3.5.)

**Step 2: Tag + GitHub release**

```bash
gh release create v0.3.6 --repo dasimon135/pymadoka --target main \
  --title "v0.3.6" --notes "<changelog bullets>"
```

**Step 3: Sanity-install check**

```bash
pip install --no-cache-dir pymadoka-ng==0.3.6 -t /tmp/pmk-check
python -c "import sys; sys.path.insert(0,'/tmp/pmk-check'); from pymadoka import PairingRequiredError; print('ok')"
```
Expected: `ok`

---

**Done criteria:** PyPI serves 0.3.6; `from pymadoka import PairingRequiredError,
DeviceUnreachableError` works; full pytest suite green; PR #3 content included.
The daikin_madoka v3.2.0 integration work (separate plan, same design doc) can
then pin `pymadoka-ng==0.3.6`.
