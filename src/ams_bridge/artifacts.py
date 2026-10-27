"""Bounded directory manifests and extraction without following archive links."""
import hashlib
import gzip
import re
import shlex
import shutil
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .common import BridgeError, canonical, digest, is_link, local_file, relative


def _member(name, aliases):
    try:
        normalized = relative(name, "artifact member")
        if normalized != name:
            raise BridgeError("noncanonical artifact member")
        prefix = ""
        for part in name.split("/"):
            prefix = part if not prefix else prefix + "/" + part
            previous = aliases.setdefault(prefix.casefold(), prefix)
            if previous != prefix:
                raise BridgeError(f"case alias: {previous} and {prefix}")
        return normalized
    except BridgeError as exc:
        raise BridgeError(f"unsafe artifact member {name!r}: {exc}", "unsafe_path") from exc


def _budget(members, limits):
    if (len(members) > limits["members"] or any(row["size"] > limits["file_bytes"] for row in members) or
            sum(row["size"] for row in members) > limits["total_bytes"]):
        raise BridgeError("directory artifact budget exceeded", "budget_exceeded")


def local_manifest(root, limits):
    root = Path(root)
    members, aliases = [], {}

    def visit(path, name):
        _member(name, aliases)
        if is_link(path):
            raise BridgeError(f"artifact link refused: {name}", "unsafe_path")
        stat = path.stat()
        if path.is_dir():
            members.append({"path": name, "type": "directory", "size": 0, "sha256": None})
            _budget(members, limits)
            for child in sorted(path.iterdir()):
                visit(child, child.name if name == "." else name + "/" + child.name)
        elif path.is_file() and stat.st_nlink == 1:
            members.append({"path": name, "type": "file", "size": stat.st_size, "sha256": None})
            _budget(members, limits)
            members[-1]["sha256"] = digest(path)
        else:
            raise BridgeError(f"artifact is linked or not regular: {name}", "unsafe_path")
    visit(root, ".")
    return sorted(members, key=lambda row: row["path"])


class _ArchiveInput:
    def __init__(self, stream, limit):
        self.stream, self.limit = stream, limit

    def read(self, count=-1):
        if count < 0 or count > 1024 * 1024:
            raise BridgeError("archive metadata read exceeds 1 MiB", "budget_exceeded")
        return self.stream.read(count)

    def tell(self):
        return self.stream.tell()

    def seek(self, offset, whence=0):
        position = self.stream.seek(offset, whence)
        if position < 0 or position > self.limit:
            raise BridgeError("archive position exceeds expanded budget", "budget_exceeded")
        return position


@contextmanager
def _archive_input(path, limits, ensure):
    limit = limits["total_bytes"] + limits["members"] * 8192 + 1048576
    with Path(path).open("rb") as source:
        signature = source.read(6)
        source.seek(0)
        if signature.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=source) as compressed, tempfile.TemporaryFile() as expanded:
                size = 0
                while chunk := compressed.read(1024 * 1024):
                    ensure()
                    size += len(chunk)
                    if size > limit:
                        raise BridgeError("compressed archive expanded beyond budget", "budget_exceeded")
                    expanded.write(chunk)
                expanded.seek(0)
                yield _ArchiveInput(expanded, limit)
        else:
            if source.seek(0, 2) > limit:
                raise BridgeError("archive size exceeds expanded budget", "budget_exceeded")
            source.seek(0)
            yield _ArchiveInput(source, limit)


def unpack_directory(archive, staging, name, limits, *, ensure=lambda: None):
    staging = Path(staging).absolute()
    parent = staging.parent.resolve()
    local_file(parent, staging.name)
    if staging.exists():
        raise BridgeError("archive staging directory must be new", "unsafe_path")
    staging.mkdir()
    names, aliases, headers = set(), {}, []
    try:
        with _archive_input(archive, limits, ensure) as stream, tarfile.open(fileobj=stream, mode="r:") as tar:
            for item in tar:
                ensure()
                member = _member(item.name.rstrip("/"), aliases)
                if member.casefold() in names or not (member == name or member.startswith(name + "/")):
                    raise BridgeError(f"duplicate or out-of-scope archive member: {member}", "unsafe_path")
                names.add(member.casefold())
                if (not item.isdir() and not item.isreg()) or item.issparse() or item.size < 0 or (member == name and not item.isdir()):
                    raise BridgeError(f"archive link or special member: {member}", "unsafe_path")
                headers.append({"size": item.size})
                _budget(headers, limits)
                destination = local_file(staging, member)
                if item.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    source = tar.extractfile(item)
                    if source is None:
                        raise BridgeError(f"archive member has no data: {member}", "parse_error")
                    copied = 0
                    with source, destination.open("xb") as output:
                        while chunk := source.read(min(1024 * 1024, item.size - copied + 1)):
                            ensure()
                            copied += len(chunk)
                            if copied > item.size:
                                raise BridgeError("archive expanded beyond declared size", "budget_exceeded")
                            output.write(chunk)
                    if copied != item.size:
                        raise BridgeError("truncated archive member", "parse_error")
        return local_manifest(local_file(staging, name), limits)
    except (OSError, ValueError, tarfile.TarError, BridgeError) as exc:
        if staging.resolve().parent == parent and not is_link(staging):
            shutil.rmtree(staging)
        if isinstance(exc, BridgeError):
            raise
        raise BridgeError(f"cannot unpack artifact archive: {exc}", "parse_error") from exc


def manifest_hash(members):
    return hashlib.sha256(canonical(members)).hexdigest()


def safe_path_script(handle, name, kind="file"):
    root = handle["remote_directory"] + "/work"
    path = root + "/" + relative(name)
    return f'''set -eu
p={shlex.quote(path)}
cursor="$p"
while :; do
 test ! -L "$cursor" || {{ echo 'artifact link refused' >&2; exit 45; }}
 test "$cursor" != / || break
 cursor=${{cursor%/*}}; test -n "$cursor" || cursor=/
done
test -{('d' if kind == 'directory' else 'e' if kind == 'any' else 'f')} "$p" || exit 44
test "$(realpath -e -- "$p")" = "$p" || exit 45
'''


def metadata_script(handle, declarations, *, root=None):
    script = r'''set -u
printf 'AMS_ARTIFACTS_2\0'
emit() { printf '%s\0' "$artifact" "$1" "$2" "$3" "$4" "$5"; }
scan_member() {
 local file=$1 rel=$2 bytes hash
 if test -L "$file"; then emit unsafe_path "$rel" unknown 0 ''; return 65; fi
 if test -d "$file"; then emit ok "$rel" directory 0 ''; return; fi
 if test ! -f "$file" || test "$(stat -c %h -- "$file")" != 1; then emit unsafe_path "$rel" unknown 0 ''; return 65; fi
 bytes=$(wc -c < "$file") || return 66
 total=$((total+bytes))
 if test "$file_limit" != 0 && { test "$bytes" -gt "$file_limit" || test "$total" -gt "$total_limit"; }; then
  emit budget_exceeded "$rel" file "$bytes" ''; return 64
 fi
 hash=$(sha256sum < "$file") || return 66; hash=${hash%% *}
 emit ok "$rel" file "$bytes" "$hash"
}
scan_artifact() {
 local cursor="$p" count=1 total=0 rc child rel
 while :; do
  if test -L "$cursor"; then emit unsafe_path . unknown 0 ''; return; fi
  test "$cursor" != / || break
  cursor=${cursor%/*}; test -n "$cursor" || cursor=/
 done
 if test ! -e "$p"; then emit missing . "$kind" 0 ''; return; fi
 if test "$kind" = directory && test ! -d "$p"; then emit collection . unknown 0 ''; return; fi
 if test "$kind" = file && test ! -f "$p"; then emit collection . unknown 0 ''; return; fi
 scan_member "$p" .; rc=$?
 if test "$rc" != 0; then
  if test "$rc" != 64 && test "$rc" != 65; then emit collection . unknown 0 ''; fi
  return
 fi
 if test "$kind" = directory; then
  set -o pipefail
  find "$p" -mindepth 1 -print0 | while IFS= read -r -d '' child; do
   count=$((count+1)); rel=${child#"$p"/}
   if test "$count" -gt "$member_limit"; then emit budget_exceeded "$rel" unknown 0 ''; exit 64; fi
   scan_member "$child" "$rel" || exit $?
  done
  rc=$?
  if test "$rc" != 0 && test "$rc" != 64 && test "$rc" != 65; then emit collection . unknown 0 ''; fi
 fi
}
'''
    for item in declarations:
        limits = item.get("limits", {})
        assignments = {"artifact": item["path"], "p": (root or handle["remote_directory"] + "/work") + "/" + item["path"],
                       "kind": item.get("type", "file"), "file_limit": limits.get("file_bytes", 0),
                       "total_limit": limits.get("total_bytes", 0), "member_limit": limits.get("members", 0)}
        script += "\n" + "\n".join(key + "=" + shlex.quote(str(value)) for key, value in assignments.items()) + "\nscan_artifact\n"
    return script


def parse_metadata(data, declarations):
    values = data.split(b"\0")
    if values[0] != b"AMS_ARTIFACTS_2" or values[-1] != b"" or (len(values) - 2) % 6:
        raise BridgeError("invalid artifact manifest framing", "invalid_remote_record")
    result = {item["path"]: {"status": "missing", "members": [], "errors": []} for item in declarations}
    specs = {item["path"]: item for item in declarations}
    aliases, seen, observed = {}, {}, set()
    for offset in range(1, len(values) - 1, 6):
        try:
            name, state, member, kind, size, sha = [value.decode("utf-8") for value in values[offset:offset + 6]]
            if name not in result:
                raise BridgeError("unrequested artifact in remote manifest", "invalid_remote_record")
            record = result[name]
            observed.add(name)
            if state == "missing":
                if member != "." or kind != specs[name].get("type", "file") or size != "0" or sha or record["members"]:
                    raise BridgeError("invalid missing-artifact record", "invalid_remote_record")
                continue
            if state != "ok":
                record["errors"].append({"code": state if state in ("unsafe_path", "budget_exceeded", "collection") else "invalid_remote_record", "member": member})
                record["status"] = "error"
                continue
            _member(member, aliases.setdefault(name, {}))
            if member.casefold() in seen.setdefault(name, set()):
                raise BridgeError(f"duplicate remote member: {member}", "unsafe_path")
            seen[name].add(member.casefold())
            if kind not in ("file", "directory") or not re.fullmatch(r"\d+", size):
                raise BridgeError("invalid remote artifact type/size", "invalid_remote_record")
            row = {"path": member, "type": kind, "size": int(size), "sha256": sha if kind == "file" else None}
            if (kind == "directory" and (row["size"] or sha)) or (kind == "file" and not re.fullmatch(r"[0-9a-f]{64}", sha)):
                raise BridgeError("invalid remote artifact hash", "invalid_remote_record")
            record["members"].append(row)
            if "limits" in specs[name]:
                _budget(record["members"], specs[name]["limits"])
            if record["status"] != "error":
                record["status"] = "present"
        except (ValueError, UnicodeError, BridgeError) as exc:
            if isinstance(exc, BridgeError) and 'name' in locals() and name in result:
                result[name]["status"] = "error"
                result[name]["errors"].append({"code": exc.code, "message": str(exc)})
            else:
                raise BridgeError(f"invalid artifact metadata: {exc}", "invalid_remote_record") from exc
    for name, record in result.items():
        if name not in observed:
            raise BridgeError(f"remote manifest omitted artifact: {name}", "invalid_remote_record")
        record["members"].sort(key=lambda row: row["path"])
        if record["status"] == "present" and (not record["members"] or record["members"][0]["path"] != "." or
                                              record["members"][0]["type"] != specs[name].get("type", "file")):
            record.update(status="error", errors=[{"code": "invalid_remote_record", "message": "missing or incorrect artifact root"}])
    return result
