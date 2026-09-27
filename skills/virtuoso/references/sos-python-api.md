# SOS Cellview API

Virtuoso Bridge can operate on one explicit Cliosoft SOS cellview through the
same profile and GUI-host connection used by the CIW. SOS is optional: normal
Bridge use has no SOS dependency, and SOS calls return an actionable blocked
result when the executable or Cadence integration is unavailable.

## Configuration

The SOS executable is resolved on the GUI host in this order:

1. the `soscmd=` argument;
2. `VB_SOS_COMMAND_<profile>`;
3. `VB_SOS_COMMAND`;
4. `soscmd` on the remote `PATH`;
5. `$CLIOSOFT_DIR/bin/soscmd`.

An explicit value may be an absolute executable or a command name/site wrapper.
It is never silently replaced with another installation after a failure. Plain
TCP clients and native Windows local clients do not prove access to the CIW
filesystem. A same-host POSIX client must explicitly set
`VB_SOS_LOCAL_FILESYSTEM=1`; do not use that setting for an SSH-forwarded
loopback connection.

## Python API

```python
from virtuoso_bridge import VirtuosoClient

client = VirtuosoClient.from_env(profile="lab")

status = client.sos.status_cellview("LIB", "CELL", "schematic")
checkout = client.sos.checkout_cellview(
    "LIB", "CELL", "schematic", dry_run=True,
)
checkin = client.sos.checkin_cellview(
    "LIB", "CELL", "schematic", message="Fix gain", dry_run=True,
)
initial = client.sos.register_cellview(
    "LIB", "NEW_CELL", "schematic", message="Initial version", dry_run=True,
)
```

`status_cellview` is read-only for any resolvable view. Writes are limited to
saved OpenAccess `schematic`, `schematicSymbol`, and `maskLayout` views. Maestro,
config, Verilog, and other view types remain read-only. Checkin and registration
reject any target whose logical name or resolved path contains `calibre`.

The API resolves the real view directory and master file in the connected CIW,
verifies the SOS workarea and exact status row, rejects unsaved buffers, and
rechecks target identity immediately before a write. Checkout uses `ddCheckout`.
Checkin and initial registration prefer the installed SOS Design Manager flow
when its runtime capabilities are present. If that native flow is unavailable
while a Maestro session is open, the operation is blocked instead of falling
back to `ddCheckin`, because direct GDM callbacks can change an unrelated ADE
session mode on some IC/SOS releases.

Results contain `target`, `before`, `after`, `outcome`, and `diagnostics`.
`success`, `noop`, and `dry_run` set `ok=True`. A timeout or lost response after
dispatch is `unknown`: Bridge queries fresh SOS state but never resends the
mutation and never claims that a matching state proves which request completed.
Use `reconcile_cellview(..., receipt=previous_result)` for a read-only follow-up.

## CLI

```bash
virtuoso-bridge sos status LIB CELL schematic -p lab --json
virtuoso-bridge sos co LIB CELL schematic --dry-run -p lab --json
virtuoso-bridge sos ci LIB CELL schematic -m "Fix gain" --dry-run -p lab --json
virtuoso-bridge sos register LIB NEW_CELL schematic -m "Initial version" --dry-run -p lab --json
virtuoso-bridge sos doctor LIB CELL schematic -p lab --json
virtuoso-bridge sos reconcile LIB CELL schematic --receipt unknown.json -p lab --json
```

CLI exit codes are `0` for success/noop/dry-run, `1` for blocked or failed, and
`3` for unknown. `doctor` is read-only and does not acquire a license, save,
checkout, checkin, run history, or create diff reports.

## Ordinary SOS Files

`client.sos.attach(workarea)` exposes status, object status, history, diff,
checkout, and checkin for explicitly named ordinary files. OA packages,
`.oa`/`master.tag` paths, symlinks, directories, and Calibre-related targets are
rejected by the write methods; use the cellview API for OA data.
