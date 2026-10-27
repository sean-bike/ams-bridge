"""Dependency syntax for resolve; simulator settings remain native."""
import re
import shlex

from .common import BridgeError

FLAGS = {"-ams", "-ams_flex", "-64bit", "-sv", "-quiet", "-messages", "-linedebug", "-licqueue", "-status",
         "-UNBUFFERED", "-v93", "-ade", "-novitalaccl", "-noparamerr", "-rnm_partinfo", "-run", "-exit", "-allowredefinition",
         "-notimingchecks", "-endlib", "-v200x", "-vhdl", "-compile", "-elaborate"}
VALUES = {"-top", "-timescale", "-define", "-access", "-connectrules", "-amsconnrules", "-errormax", "-nowarn",
          "-amsformat", "-vtimescale", "-delay_mode", "-simcompatible_ams", "-name", "-defparam",
          "-svseed", "-seed", "-work", "-libext"}
FILES = {"-v", "-input", "-amscontrolfile", "-cdslib", "-hdlvar", "-sdf_cmd_file"}
OUTPUTS = {"-l", "-log", "-xmlibdirname", "-amspartinfo", "-makelib"}
NESTED = {"-spectre_args", "-xmsimargs", "-xmvlogargs", "-xmelabargs"}


def argument_tokens(text):
    # A Verilog apostrophe is data, whereas single-quoted paths retain V1 semantics.
    pattern = r'"(?:\\.|[^"\\])*"|(?P<number>\d+\'[sS]?[bBoOdDhH][0-9a-fA-F_xXzZ?]+)'
    protected = re.sub(pattern, lambda m: m[0].replace("'", "\\'") if m.group("number") else m[0], text)
    return shlex.split(protected.replace("\\\n", " "), comments=True, posix=True)


def compilefile(value, origin):
    fields = {}
    for token in value.split():
        key, colon, entry = token.partition(":")
        if not colon or not entry or key in fields or key not in ("file", "ftype", "lib", "cell", "view"):
            raise BridgeError(f"{origin}: unsupported -amscompilefile fields; use dependency_mode=explicit")
        fields[key] = entry
    if "file" not in fields or fields.get("ftype", "va") != "va":
        raise BridgeError(f"{origin}: -amscompilefile requires file and supported va type")
    for key in ("lib", "cell", "view"):
        if key in fields and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", fields[key]):
            raise BridgeError(f"{origin}: invalid logical {key}")
    return fields


def nested_arguments(option, value, origin, output):
    try:
        tokens = argument_tokens(value)
    except ValueError as exc:
        raise BridgeError(f"{origin}: malformed {option}: {exc}") from exc
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"+amsrawdir", "-ahdllibdir", "-log", "-raw"} and index + 1 < len(tokens):
            output(tokens[index + 1], origin)
            index += 2
        else:
            index += 1


def binding(text, origin):
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        pairs = re.fullmatch(r'\s*config((?:\s+[a-z]+="[A-Za-z0-9_.$:-]+")+)\s*', line)
        fields = re.findall(r'([a-z]+)="([^"]+)"', pairs[1]) if pairs else []
        keys = [k for k, _ in fields]
        if (not fields or len(keys) != len(set(keys)) or set(keys) - {"designtop", "cell", "lib", "view", "stopview"} or
                ("designtop" in keys and keys != ["designtop"]) or
                ("designtop" not in keys and "cell" not in keys)):
            raise BridgeError(f"{origin}:{number}: unsupported UNL binding declaration; use dependency_mode=explicit")


def tcl(text, origin, output):
    databases = set()
    for number, line in enumerate(text.splitlines(), 1):
        value = line.strip()
        where = f"{origin}:{number}"
        if not value or value.startswith("#"):
            continue
        if re.fullmatch(r"(?:run(?:\s+\d+(?:\.\d+)?\s*[a-z]+)?|exit)\s*;?", value):
            continue
        database = re.fullmatch(r'database\s+-open\s+([A-Za-z_]\w*)\s+-into\s+(?:"([^"$\[\]{};\\]+)"|([^\s"$\[\]{};\\]+))\s+-default', value)
        if database:
            output(database[2] or database[3], where)
            databases.add(database[1])
            continue
        probe = re.fullmatch(r'probe\s+-create\s+(?:-emptyok\s+)?-database\s+([A-Za-z_]\w*)\s+\{([A-Za-z_][A-Za-z0-9_.\[\]:-]*)\}', value)
        if probe and probe[1] in databases:
            continue
        raise BridgeError(f"{where}: unsupported Tcl dependency syntax; use dependency_mode=explicit with all inputs declared")
