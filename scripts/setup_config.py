#!/usr/bin/env python3
"""Создаёт config.yaml из config.example.yaml, сохраняя комментарии.

  setup_config.py --token 123:ABC --admin 111111111 --key openai=sk-... --key gemini=AIza... \
                  [--example config.example.yaml] [--out config.yaml]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def fill(text: str, token: str, admins: list[int], keys: dict[str, list[str]]) -> str:
    text, n = re.subn(r'^(\s*token:\s*)"[^"]*"', lambda m: m.group(1) + json.dumps(token), text, count=1, flags=re.M)
    if not n:
        raise SystemExit("В шаблоне не найден telegram.token")
    text, n = re.subn(r"^(\s*admins:\s*)\[[^\]]*\]", lambda m: m.group(1) + json.dumps(admins), text,
                      count=1, flags=re.M)
    if not n:
        raise SystemExit("В шаблоне не найден access.admins")
    for provider, values in keys.items():
        # ищем блок провайдера и первую строку keys: [] внутри него
        rx = re.compile(rf"(^  {re.escape(provider)}:\n(?:(?:    .*|\s*#.*|\s*)\n)*?    keys: )\[\]", re.M)
        text, n = rx.subn(lambda m: m.group(1) + json.dumps(values, ensure_ascii=False), text, count=1)
        if not n:
            raise SystemExit(f"Провайдер {provider} не найден в шаблоне (или у него уже есть ключи)")
    return text


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--example", default="config.example.yaml")
    ap.add_argument("--out", default="config.yaml")
    ap.add_argument("--token", required=True)
    ap.add_argument("--admin", type=int, action="append", required=True)
    ap.add_argument("--key", action="append", default=[], help="provider=KEY, можно несколько раз")
    a = ap.parse_args()

    keys: dict[str, list[str]] = {}
    for item in a.key:
        prov, _, key = item.partition("=")
        if prov and key:
            keys.setdefault(prov.strip(), []).append(key.strip())

    out = fill(Path(a.example).read_text(encoding="utf-8"), a.token, a.admin, keys)
    Path(a.out).write_text(out, encoding="utf-8")
    print(f"Записан {a.out}: админы {a.admin}, ключи для: {', '.join(keys) or 'нет'}", file=sys.stderr)


if __name__ == "__main__":
    main()
