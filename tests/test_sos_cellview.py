from __future__ import annotations

import json
import shlex

import pytest

from virtuoso_bridge.models import ExecutionStatus, VirtuosoResult
from virtuoso_bridge.transport.ssh import CommandResult
from virtuoso_bridge.virtuoso.sos import SOSOps, SOSCellViewTarget
from virtuoso_bridge.virtuoso.sos.cellview import _skill


ROOT = "/work area"
PATH = "lib/cell/schematic_Vt"
DIRECTORY = f"{ROOT}/{PATH}"
MASTER = f"{DIRECTORY}/sch.oa"


def _skill_ok(output):
    return VirtuosoResult(status=ExecutionStatus.SUCCESS, output=output)


def _state(state="O", change="M", revision="3", *, lock="-", path=PATH, kind="p"):
    return CommandResult(0, f"{kind}\t{state}\t{change}\t{lock}\t-\t-\t{revision}\t./{path}\n", "")


class _Client:
    _timeout = 60

    def __init__(self, *, before=None, after=None, dirty=False, view_type="schematic",
                 mutation=None, native=False, native_dispatch=None, native_result=None,
                 maestro=False):
        self.sos_runner = self
        self.commands = []
        self.skill_calls = []
        self.states = iter([
            before if before is not None else _state(),
            after if after is not None else _state("-", "-", "4"),
        ])
        self.resolution = _skill_ok(
            f'("ok" {json.dumps(DIRECTORY)} {json.dumps(MASTER)} '
            f'{json.dumps(view_type)} {"t" if dirty else "nil"})'
        )
        self.mutation = mutation if mutation is not None else _skill_ok('("ok")')
        self.native = native
        self.native_dispatch = (native_dispatch if native_dispatch is not None
                                else _skill_ok('("scheduled" "sos_design_manager")'))
        self.native_result = (native_result if native_result is not None
                              else _skill_ok('("ok" t t t nil)'))
        self.maestro = maestro
        self.root = CommandResult(0, ROOT + "\n", "")
        self.sos = SOSOps(self)

    def run_command(self, command, *, timeout=None):
        self.commands.append(command)
        if command.startswith("vb_sos="):
            return CommandResult(0, "/opt/sos/bin/soscmd\n", "")
        parts = shlex.split(command)
        assert parts[:2] == ["cd", DIRECTORY if "findwaroot" in parts else ROOT]
        if parts[-1] == "findwaroot":
            return self.root
        assert parts[3:6] == ["/opt/sos/bin/soscmd", "status", "-Nhdr"]
        assert parts[-1] == "./" + PATH
        state = next(self.states)
        if isinstance(state, Exception):
            raise state
        return state

    def execute_skill(self, code, timeout=None, *, retry_connect=True):
        if not retry_connect:
            self.skill_calls.append((code, {"one_shot": True}))
            if "VB_SOS_NATIVE_DISPATCH" in code:
                if isinstance(self.native_dispatch, Exception):
                    raise self.native_dispatch
                return self.native_dispatch
            if isinstance(self.mutation, Exception):
                raise self.mutation
            return self.mutation
        if "ddCheckin(" in code or "ddCheckout(" in code:
            raise AssertionError("mutations must disable connection retries")
        self.skill_calls.append((code, {}))
        if "VB_SOS_NATIVE_PROBE" in code:
            return _skill_ok(f'("ok" {"t" if self.native else "nil"})')
        if "VB_SOS_NATIVE_RESULT" in code:
            if isinstance(self.native_result, Exception):
                raise self.native_result
            if isinstance(self.native_result, list):
                if len(self.native_result) > 1:
                    return self.native_result.pop(0)
                return self.native_result[0]
            return self.native_result
        if "VB_SOS_MAESTRO_PROBE" in code:
            return _skill_ok(f'("ok" {"t" if self.maestro else "nil"})')
        return self.resolution

    @property
    def writes(self):
        return [code for code, _ in self.skill_calls
                if "ddCheckin(" in code or "ddCheckout(" in code
                or "VB_SOS_NATIVE_DISPATCH" in code]


def _ci(client, **kwargs):
    return client.sos.checkin_cellview("lib", "cell", "schematic_Vt", message="Saved changes", **kwargs)


def _unmanaged(kind="d", path=PATH):
    return CommandResult(0, f"{kind}\t?\t?\t?\t?\t?\t?\t./{path}\n", "")


def _registration_client(*, states=None, **kwargs):
    client = _Client(**kwargs)
    client.states = iter(states if states is not None else [
        _unmanaged(), _unmanaged(), _state("-", "-", "1"),
    ])
    return client


def _register(client, **kwargs):
    return client.sos.register_cellview("lib", "cell", "schematic_Vt", message="Initial version", **kwargs)


@pytest.mark.parametrize("kind", ["d", "p"])
@pytest.mark.parametrize("view_type", ["schematic", "schematicSymbol", "maskLayout"])
def test_register_unmanaged_oa_view_via_gdm(kind, view_type):
    client = _registration_client(view_type=view_type, states=[
        _unmanaged(kind), _unmanaged(kind), _state("-", "-", "1"),
    ])
    result = _register(client)
    assert result.outcome == "success" and result.action == "register"
    assert result.before.state == "?" and result.before.revision == "?"
    assert result.after.revision == "1" and result.after.object_type == "p"
    assert len(client.writes) == 1
    assert 'ddCheckin(vbFile "Initial version")' in client.writes[0]
    assert "ddCheckout(" not in client.writes[0]
    status_commands = [shlex.split(cmd) for cmd in client.commands if "status" in cmd]
    assert len(status_commands) == 3
    assert all(parts[-3:] == ["-sall", "-sNr", "./" + PATH] for parts in status_commands)
    assert all("-sr" not in parts for parts in status_commands)


def test_register_dry_run_reads_explicit_unmanaged_state_without_write():
    client = _registration_client(states=[_unmanaged()])
    result = _register(client, dry_run=True)
    assert result.outcome == "dry_run" and result.before.revision == "?"
    assert not client.writes
    assert "'ddCheckin" in client.skill_calls[0][0]


@pytest.mark.parametrize("before", [
    _state("-", "-", "1"), _state("O", "M", "1"), _state("N", "?", "?"),
    _state("X", "?", "?"), _state("W", "M", "1"), _state(lock="L"),
    _unmanaged("D"), _unmanaged("P"), _unmanaged("s"), _unmanaged("f"),
    _state("?", "?", "1", kind="d", lock="?"),
])
def test_register_rejects_existing_reference_unpopulated_and_ambiguous_objects(before):
    client = _registration_client(states=[before])
    result = _register(client)
    assert result.outcome == "blocked"
    assert not client.writes


@pytest.mark.parametrize("response,expected", [
    (CommandResult(0, "!! Warning: No objects selected to display status.\n", ""), "blocked"),
    (CommandResult(0, "", ""), "failed"),
    (CommandResult(1, "", "License unavailable"), "failed"),
    (CommandResult(0, _unmanaged().stdout * 2, ""), "failed"),
    (_unmanaged(path=PATH + "/sibling"), "failed"),
    (TimeoutError("status timed out"), "failed"),
])
def test_register_never_treats_missing_or_failed_status_as_new(response, expected):
    client = _registration_client(states=[response])
    result = _register(client)
    assert result.outcome == expected and not client.writes


def test_register_detects_changed_state_on_final_refresh():
    client = _registration_client(states=[_unmanaged(), _state("-", "-", "1")])
    result = _register(client)
    assert result.outcome == "blocked" and "changed" in result.diagnostics[0]
    assert not client.writes


@pytest.mark.parametrize("view_type", ["maestro", "config", "verilogA"])
def test_register_rejects_unsupported_views_before_cli(view_type):
    client = _registration_client(view_type=view_type)
    assert _register(client).outcome == "blocked"
    assert not client.writes and not client.commands


def test_register_rejects_dirty_view_before_cli():
    client = _registration_client(dirty=True)
    assert _register(client).outcome == "blocked"
    assert not client.writes and not client.commands


@pytest.mark.parametrize("target", [
    ("Calibre", "cell", "layout"), ("lib", "calibre_cell", "symbol"),
    ("lib", "cell", "Calibre_PEX"),
])
def test_register_cannot_bypass_calibre_guard(target):
    client = _registration_client()
    result = client.sos.register_cellview(*target, message="Initial version")
    assert result.outcome == "blocked"
    assert not client.skill_calls and not client.commands


def test_register_rejects_resolved_calibre_path():
    client = _registration_client()
    client.resolution = _skill_ok('("ok" "/work/calibre/view" "/work/calibre/view/sch.oa" "schematic" nil)')
    assert _register(client).outcome == "blocked"
    assert not client.writes and not client.commands


@pytest.mark.parametrize("message", ["", " ", "two\nlines", "bad\x00"])
def test_register_requires_message_before_any_remote_call(message):
    client = _registration_client()
    with pytest.raises(ValueError):
        client.sos.register_cellview("lib", "cell", "schematic", message=message)
    assert not client.skill_calls and not client.commands


@pytest.mark.parametrize("after", [
    _unmanaged(), _state("-", "-", "?"), _state("-", "-", "-"),
    _state("-", "-", "invalid"), _state("-", "-", " 1 "),
    _state("O", "-", "1"), _state("-", "M", "1"),
    _state("-", "-", "1", kind="d"), _state("-", "-", "1", lock="L"),
])
def test_register_requires_confirmed_managed_checked_in_postcondition(after):
    client = _registration_client(states=[_unmanaged(), _unmanaged(), after])
    assert _register(client).outcome == "failed"
    assert len(client.writes) == 1


def test_register_does_not_hardcode_first_revision_number():
    client = _registration_client(states=[_unmanaged(), _unmanaged(), _state("-", "-", "1.1")])
    assert _register(client).ok


@pytest.mark.parametrize("mutation", [TimeoutError("response lost"), ConnectionResetError("reset")])
def test_register_lost_response_reads_state_but_never_retries(mutation):
    client = _registration_client(mutation=mutation)
    result = _register(client)
    assert result.outcome == "unknown" and result.after.revision == "1"
    assert len(client.writes) == 1


def test_register_gdm_failure_is_not_masked_by_post_state():
    client = _registration_client(mutation=_skill_ok('("failed" "GDM returned nil.")'))
    assert _register(client).outcome == "failed"
    assert len(client.writes) == 1


def test_register_missing_post_state_is_unknown():
    client = _registration_client(states=[_unmanaged(), _unmanaged(), TimeoutError("status")])
    assert _register(client).outcome == "unknown"
    assert len(client.writes) == 1


def test_register_repeat_is_blocked_without_another_revision():
    client = _registration_client(states=[_state("-", "-", "1")])
    assert _register(client).outcome == "blocked"
    assert not client.writes


def test_normal_checkin_still_rejects_unmanaged_view():
    client = _Client(before=_unmanaged())
    assert _ci(client).outcome == "blocked"
    assert not client.writes


@pytest.mark.parametrize('notice', [
    '** The flags and attributes have been updated.\n',
    "## All servers for project 'FCGMASH' are online.\n",
])
def test_custom_view_status_resolves_workarea_and_refresh_notice(notice):
    state = _state("-", "-", "4")
    client = _Client(before=state._replace(stdout=state.stdout + notice))
    result = client.sos.status_cellview("lib", "cell", "schematic_Vt")
    assert result.ok
    assert result.target.view_type == "schematic"
    assert result.target.workarea == ROOT
    assert result.target.master == MASTER
    assert result.before.revision == "4"
    assert not client.writes


@pytest.mark.parametrize('notice', [
    "## All servers for project 'FCGMASH' are offline.\n",
    "## ERROR: cache unavailable\n",
    "## All servers for project 'FCGMASH' are online. unexpected\n",
])
def test_unknown_notices_remain_errors(notice):
    state = _state('-', '-', '4')
    client = _Client(before=state._replace(stdout=state.stdout + notice))
    assert not client.sos.status_cellview('lib', 'cell', 'schematic_Vt').ok
    assert not client.writes


@pytest.mark.parametrize("action", ["co", "ci"])
def test_dry_run_does_not_dispatch_gdm(action):
    client = _Client(before=_state("-", "-") if action == "co" else _state())
    result = (client.sos.checkout_cellview("lib", "cell", "schematic_Vt", dry_run=True)
              if action == "co" else _ci(client, dry_run=True))
    assert result.outcome == "dry_run"
    assert not client.writes


def test_checkin_checks_gdm_and_revision_postcondition():
    client = _Client()
    result = _ci(client)
    assert result.outcome == "success"
    assert result.before.revision == "3" and result.after.revision == "4"
    assert len(client.writes) == 1
    assert 'ddCheckin(vbFile "Saved changes")' in client.writes[0]
    assert json.loads(json.dumps(result.to_dict()))["ok"] is True


def test_checkin_prefers_sos_design_manager_when_available():
    client = _Client(native=True)
    result = _ci(client)
    assert result.outcome == "success"
    assert result.before.revision == "3" and result.after.revision == "4"
    assert len(client.writes) == 1
    assert "VB_SOS_NATIVE_DISPATCH" in client.writes[0]
    assert "ddCheckin(" not in client.writes[0]
    assert '"lib" "cell" "schematic_Vt"' in client.writes[0]
    assert 'vbForm->descItem->value="Saved changes"' in client.writes[0]
    assert "vbSosBridgeFormsBefore=hiFormList()" in client.writes[0]
    assert "vbForm->cellViewListBox->choices" in client.writes[0]
    assert "hiIsFormDisplayed(vbForm)" in client.writes[0]
    assert "hiSetCurrentForm(vbForm)" in client.writes[0]
    assert "SosObjStatusCreateFormMain" not in client.writes[0]


def test_native_checkin_lost_dispatch_reply_is_unknown_and_not_retried():
    client = _Client(native=True, native_dispatch=TimeoutError("response lost"))
    result = _ci(client)
    assert result.outcome == "unknown" and result.after.revision == "4"
    assert len(client.writes) == 1
    assert not any("VB_SOS_NATIVE_RESULT" in code for code, _ in client.skill_calls)


def test_native_checkin_requires_completion_receipt():
    client = _Client(native=True, native_result=TimeoutError("completion unavailable"))
    result = _ci(client, timeout=0.01)
    assert result.outcome == "unknown" and result.after.revision == "4"
    assert len(client.writes) == 1


def test_native_failed_completion_stops_polling_without_retry():
    before = _state()
    client = _Client(
        before=before,
        after=before,
        native=True,
        native_result=_skill_ok('("ok" t nil nil "SOS form failed.")'),
    )
    result = _ci(client, timeout=5)
    assert result.outcome == "failed"
    assert result.diagnostics == ("SOS form failed.",)
    assert len(client.writes) == 1
    status_commands = [command for command in client.commands if " status " in command]
    assert len(status_commands) == 2


def test_native_waits_for_callback_after_sos_state_changes():
    complete = _skill_ok('("ok" t t t nil)')
    client = _Client(
        native=True,
        native_result=[_skill_ok('("ok" nil t nil nil)'), complete, complete],
    )
    client.states = iter([_state(), _state("-", "-", "4"), _state("-", "-", "4")])
    result = _ci(client)
    assert result.outcome == "success"
    status_commands = [command for command in client.commands if " status " in command]
    assert len(status_commands) == 3


def test_gdm_fallback_is_blocked_while_maestro_is_open():
    client = _Client(native=False, maestro=True)
    result = _ci(client)
    assert result.outcome == "blocked"
    assert "Maestro" in result.diagnostics[0]
    assert not client.writes


def test_register_prefers_sos_design_manager_when_available():
    client = _registration_client(native=True)
    result = _register(client)
    assert result.outcome == "success" and result.after.revision == "1"
    assert len(client.writes) == 1
    assert "VB_SOS_NATIVE_DISPATCH" in client.writes[0]
    assert 'vbForm->descItem->value="Initial version"' in client.writes[0]


def test_checkout_verified():
    client = _Client(before=_state("-", "-"), after=_state("O", "-"))
    result = client.sos.checkout_cellview("lib", "cell", "schematic_Vt")
    assert result.ok and result.after.state == "O"
    assert len(client.writes) == 1


@pytest.mark.parametrize("view_type", ["schematic", "schematicSymbol", "maskLayout"])
@pytest.mark.parametrize("action", ["co", "ci"])
def test_supported_oa_types_use_gdm(view_type, action):
    client = _Client(view_type=view_type,
                     before=_state("-", "-") if action == "co" else _state(),
                     after=_state("O", "-") if action == "co" else _state("-", "-", "4"))
    result = (_ci(client) if action == "ci"
              else client.sos.checkout_cellview("lib", "cell", "custom_view"))
    assert result.outcome == "success"
    assert len(client.writes) == 1


@pytest.mark.parametrize("view_type", ["maestro", "config", "verilog", "verilogA"])
@pytest.mark.parametrize("action", ["status", "co", "ci"])
def test_other_types_are_read_only(view_type, action):
    client = _Client(view_type=view_type)
    if action == "status":
        result = client.sos.status_cellview("lib", "cell", "custom_view")
    elif action == "co":
        result = client.sos.checkout_cellview("lib", "cell", "custom_view")
    else:
        result = _ci(client)
    assert result.outcome == ("success" if action == "status" else "blocked")
    assert result.target.unsaved is None
    assert not client.writes


@pytest.mark.parametrize("target", [
    ("lib", "cell", "calibre"), ("lib", "cell", "Calibre_PEX"),
    ("CALIBRE_LIB", "cell", "layout"), ("lib", "cell_calibre", "schematic"),
])
@pytest.mark.parametrize("dry_run", [True, False])
def test_calibre_checkin_blocked_before_remote_access(target, dry_run):
    client = _Client()
    result = client.sos.checkin_cellview(*target, message="Never submit", dry_run=dry_run)
    assert result.outcome == "blocked" and "Calibre" in result.diagnostics[0]
    assert not client.skill_calls and not client.commands


def test_calibre_status_remains_read_only():
    client = _Client()
    assert client.sos.status_cellview("lib", "cell", "calibre").outcome == "success"
    assert not client.writes


@pytest.mark.parametrize("directory,master", [
    ("/work/Calibre/lib/cell/layout", "/work/Calibre/lib/cell/layout/layout.oa"),
    (DIRECTORY, DIRECTORY + "/calibre.oa"),
])
def test_calibre_resolved_path_blocks_checkin_even_for_allowed_oa_type(directory, master):
    client = _Client(view_type="maskLayout")
    client.resolution = _skill_ok(
        f'("ok" {json.dumps(directory)} {json.dumps(master)} "maskLayout" nil)'
    )
    result = _ci(client)
    assert result.outcome == "blocked" and not client.writes and not client.commands


def test_checkout_missing_package_after_gdm_is_not_success():
    client = _Client(before=_state("-", "-"), after=_state("O", "!"))
    assert client.sos.checkout_cellview("lib", "cell", "schematic_Vt").outcome == "failed"


def test_non_oa_master_blocks_writes():
    client = _Client()
    client.resolution = _skill_ok(
        f'("ok" {json.dumps(DIRECTORY)} {json.dumps(DIRECTORY + "/sch.cdb")} "schematic" nil)'
    )
    assert _ci(client).outcome == "blocked"
    assert not client.writes


@pytest.mark.parametrize("action,state,expected", [
    ("co", _state(), "noop"),
    ("ci", _state("O", "-"), "noop"),
    ("ci", _state("-", "-"), "blocked"),
    ("co", _state("-", "M"), "blocked"),
    ("co", _state("-", "-", lock="L"), "blocked"),
    ("ci", _state(lock="L"), "blocked"),
    ("ci", _state(kind="P"), "blocked"),
    ("ci", _state("W"), "blocked"),
    ("ci", _state(change="!"), "blocked"),
])
def test_preconditions_and_noops_never_write(action, state, expected):
    client = _Client(before=state)
    result = (_ci(client) if action == "ci" else client.sos.checkout_cellview("lib", "cell", "schematic_Vt"))
    assert result.outcome == expected
    assert not client.writes


@pytest.mark.parametrize("dirty,view_type", [(True, "schematic"), (False, "maestro"), (False, "config")])
def test_unsaved_or_unsupported_view_blocks_writes(dirty, view_type):
    client = _Client(dirty=dirty, view_type=view_type)
    assert _ci(client).outcome == "blocked"
    assert not client.writes


@pytest.mark.parametrize("reply,outcome", [
    (_skill_ok('("failed" "GDM returned nil.")'), "failed"),
    (_skill_ok('("blocked" "Target changed.")'), "blocked"),
    (_skill_ok('("ok") trailing'), "unknown"),
    (_skill_ok('("ok" "extra")'), "unknown"),
    (_skill_ok('("CHECKIN_OK")'), "unknown"),
    (VirtuosoResult(status=ExecutionStatus.ERROR, errors=["Socket timeout"]), "unknown"),
    (ConnectionResetError("lost response"), "unknown"),
])
def test_failure_or_ambiguous_reply_is_never_retried(reply, outcome):
    client = _Client(mutation=reply)
    result = _ci(client)
    assert result.outcome == outcome
    assert len(client.writes) == 1
    assert result.after.revision == "4"


@pytest.mark.parametrize("after,outcome", [
    (_state("O", "M"), "failed"),
    (_state("-", "-", "3"), "failed"),
    (TimeoutError("SSH status unavailable"), "unknown"),
])
def test_gdm_success_alone_is_not_operation_success(after, outcome):
    client = _Client(after=after)
    assert _ci(client).outcome == outcome
    assert len(client.writes) == 1


@pytest.mark.parametrize("status", [
    _state(path="lib/other/schematic_Vt"),
    _state()._replace(stdout=_state().stdout * 2),
    _state()._replace(stdout="** Error: refresh failed\n" + _state().stdout),
    _state()._replace(stdout="pOM--- 3 ./" + PATH),
    CommandResult(1, "error", "SOS unreachable"),
])
def test_untrusted_status_does_not_trigger_write(status):
    client = _Client(before=status)
    assert _ci(client).outcome == "failed"
    assert not client.writes


@pytest.mark.parametrize("action", ["status", "ci"])
def test_unmanaged_object_reports_unavailable_without_a_write(action):
    client = _Client(before=CommandResult(
        0, "** The flags and attributes have been updated.\n"
        "!! Warning: No objects selected to display status.\n", "",
    ))
    result = (_ci(client) if action == "ci"
              else client.sos.status_cellview("lib", "cell", "schematic_Vt"))
    assert result.outcome == "blocked" and "unmanaged" in result.diagnostics[0]
    assert not client.writes


def test_workarea_mismatch_does_not_trigger_write():
    client = _Client()
    client.root = CommandResult(0, "/other/workarea\n", "")
    assert _ci(client).outcome == "failed"
    assert not client.writes


@pytest.mark.parametrize("output", [
    '("ok" "/work area/lib/cell/schematic_Vt" "/other/sch.oa" "schematic" nil)',
    '("ok" "relative" "relative/sch.oa" "schematic" nil)',
    '("ok" "/work/../other" "/work/../other/sch.oa" "schematic" nil)',
    '("ok" "/work" "/work/sch.oa" "schematic" "nil")',
    '("ok")',
    '("blocked")',
    '(("ok"))',
])
def test_invalid_resolution_fails_before_ssh_or_mutation(output):
    client = _Client()
    client.resolution = _skill_ok(output)
    assert _ci(client).outcome == "failed"
    assert not client.commands and not client.writes


@pytest.mark.parametrize("message", ["", "   ", "hello\nworld", "bad\x00"])
def test_invalid_message_rejected_without_remote_access(message):
    client = _Client()
    with pytest.raises(ValueError):
        client.sos.checkin_cellview("lib", "cell", "schematic", message=message)
    assert not client.skill_calls


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeout_rejected_without_remote_access(timeout):
    client = _Client()
    with pytest.raises(ValueError):
        _ci(client, timeout=timeout)
    assert not client.skill_calls


def test_skill_escapes_identifiers_message_and_rechecks_paths_and_dirty_flag():
    target = SOSCellViewTarget('lib"name', 'cell\\name', "schematic_Vt", DIRECTORY, MASTER, "schematic")
    code = _skill(target, "ci", 'Fix "gain"\\test')
    assert 'ddGetObj("lib\\"name")' in code
    assert '"cell\\\\name"' in code
    assert 'ddCheckin(vbFile "Fix \\"gain\\"\\\\test")' in code
    assert code.index("Target path or view type changed") < code.index("ddCheckin(")
    assert code.index("when(vbDirty") < code.index("ddCheckin(")
    assert "dbIsCellViewModified" in code
    assert "dbSave" not in code and "schCheck" not in code
