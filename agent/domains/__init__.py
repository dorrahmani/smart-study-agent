"""
agent/domains — the life areas the secretary knows about, plugged into one core (16/11).

The owner 16/11: "It's my personal assistant. Because now I am a student it helps me most in learning, but in the future he
will be next to me in every step on my way." The core (memory of his facts / files / people, calendar, reminders and
tasks, follow-through, initiative caps, the ✅ gate, security) is the same whatever he does; what he does is a domain.

A domain module gives, all optional:
    NAME          'student'
    IDENTITY      one Hebrew clause about him in this domain ("סטודנט באוניברסיטת תל אביב", from SSA_UNIVERSITY)
    MIND_LINES    extra lines for the mind's system prompt (its sites, its people, where to look)
    active()      is it on now (a finished degree turns 'student' off without code changes)

Today only 'student' exists; SSA_DOMAINS (comma list, default 'student') picks which are on. The next ones (work,
money, home, health, travel) plug in here; docs/FEATURES.md lists what is still student-only.
A domain that is off (student without SSA_STUDY=1) adds nothing.
"""
from __future__ import annotations

import importlib
import os

KNOWN = ('student',)


def active() -> list:
    names = [n.strip() for n in os.environ.get('SSA_DOMAINS', 'student').split(',') if n.strip()]
    out = []
    for n in names:
        if n not in KNOWN:
            continue
        mod = importlib.import_module(f'agent.domains.{n}')
        if getattr(mod, 'active', lambda: True)():
            out.append(mod)
    return out


def identity(owner: str = 'המשתמש') -> str:
    """"המשתמש סטודנט באוניברסיטת תל אביב" — or just his name when no domain says who he is."""
    parts = [getattr(m, 'IDENTITY', '') for m in active()]
    parts = [p for p in parts if p]
    return f"{owner} {' ו'.join(parts)}" if parts else owner


def mind_lines() -> str:
    return '\n'.join(getattr(m, 'MIND_LINES', '').strip() for m in active() if getattr(m, 'MIND_LINES', '').strip())
