"""OpenSSH subprocess transport. No passwords, agent or server installation."""
from __future__ import annotations

import math
import os
import queue
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from contextvars import ContextVar
from contextlib import contextmanager

from .common import BridgeError
from .remote import IDENTITY

_ACTIVE_LEASE = ContextVar("ams_bridge_operation_lease", default=None)


_LEASE_READERS = r'''
readers() {
 local signal=$1 found=0 file p id entry start boot
 for file in /proc/[0-9]*/environ; do
  p=${file#/proc/}; p=${p%/environ}
  test -r "$file" || continue
  id=$(identity "$p") || continue
  while IFS= read -r -d '' entry; do
   if test "$entry" = "AMS_BRIDGE_OPERATION_ID=$token"; then
    read -r p start boot <<< "$id"
    if same "$p" "$start" "$boot"; then
     found=1
     if test "$signal" != check; then kill -"$signal" "$p" 2>/dev/null || :; fi
    fi
    break
   fi
  done < "$file" 2>/dev/null || :
 done
 return "$found"
}
drain_readers() {
 local pass signal
 for pass in {1..15}; do
  signal=TERM; test "$pass" -lt 5 || signal=KILL
  if readers "$signal"; then return 0; fi
  sleep .1
 done
 readers check
}
'''


def lease_timeout_s(execution):
    return execution.get("transfer_timeout_s", 300) + 2 * (execution["control_timeout_s"] + execution["connect_timeout_s"]) + 5


class SSH:
    def __init__(self, execution):
        self.execution = execution

    def _command(self, budget):
        bootstrap = ('IFS= read -r n; case "$n" in ""|*[!0-9]*) exit 64;; esac; '
                     'code=$(head -c "$n") || exit 65; exec bash -c "$code"')
        return ["ssh", "-T", "-o", "BatchMode=yes", "-o",
                   f"ConnectTimeout={math.ceil(self.execution['connect_timeout_s'])}",
                   *(["-F", self.execution["ssh_config_file"]] if self.execution.get("ssh_config_file") else []),
                   self.execution["ssh_host"], shlex.join(["timeout", "--kill-after=2s",
                   f"{budget}s", "bash", "-c", bootstrap])]

    def run(self, script, *, input_file=None, output_file=None, check=True, timeout=None, transfer=False):
        # head consumes the declared UTF-8 bytes; remaining stdin is the archive.
        lease = _ACTIVE_LEASE.get()
        if lease:
            lease["ensure"]()
            if (self.execution["ssh_host"], self.execution.get("ssh_config_file")) != lease["host"]:
                raise BridgeError("operation lease connection mismatch", "identity_mismatch")
            guard = IDENTITY + f"\ncontrol={shlex.quote(lease['control'])}\n" + r'''
test ! -L "$control/operation-gate.lock" || exit 87
exec 8>"$control/operation-gate.lock"
flock -s 8
read -r token state p start boot < "$control/operation.owner" || exit 87
''' + f"test \"$token\" = {shlex.quote(lease['token'])} && test \"$state\" = active && same \"$p\" \"$start\" \"$boot\" || exit 87\nflock -u 8\nexec 8>&-\n"
            script = shlex.join(["env", "AMS_BRIDGE_OPERATION_ID=" + lease["token"], "bash", "-c", guard + script])
        budget = self.execution.get("transfer_timeout_s", 300) if transfer else self.execution["control_timeout_s"]
        if transfer and timeout is not None:
            budget = min(budget, timeout)
        command = self._command(budget)
        try:
            with tempfile.TemporaryFile() as wire:
                data = script.encode("utf-8")
                wire.write(str(len(data)).encode("ascii") + b"\n" + data)
                if input_file is not None:
                    shutil.copyfileobj(input_file, wire)
                wire.seek(0)
                result = subprocess.run(command, stdin=wire, stdout=output_file or subprocess.PIPE,
                                        stderr=subprocess.PIPE, timeout=timeout or budget,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
        except subprocess.TimeoutExpired as exc:
            operation = 'transfer' if transfer else 'control'
            raise BridgeError(f"SSH {operation} exceeded its operation timeout", operation + "_timeout") from exc
        except OSError as exc:
            raise BridgeError(f"SSH unavailable: {exc}", "communication") from exc
        if result.returncode == 255:
            raise BridgeError(result.stderr.decode("utf-8", "replace").strip(), "communication")
        if check and result.returncode:
            raise BridgeError(result.stderr.decode("utf-8", "replace").strip() or
                              f"remote command failed ({result.returncode})", "remote")
        return result

    @contextmanager
    def lease(self, control):
        budget = self.execution.get("transfer_timeout_s", 300)
        token = uuid.uuid4().hex
        code = (IDENTITY + _LEASE_READERS + f"\nset -eu\ncd -- {shlex.quote(control)}\ntoken={shlex.quote(token)}\n"
                "test \"$(realpath -e .)\" = \"$PWD\"\n"
                "for name in operation.lock operation-gate.lock operation.owner; do\n"
                " test ! -L \"$name\" || exit 87\n"
                " if test -e \"$name\"; then test -f \"$name\" && test \"$(stat -c %h -- \"$name\")\" = 1 || exit 87; fi\ndone\nexec 9>operation.lock\n"
                "if ! flock -n 9; then printf 'BUSY\\n'; exit 0; fi\n"
                "if test -s operation.owner; then\n"
                " read -r previous state p start boot extra < operation.owner || exit 87\n"
                " [[ $previous =~ ^[0-9a-f]{32}$ && $state =~ ^(active|closed)$ && $p =~ ^[1-9][0-9]*$ && $start =~ ^[0-9]+$ && $boot =~ ^[0-9a-f-]{36}$ && -z $extra ]] || exit 87\n"
                " if same \"$p\" \"$start\" \"$boot\"; then printf 'BUSY\\n'; exit 0; fi\n"
                " current=$token; token=$previous\n"
                " if ! drain_readers; then printf 'BUSY\\n'; exit 0; fi\n"
                " token=$current\nfi\n"
                "exec 8>operation-gate.lock\nflock -x 8\nowner=$(identity $$)\n"
                "printf '%s active %s\\n' \"$token\" \"$owner\" > operation.owner\nflock -u 8\n"
                "printf 'LOCKED\\n'\nrelease=\nIFS= read -r release || :\nflock -x 8\n"
                "printf '%s closed %s\\n' \"$token\" \"$owner\" > operation.owner\nflock -u 8\n")
        # Timeouts can outlive their SSH client even during an orderly release.
        # Retain flock until all readers have stopped; an abandoned owner is
        # drained before its token can be replaced by a later claimant.
        code += "if ! drain_readers; then while ! readers check; do sleep .2; done; fi\n"
        data = code.encode("utf-8")
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(self._command(lease_timeout_s(self.execution)), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
            deadline = time.monotonic() + budget
            answer = queue.Queue()
            reader = threading.Thread(target=lambda: answer.put(process.stdout.readline(32)), daemon=True)
            try:
                process.stdin.write(str(len(data)).encode() + b"\n" + data)
                process.stdin.flush()
                reader.start()
                try:
                    response = answer.get(timeout=self.execution["control_timeout_s"])
                except queue.Empty as exc:
                    raise BridgeError("remote operation lease was not confirmed", "communication") from exc
                if response == b"BUSY\n":
                    raise BridgeError("remote job has a conflicting operation", "busy")
                if response != b"LOCKED\n":
                    errors.seek(0)
                    raise BridgeError("remote operation lease failed: " + errors.read(65536).decode("utf-8", "replace"), "communication")
                def ensure():
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise BridgeError("remote operation lease or transfer budget expired", "communication")
                active = _ACTIVE_LEASE.set({"ensure": ensure, "host": (self.execution["ssh_host"], self.execution.get("ssh_config_file")),
                                           "token": token, "control": control})
                try:
                    yield ensure
                    ensure()
                finally:
                    _ACTIVE_LEASE.reset(active)
            finally:
                try:
                    process.stdin.write(b"release\n")
                    process.stdin.flush()
                except (OSError, ValueError):
                    pass
                try:
                    process.stdin.close()
                except OSError:
                    pass
                try:
                    process.wait(timeout=self.execution["control_timeout_s"])
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise BridgeError("remote operation reader cleanup was not confirmed", "communication")
                process.stdout.close()


def environment_command(execution, arguments):
    command = shlex.join(arguments)
    setup = execution.get("environment_setup")
    if setup:
        if setup["shell"] in ("/bin/csh", "/bin/tcsh"):
            code = f"source {shlex.quote(setup['path'])}\nif ($status != 0) exit 70\nexec {command}"
            command = shlex.join([setup["shell"], "-f", "-c", code])
        else:
            code = f". {shlex.quote(setup['path'])} || exit 70\nexec {command}"
            command = shlex.join([setup["shell"], "-c", code])
    assignments = [f"{key}={value}" for key, value in execution["environment"].items()]
    return shlex.join(["env", *assignments]) + " " + command
