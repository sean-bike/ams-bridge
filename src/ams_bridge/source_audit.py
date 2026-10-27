"""Lexical auditing of the supported literal native file operations."""
import re
from dataclasses import dataclass

from .common import BridgeError


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    line: int
    start: int


def tokens(text):
    pattern = r'(?P<space>(?:\s|\\\r?\n)+)|(?P<string>"(?:\\.|[^"\\])*")|(?P<word>[$A-Za-z_][$A-Za-z_0-9]*)|(?P<other>.)'
    line = 1
    result = []
    for match in re.finditer(pattern, text, re.S):
        if match.lastgroup != "space":
            result.append(Token(match.lastgroup, match[0], line, match.start()))
        line += match[0].count("\n")
    return result


def literal(argument, origin):
    if len(argument) != 1 or argument[0].kind != "string":
        raise BridgeError(f"{origin}: dynamic file argument is unsupported; use dependency_mode=explicit with all inputs declared")
    return argument[0].text[1:-1]


def file_calls(text, origin, chain=(), *, locations=False):
    items = tokens(text)
    functions = {"$readmemh", "$readmemb", "$fopen", "$dumpfile", "$shm_open"}
    for index, token in enumerate(items):
        if token.kind != "word" or token.text not in functions:
            continue
        where = f"{origin}:{token.line}"
        if chain:
            where += f" (reference chain: {' -> '.join(chain)})"
        if index + 1 == len(items) or items[index + 1].text != "(":
            raise BridgeError(f"{where}: unsupported {token.text} call")
        arguments, current, stack = [], [], [")"]
        for part in items[index + 2:]:
            if part.kind == "other" and part.text in ("(", "[", "{"):
                stack.append({"(": ")", "[": "]", "{": "}"}[part.text])
            elif part.kind == "other" and part.text in (")", "]", "}"):
                if not stack or part.text != stack.pop():
                    raise BridgeError(f"{where}: unbalanced {token.text} arguments")
                if not stack:
                    arguments.append(current)
                    break
            if part.text == "," and len(stack) == 1:
                arguments.append(current)
                current = []
            else:
                current.append(part)
        else:
            raise BridgeError(f"{where}: unterminated {token.text} arguments")
        path = literal(arguments[0], where)
        writing = token.text in ("$dumpfile", "$shm_open", "$fopen")
        if token.text == "$fopen":
            if len(arguments) not in (1, 2):
                raise BridgeError(f"{where}: unsupported $fopen argument count")
            mode = literal(arguments[1], where) if len(arguments) == 2 else "w"
            if mode not in {"r", "w", "a", "r+", "w+", "a+", "rb", "wb", "ab", "r+b", "w+b", "a+b", "rb+", "wb+", "ab+"}:
                raise BridgeError(f"{where}: unsupported $fopen mode: {mode!r}")
            writing = mode not in ("r", "rb")
        yield (path, writing, token.line, arguments[0][0].start + 1, len(path)) if locations else (path, writing, token.line)


def spectre_outputs(text, origin, chain=()):
    items = tokens(text)
    for index, token in enumerate(items[:-1]):
        if token.kind != "word" or token.text not in ("writefinal", "write", "output") or items[index + 1].text != "=":
            continue
        where = f"{origin}:{token.line}"
        if chain:
            where += f" (reference chain: {' -> '.join(chain)})"
        if index + 2 >= len(items):
            raise BridgeError(f"{where}: missing Spectre output path")
        value = items[index + 2]
        if value.kind == "string":
            path = value.text[1:-1]
        else:
            path = re.match(r'[^\s)]+', text[value.start:])[0]
            if not re.fullmatch(r"[A-Za-z0-9_./+-]+", path):
                raise BridgeError(f"{where}: unsupported Spectre output expression: {path}; use dependency_mode=explicit with all inputs declared")
        yield path, token.line
