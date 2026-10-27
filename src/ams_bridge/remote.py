"""Inline Linux control commands, sent over SSH; never uploaded as scripts."""
import shlex



def work_available(control):
    work = control.rsplit("/", 1)[0] + "/work"
    return f"test -d {shlex.quote(work)} && test ! -L {shlex.quote(work)} || exit 89\n"


IDENTITY = r'''
boot_id=$(cat /proc/sys/kernel/random/boot_id) || exit 75
identity() {
    local p=$1 row
    test -r "/proc/$p/stat" || return 1
    IFS= read -r row < "/proc/$p/stat" || return 1
    row=${row##*) }
    local -a fields
    read -ra fields <<< "$row"
    test "${fields[0]}" != Z || return 1
    printf '%s %s %s\n' "$p" "${fields[19]}" "$boot_id"
}
same() { test "$(identity "$1")" = "$1 $2 $3"; }
'''


def supervisor(control, cwd, solver, wall):
    marker = control.rsplit("/", 2)[-2]
    timeout_command = f"timeout --signal=USR1 --kill-after=2s {wall}s " if wall is not None else ""
    worker = (f"exec >{shlex.quote(control + '/launch.log')} 2>&1\n"
              f"trap {shlex.quote('touch ' + shlex.quote(control + '/timeout.trigger'))} USR1\n"
              f"cd {shlex.quote(cwd)} || exit 72\n{solver} &\nchild=$!\nwait \"$child\"\nrc=$?\n"
              f"printf '%s\\n' \"$rc\" > {shlex.quote(control + '/worker_exit.tmp')}\n"
              f"mv {shlex.quote(control + '/worker_exit.tmp')} {shlex.quote(control + '/worker_exit')}\nexit \"$rc\"\n")
    return IDENTITY + f"\ncd {shlex.quote(control)} || exit 72\nmarker={shlex.quote(marker)}\n" + r'''
umask 077
identity $$ > identity.tmp
mv identity.tmp identity
date -u +%Y-%m-%dT%H:%M:%SZ > started_at
declare -A known
escaped=0
capture() {
    local p parent group id row start
    local -a fields
    while read -r p parent group; do
        if test "$group" = "$worker" || test -n "${known[$parent]-}"; then
            id=$(identity "$p") || continue
            IFS= read -r row < "/proc/$p/stat" || continue
            row=${row##*) }; read -ra fields <<< "$row"
            read -r p start ignored <<< "$id"
            test "${fields[19]}" = "$start" || continue
            parent=${fields[1]}; group=${fields[2]}
            if ! { test "$group" = "$worker" && test -n "$worker_id" && test "$(identity "$worker")" = "$worker_id"; } &&
               ! { test -n "${known[$parent]-}" && test "$(identity "$parent")" = "${known[$parent]}"; }; then continue; fi
            known[$p]=$id
            test "$group" = "$worker" || escaped=1
        fi
    done < <(ps -eo pid,ppid,pgid --no-headers)
}
capture_marker() {
    local entry file p id
    for file in /proc/[0-9]*/environ; do
        p=${file#/proc/}; p=${p%/environ}
        id=$(identity "$p") || continue
        if test -n "${known[$p]-}" && test "$id" = "${known[$p]}"; then continue; fi
        test -r "$file" || continue
        while IFS= read -r -d '' entry; do
            if test "$entry" = "AMS_BRIDGE_JOB_ID=$marker"; then
                test "$(identity "$p")" = "$id" && known[$p]=$id
                break
            fi
        done < "$file" 2>/dev/null
    done
}
signal_known() {
    local sig=$1 id p start boot
    for id in "${known[@]}"; do
        read -r p start boot <<< "$id"
        if same "$p" "$start" "$boot"; then kill -"$sig" "$p" 2>/dev/null || :; fi
    done
}
''' + f"\nLC_ALL=C env AMS_BRIDGE_JOB_ID=\"$marker\" setsid {timeout_command}bash -c {shlex.quote(worker)} 2>watchdog.log &\n" + r'''
worker=$!
identity "$worker" > worker.identity.tmp || :
mv worker.identity.tmp worker.identity
worker_id=$(cat worker.identity)
cancelled=0
elapsed=0
while kill -0 "$worker" 2>/dev/null; do
    capture
    if test -f cancel.request; then
        if test "$cancelled" = 0; then
            cancelled=1
            signal_known TERM
        fi
        elapsed=$((elapsed + 1))
        if test "$elapsed" -ge 20; then signal_known KILL; fi
    fi
    sleep 0.1
done
wait "$worker"
rc=$?
capture_marker
# Successful launchers can finish just before their normal helper processes.
if test "$rc" = 0; then
    for attempt in {1..20}; do
        alive=0
        for id in "${known[@]}"; do
            read -r p start boot <<< "$id"
            same "$p" "$start" "$boot" && alive=1
        done
        test "$alive" = 0 && break
        sleep 0.1
    done
fi
# A killed group leader may leave children alive; only signal identities we observed.
leftovers=0
for id in "${known[@]}"; do
    read -r p start boot <<< "$id"
    if same "$p" "$start" "$boot"; then
        leftovers=1
        printf '%s %s\n' "$id" "$(cat /proc/$p/comm 2>/dev/null)" >> lingering_processes
    fi
done
signal_known TERM
for attempt in {1..20}; do
    alive=0
    for id in "${known[@]}"; do
        read -r p start boot <<< "$id"
        same "$p" "$start" "$boot" && alive=1
    done
    test "$alive" = 0 && break
    sleep 0.1
done
signal_known KILL
sleep 0.1
capture_marker
signal_known KILL
sleep 0.1
unclean=0
for id in "${known[@]}"; do
    read -r p start boot <<< "$id"
    same "$p" "$start" "$boot" && unclean=1
done
state=failed
reason=tool_exit
test "$rc" = 0 && state=completed
if test "$rc" = 0 && test "$leftovers" = 1; then reason=tool_exit_with_child_cleanup; fi
exec 9>decision.lock
flock -x 9
if test -f cancel.request; then cancelled=1; fi
if test "$cancelled" = 1; then state=cancelled; reason=cancel_request; fi
if test -f timeout.trigger || { test "$rc" = 124 && test ! -f worker_exit; }; then state=timed_out; reason=wall_timeout; fi
if test "$unclean" = 1; then state=failed; reason=cleanup_incomplete; fi
printf '%s\n' "${known[@]}" > observed_processes
printf '%s\n' "$escaped" > changed_process_groups
if mkdir terminal.claim 2>/dev/null; then
    printf '%s\t%s\t%s\n' "$state" "$rc" "$reason" > result.tmp
    mv result.tmp result
    date -u +%Y-%m-%dT%H:%M:%SZ > finished_at
fi
'''


def launch(control, code):
    return f"""set -eu
cd {shlex.quote(control)}
mkdir launch.claim
nohup setsid bash -c {shlex.quote(code)} </dev/null >supervisor.log 2>&1 &
for attempt in {{1..30}}; do
    if test -s identity; then cat identity; exit 0; fi
    sleep 0.1
done
echo 'supervisor identity not confirmed' >&2
exit 73
"""


def query(control):
    return IDENTITY + f"\ncd {shlex.quote(control)} || exit 74\n" + r'''
if test -s result; then
    printf 'terminal\t'; cat result
elif test -s identity; then
    read -r p start boot < identity
    if same "$p" "$start" "$boot"; then printf 'running\n';
    else printf 'unknown\tsupervisor_missing_or_identity_changed\n'; fi
else
    printf 'unknown\tlaunch_not_confirmed\n'
fi
'''


def processes(control):
    """Only the verified worker session, bounded to 64 process names."""
    return IDENTITY + f"\ncd {shlex.quote(control)} || exit 74\n" + r'''
test ! -s result || exit 0
test ! -L worker.identity && test -s worker.identity || exit 0
read -r p start boot < worker.identity
same "$p" "$start" "$boot" || exit 0
ps --sid "$p" -o pid=,comm= | head -n 64
'''


def request_cancel(control):
    return IDENTITY + f"\ncd {shlex.quote(control)} || exit 74\n" + r'''
exec 9>decision.lock
if ! flock -n 9; then printf 'busy\n'; exit 0; fi
if test -s result; then printf 'already_terminal\n'; exit 0; fi
if test -s identity; then
    read -r p start boot < identity
    if same "$p" "$start" "$boot"; then
        : > cancel.request.tmp
        mv cancel.request.tmp cancel.request
        printf 'requested\n'
        exit 0
    fi
fi
printf 'unknown\n'
'''
