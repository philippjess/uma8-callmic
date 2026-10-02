"""Kleiner Leser für PipeWires SPA-JSON (Konfigurationsdateien), nur für Tests.

Schlüssel und Werte dürfen ohne Anführungszeichen stehen, „=“ oder „:“ trennen, Kommas sind optional,
„#“ beginnt einen Kommentar. Die Datei selbst ist ein Objekt ohne äußere Klammern. Werte ohne
Anführungszeichen bleiben Zeichenketten, nur `null` wird None."""
import re

_TOKEN = re.compile(r'\s+|#[^\n]*|"(?:[^"\\]|\\.)*"|[{}\[\]=:,]|[^\s{}\[\]=:,"#]+')


def _tokens(text: str) -> list[str]:
    return [t for t in (m.group() for m in _TOKEN.finditer(text))
            if not (t.isspace() or t.startswith("#") or t == ",")]


def _scalar(tok: str):
    if tok.startswith('"'):
        return tok[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return None if tok == "null" else tok


def _value(toks: list[str], i: int):
    if toks[i] == "{":
        obj, i = {}, i + 1
        while toks[i] != "}":
            key, i = _scalar(toks[i]), i + 1
            if toks[i] in ("=", ":"):
                i += 1
            assert key not in obj, f"Schlüssel doppelt: {key}"
            obj[key], i = _value(toks, i)
        return obj, i + 1
    if toks[i] == "[":
        arr, i = [], i + 1
        while toks[i] != "]":
            v, i = _value(toks, i)
            arr.append(v)
        return arr, i + 1
    return _scalar(toks[i]), i + 1


def loads(text: str) -> dict:
    obj, _ = _value(["{", *_tokens(text), "}"], 0)
    return obj
