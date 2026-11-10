#!/usr/bin/env python3
"""OptMem invariants, checked against a synthetic life of 5000 memories.

Uses a fake compressor (join + truncate) so the run is deterministic and free.
"""

import contextlib
import datetime
import errno
import hashlib
import io
import json
import os
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader

sys.dont_write_bytecode = True
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

HERE = os.path.dirname(os.path.realpath(__file__))
MEMO = os.path.join(HERE, "memo")
INSTALL_SH = os.path.join(HERE, "install.sh")
cli = SourceFileLoader("memo_cli", MEMO).load_module()
cover = cli.cover


def complete(T):
    """Every block buildable from T memories, smallest first. The oracle for
    the tool's `pending()`: written straight from the definition, so a bug in
    the fast version (which reads level lengths, never scanning) shows up."""
    out, size = [], 2
    while size <= T:
        out += [(i * size, (i + 1) * size) for i in range(T // size)]
        size *= 2
    return out

# The shipped defaults. A fresh process starts from these, so an in-process
# call must too, or one store's config would leak into the next.
DEFAULTS = {k: getattr(cli, k) for k in
            ("ENTRY_CHARS", "SUMMARY_CHARS", "WAKE_LINES", "PART_CHARS",
             "PART_LINES")}

N = 2000
WAKE_LINES = cli.WAKE_LINES   # the shipped budget, not a second copy of it
PART_CHARS = cli.PART_CHARS
# Verified caps of the harnesses in the wild: Claude Code cuts a command's
# output at 30,000 chars (middle), pi at 50 KB / 2000 lines (head), Codex
# budgets 10,000 tokens. A part must fit the strictest of each kind.
CAP_CHARS, CAP_LINES = 30000, 2000
ok, fail = 0, 0


def check(cond, msg):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
        print("FAIL: " + msg)

# The approved format table is the one source of widths and hard limits.
check(cli.FORMATS == {1: (320, 288, 280, 280), 2: (2112, 576, 2048, 560)},
      "the format table changed: %r" % (cli.FORMATS,))
check(cli.STORE_VERSION == 2 and cli.LOG_REC == 2112
      and cli.TREE_REC == 576, "the shipped format is not 2")
check(cli.SUMMARY_CHARS == 560 and cli.KNOBS["SUMMARY_CHARS"][0] == 560,
      "SUMMARY_CHARS is not a 560-byte knob")
check(cli.RAW_MAX == 16 and cli.RAW_BYTES == 8192,
      "the raw compression budget changed: %r/%r"
      % (cli.RAW_MAX, cli.RAW_BYTES))



# ---- pure block math -------------------------------------------------

for T in list(range(1, 400)) + [1000, 4096, 10000, 65536, 100003]:
    c = cover(T, WAKE_LINES)
    check(len(c) <= WAKE_LINES, "T=%d: %d lines > budget" % (T, len(c)))
    check(c[0][0] == 0 and c[-1][1] == T, "T=%d: does not span [0,T)" % T)
    for a, b in zip(c, c[1:]):
        check(a[1] == b[0], "T=%d: gap or overlap at %s %s" % (T, a, b))
    for lo, hi in c:
        s = hi - lo
        check(s & (s - 1) == 0 and lo % s == 0,
              "T=%d: [%d,%d) is not an aligned power-of-two block" % (T, lo, hi))
    for a, b in zip(c, c[1:]):
        check(b[1] - b[0] <= a[1] - a[0],
              "T=%d: detail does not increase toward the present" % T)

check(cover(300, 320) == [(i, i + 1) for i in range(300)],
      "under budget, memory should be verbatim")

# every block a cover ever needs must be buildable. cover() costs a 60-step
# binary search, so this walks every tree shape up to 300 and then samples:
# the property is structural, not a function of the exact T.
seen = set()
for T in list(range(1, 300)) + [512, 700, 1000, 1023, 1024, 2000, 2999]:
    seen.update(b for b in cover(T, WAKE_LINES) if b[1] - b[0] > 1)
buildable = set(complete(3000))
check(seen <= buildable, "a cover wants a block that complete() never yields")

# work never spikes: naps created by one new memory
worst, prev = 0, 0
for T in range(1, N):
    cur = len(complete(T))
    worst = max(worst, cur - prev)
    prev = cur
check(worst <= 16, "a single memory created %d naps" % worst)

# ---- the real CLI ----------------------------------------------------

d = tempfile.mkdtemp(prefix="optmem-test-")
memo = [sys.executable, MEMO]


class Result:
    def __init__(self, returncode, stdout, stderr):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def run(*args, store=None):
    """One `memo` command, in-process, through the real locked entry point.
    Spawning an interpreter per call cost ~40ms x ~2000 naps; the
    cross-process behaviour that genuinely needs real processes (the lock) is
    tested with real processes below."""
    old = os.environ.get("MEMORY_DIR")
    old_me = cli.ME
    os.environ["MEMORY_DIR"] = store or d
    for k, v in DEFAULTS.items():
        setattr(cli, k, v)
    out, err, code = io.StringIO(), io.StringIO(), 0
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            sd = cli.store()
            cli.execute(sd, args[0], list(args[1:]))
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 0
    finally:
        cli.ME = old_me
        if old is None:
            os.environ.pop("MEMORY_DIR", None)
        else:
            os.environ["MEMORY_DIR"] = old
    return Result(code, out.getvalue(), err.getvalue())


def nap_id(out):
    """The block id from the command a nap prompt offers."""
    m = re.search(r"memo nap (\d+)-(\d+)", out)
    return "%s-%s" % m.groups() if m else None


def run_init(store):
    """Init may create the store, so it cannot pass through store() first."""
    old = os.environ.get("MEMORY_DIR")
    old_me = cli.ME
    os.environ["MEMORY_DIR"] = store
    for k, v in DEFAULTS.items():
        setattr(cli, k, v)
    out, err, code = io.StringIO(), io.StringIO(), 0
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cli.execute(store, "init", [])
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 0
    finally:
        cli.ME = old_me
        if old is None:
            os.environ.pop("MEMORY_DIR", None)
        else:
            os.environ["MEMORY_DIR"] = old
    return Result(code, out.getvalue(), err.getvalue())


def offered(out):
    """The line offering a command. Every command handed to an agent must be
    an order, not a label: `Run: memo ...`, never `next: memo ...`."""
    return [l for l in out.splitlines() if "memo nap " in l or "memo wake " in l]


# the real entry point still has to work: shebang, argv parsing, exit code
smoke = subprocess.run(memo + ["wake"], env=dict(os.environ, MEMORY_DIR=d),
                       capture_output=True, text=True)
check(smoke.returncode == 0 and "No memories yet" in smoke.stdout,
      "the memo CLI does not run: " + smoke.stdout + smoke.stderr)

# a typo in MEMORY_DIR must not silently open a second, empty identity
ghost = subprocess.run(memo + ["wake"], capture_output=True, text=True,
                       env=dict(os.environ, MEMORY_DIR=d + "-typo"))
check(ghost.returncode == 1 and "No memory at" in ghost.stderr,
      "a missing MEMORY_DIR was created instead of reported")
check(not os.path.exists(d + "-typo"), "a missing MEMORY_DIR was created")

# the fresh-user path: no MEMORY_DIR, wake refuses, init creates the memory,
# prints the manual session policy, and is idempotent. The repo copy is
# unmanaged, so its default is <repo>/memory: never exercise that path from a
# test. Copy it beside an isolated HOME and use this copy for the whole
# fresh-home sequence.
fresh = {k: v for k, v in os.environ.items() if k != "MEMORY_DIR"}
fresh["HOME"] = tempfile.mkdtemp(prefix="optmem-fresh-home-")
fresh_opt = os.path.join(fresh["HOME"], ".optmem")
os.makedirs(fresh_opt)
shutil.copy2(MEMO, os.path.join(fresh_opt, "memo"))
freshmemo = [sys.executable, os.path.join(fresh_opt, "memo")]
noenv = subprocess.run(freshmemo + ["wake"], capture_output=True, text=True, env=fresh)
check(noenv.returncode == 1 and "memo init" in noenv.stderr,
      "with no MEMORY_DIR and no memory, wake must point at init")
init = subprocess.run(freshmemo + ["init"], capture_output=True, text=True, env=fresh)
check(init.returncode == 0 and "## Memory" in init.stdout
      and "explicit user activation" in init.stdout
      and "activate OptMem for the current session" in init.stdout
      and "Do not activate it at session startup" not in init.stdout,
      "init must print the manual session policy")
check("mandatory" not in init.stdout.lower()
      and "subagent" not in init.stdout.lower()
      and "AGENTS.md" not in init.stdout
      and "CLAUDE.md" not in init.stdout
      and "Paste this" not in init.stdout,
      "init still prints mandatory startup or subagent instructions")
check("Generate a manual-only skill" in init.stdout,
      "init must point at the manual-only skill")
check(os.path.exists(os.path.join(fresh["HOME"], ".optmem", "memory", "config")),
      "init must create ~/.optmem/memory with its config")
again = subprocess.run(freshmemo + ["init"], capture_output=True, text=True, env=fresh)
check(again.returncode == 0 and "Found" in again.stdout, "init must be idempotent")
woke = subprocess.run(freshmemo + ["wake"], capture_output=True, text=True, env=fresh)
check(woke.returncode == 0 and "You are awake." in woke.stdout,
      "after init, wake must work with zero configuration")

# Every command the tool prints must RUN on the machine it printed it on.
# `curl | sh` puts nothing on PATH, so a bare `memo nap ...` would not: the
# whole loop (note -> merge prompt -> nap) dies on `command not found`.
# Nix has no python3 in /usr/bin: keep the interpreter's own directory on
# PATH, but never the store's directory -- the order uses the memo path.
bare = dict(fresh, PATH=os.pathsep.join([os.path.dirname(sys.executable),
                                         "/usr/bin", "/bin"]))
subprocess.run(freshmemo + ["note", "the first thing that happened"], env=bare,
               capture_output=True)
asked = subprocess.run(freshmemo + ["note", "the second thing that happened"],
                       env=bare, capture_output=True, text=True)
order = [l[5:] for l in asked.stdout.splitlines() if l.startswith("Run: ")]
check(len(order) == 1, "note did not order a compression: " + asked.stdout)
obeyed = subprocess.run(order[0].replace('"<your line>"', '"both things"'),
                        shell=True, env=bare, capture_output=True, text=True)
check(obeyed.returncode == 0 and "saved" in obeyed.stdout,
      "the order the tool printed does not run with nothing on PATH: %r -> %s"
      % (order[0], obeyed.stderr.strip()))

# a size written by hand into `config` must not brick the tool with a
# recovery that is itself broken: name the file and the line
badcfg = os.path.join(fresh["HOME"], ".optmem", "memory", "config")
with open(badcfg, "a") as f:
    f.write("WAKE_LNES = 100\n")
for c in (["wake"], ["config"]):
    r_ = subprocess.run(freshmemo + c, capture_output=True, text=True, env=fresh)
    check(r_.returncode == 1 and "config line" in r_.stderr
          and "WAKE_LNES" in r_.stderr,
          "a typo in config does not say where it is: " + r_.stderr)
open(badcfg, "w").write("")

# the filesystem is the one thing the tool does not control: report it in the
# tool's own voice, never as a Python traceback. Point MEMORY_DIR at a regular
# file, never at the repo's own default path.
not_a_store = os.path.join(fresh["HOME"], "not-a-store")
open(not_a_store, "w", encoding="utf-8").write("")
r_ = subprocess.run(freshmemo + ["init"], capture_output=True, text=True,
                    env=dict(fresh, MEMORY_DIR=not_a_store))
check(r_.returncode == 1 and "Traceback" not in r_.stderr
      and "Not a directory" in r_.stderr,
      "a filesystem error printed a traceback: " + r_.stderr)
check(not os.path.exists(os.path.join(fresh["HOME"], ".optmem", "memory",
                                      "TREE", "not-a-store")),
      "a failed init wrote through a file MEMORY_DIR")
shutil.rmtree(fresh["HOME"])


r = run("note", "x" * 281)
check(r.returncode == 1 and "Too long" in r.stderr, "over-long note accepted")
r = run("note", "two\nlines")
check(r.returncode == 1 and "one line" in r.stderr, "multi-line note accepted")
r = run("note", "   ")
check(r.returncode == 1, "empty note accepted")
r = run("wake")
check("No memories yet" in r.stdout, "empty wake should say so")
check(r.stdout.rstrip().endswith("You are awake."),
      "an empty wake must still end with `You are awake.`")

with open(os.path.join(d, "seed.txt"), "w") as f:
    day = datetime.date(2020, 1, 1)
    for i in range(N):
        f.write("%s memory number %d, a thing that happened, was weighed "
                "against the rest of the week, turned out to matter more than "
                "anyone guessed at the time, and left a mark on every plan "
                "that followed it\n"
                % ((day + datetime.timedelta(days=i // 5)).isoformat(), i))
r = run("import", os.path.join(d, "seed.txt"))
check("Imported %d" % N in r.stdout, "import failed: " + r.stdout + r.stderr)
check(not os.path.exists(os.path.join(d, "config")),
      "a store wrote its own config file: the defaults are now frozen in it")

r = run("wake")
check(r.returncode == 1 and "Cannot wake" in r.stdout,
      "wake must refuse while work is pending")
check("wake again" in r.stdout,
      "the refusal must order the agent back to wake")
check("None" not in r.stdout, "the refusal printed a Python None")

# nap loop, with a fake compressor
naps = 0
r = run("nap")
check("Compress memories #" in r.stdout, "nap prompt must name its object")
while "Nothing left to compress" not in r.stdout:
    line = offered(r.stdout)
    check(bool(line), "no command offered:\n" + r.stdout + r.stderr)
    if not line:
        break
    check(line[0].startswith("Run: "), "a command was offered as a label, not "
          "an order: %r" % line[0])
    bid = nap_id(r.stdout)
    body = [l.strip() for l in r.stdout.splitlines() if l.startswith("  #")]
    r = run("nap", bid, (" ".join(body)[:280]).strip() or "empty")
    check(r.returncode == 0, "nap rejected a valid merge: " + r.stderr)
    naps += 1
check("You are awake" not in r.stdout,
      "nap must never claim the agent is awake; only wake may")
check(naps == len(complete(N)), "did %d naps, expected %d" % (naps, len(complete(N))))

r = run("wake")
check(r.returncode == 0, "wake still refuses after a full nap chain")

# the document survives pagination, and every part fits every harness's cap
parts, k = [], 1
while True:
    r = run("wake", str(k))
    if r.returncode != 0:
        break
    body = [l for l in r.stdout.splitlines() if l.startswith("#")]
    check(len(r.stdout) < CAP_CHARS, "part %d is %d chars, over the %d cap"
          % (k, len(r.stdout), CAP_CHARS))
    check(len(r.stdout.splitlines()) < CAP_LINES, "part %d is over %d lines"
          % (k, CAP_LINES))
    parts.append(body)
    k += 1
check(len(parts) > 1, "a %d-line memory should need more than one part" % WAKE_LINES)
lines = [l for p in parts for l in p]
check(len(lines) == WAKE_LINES, "woke with %d lines, want %d" % (len(lines), WAKE_LINES))
check(lines[-1].startswith("#%d " % (N - 1)), "newest memory not last / not raw")
check(lines[0].startswith("#0-"), "oldest line should be a summary block")
check(re.search(r"Run: .*memo wake 2", run("wake").stdout),
      "part 1 must ORDER the next command, not label it")
check("You are awake." in run("wake", str(len(parts))).stdout,
      "last part must say it is last")
check(run("wake", str(len(parts) + 1)).returncode == 1, "a nonexistent part should fail")

# append-only: nothing was ever rewritten
logsz = os.path.getsize(os.path.join(d, "LOG.txt"))
run("note", "one more thing happened today")
check(os.path.getsize(os.path.join(d, "LOG.txt")) > logsz, "note did not append")
check(logsz % 320 == 0, "LOG.txt is not a whole number of records")
for f in os.listdir(os.path.join(d, "TREE")):
    check(os.path.getsize(os.path.join(d, "TREE", f)) % 288 == 0,
          "TREE/%s is not a whole number of records" % f)

# a nap when nothing is pending writes nothing and says so
r = run("nap", "0-1", "attempted overwrite")
check(r.returncode == 0 and "Nothing left to compress" in r.stdout,
      "nap with nothing pending must say so and write nothing")

# recall reaches memories the summaries lost, and matches the whole line:
# id and date included, not just the text
r = run("recall", "memory number 7,")
check(r.returncode == 0 and "#7 " in r.stdout, "recall missed a memory")
check("1 match." in r.stdout, "a single match is not `1 matches`: " + r.stdout)
r = run("recall", "^#7 ")
check("memory number 7," in r.stdout, "recall cannot find a memory by id")
r = run("recall", "2020-01-02")
check("#7 " in r.stdout and "5 matches." in r.stdout,
      "recall cannot find memories by date: " + r.stdout)


# zoom: one tree node, opened into its two halves. The tool only reads;
# the agent is the navigator: it descends from a wake line by halving, and
# may leap to any block id it can name.
def halves(bid):
    r = run("zoom", bid)
    check(r.returncode == 0, "zoom %s failed: %s" % (bid, r.stderr))
    out = []
    for line in r.stdout.splitlines():
        m = re.match(r"#(\d+)(?:-(\d+))? ", line)
        check(bool(m), "zoom printed a line with no id: %r" % line)
        a = int(m.group(1))
        out.append((a, int(m.group(2)) + 1 if m.group(2) else a + 1))
    return out


target, lo, hi, calls = 777, 0, 1024, 0
while hi - lo > 1:
    mid = (lo + hi) // 2
    kids = halves("%d-%d" % (lo, hi - 1))
    check(kids == [(lo, mid), (mid, hi)],
          "zoom %d-%d is not its two halves: %r" % (lo, hi - 1, kids))
    lo, hi = kids[target >= mid]
    calls += 1
check(lo == target and calls == 10,
      "halving 1024 memories took %d calls and landed on #%d" % (calls, lo))
check("memory number %d," % target in run("zoom", "776-777").stdout,
      "the last zoom must print the raw memories themselves")

# the unbuilt tail is named, the empty future is omitted
r = run("zoom", "1024-2047")  # T is N+1, so the right half has no summary
check("#1536-2047 not compressed yet" in r.stdout,
      "an unbuilt half must say so: " + r.stdout)
r = run("zoom", "%d-%d" % (N, N + 1))  # the newest memory + one not yet made
check(r.stdout.count("\n") == 1 and "#%d " % N in r.stdout,
      "a half beyond the newest memory must be omitted: " + r.stdout)

# zoom answers with the tree's own records, so the id must BE a node
check(run("zoom", "3-9").returncode == 1, "zoom accepted a non-block")
check(run("zoom", "9-3").returncode == 1, "zoom accepted a backwards range")
check(run("zoom").returncode == 1, "zoom with no id must show usage")
r = run("zoom", "1048576-2097151")
check(r.returncode == 1 and "beyond the memory" in r.stderr
      and "memo wake" in r.stderr, "zoom past the end must name a way back")


def treesize():
    t = os.path.join(d, "TREE")
    return sum(os.path.getsize(os.path.join(t, f)) for f in os.listdir(t))

before, logsize = treesize(), os.path.getsize(os.path.join(d, "LOG.txt"))
r = run("forget", "16-31")
check("16-31" in r.stdout, "forget did not report the block: " + r.stdout + r.stderr)
check(treesize() < before, "forget did not shrink the tree")
check(os.path.getsize(os.path.join(d, "LOG.txt")) == logsize, "forget touched the log")
check(run("wake").returncode == 1, "wake should refuse after a forget")
# a settled block cannot be rewritten. Resubmitting one (two sessions paid
# the same nap) is not an error: say it is settled, write nothing
mid = treesize()
r = run("nap", "0-1", "attempted overwrite")
check(r.returncode == 0 and "already settled" in r.stdout,
      "resubmitting a settled block was not reported as settled: " + r.stderr)
check(treesize() == mid, "resubmitting a settled block wrote something")
# a block that is neither settled nor next (here: a dropped ancestor,
# submitted before its half is rebuilt) is a real mistake
r = run("nap", "0-31", "out of order")
check(r.returncode == 1 and "Wrong block" in r.stderr,
      "an out-of-order block was accepted")
n = 0
while True:
    r = run("nap")
    if "Nothing left to compress" in r.stdout:
        break
    bid = nap_id(r.stdout)
    check(run("nap", bid, "rebuilt after forget").returncode == 0, "rebuild rejected")
    n += 1
check(n > 0, "forget created no work")
check(run("wake").returncode == 0, "wake still refuses after rebuilding")
check(treesize() == before, "tree did not return to its original size")
check(run("forget", "17-32").returncode == 1, "forgetting a non-block should fail")
# a summary that is not built yet is named as such, never left blank
run("forget", "16-31")
z = run("zoom", "0-31").stdout
check("#16-31 not compressed yet" in z, "zoom hid a missing summary: " + z)
while True:
    bid = nap_id(run("nap").stdout)
    if not bid:
        break
    run("nap", bid, "rebuilt after forget")
check(treesize() == before, "tree did not return to its original size")
check(run("forget", "1048576-1048577").returncode == 1, "forgetting a missing block should fail")

# UTF-8: multi-byte characters must not shift record boundaries or dodge limits
run("note", "reunião com João em São Paulo: ação aprovada, coração tranquilo")
run("note", "a plain ascii memory right after the accented one")
r = run("recall", "coração")
check("João" in r.stdout, "recall lost the accented memory: " + r.stdout + r.stderr)
r = run("recall", "plain ascii memory right after")
check("#%d " % (N + 2) in r.stdout, "record after a multi-byte one reads shifted")
r = run("note", "ã" * 150)
check(r.returncode == 1 and "300 bytes" in r.stderr,
      "multi-byte note dodged the byte limit: " + r.stderr)

# note landed -> its blocks are pending; settle before the final wake check
while True:
    r = run("nap")
    if "Nothing left to compress" in r.stdout:
        break
    bid = nap_id(r.stdout)
    run("nap", bid, "settled")
check(run("wake").returncode == 0, "wake refuses at the very end")

# a part is rendered as of T, so a note landing mid-wake cannot shift a
# boundary and silently drop a line
T0 = os.path.getsize(os.path.join(d, "LOG.txt")) // 320
before = run("wake", "1", str(T0))
check(before.returncode == 0, "as-of-T wake failed: " + before.stdout + before.stderr)
run("note", "a note that lands between two wake calls")
check(run("wake", "1", str(T0)).stdout == before.stdout,
      "a note between parts changed an already-rendered part")
check(run("wake", "1", str(T0 + 99)).returncode == 1, "wake accepted a future T")

# ...and the agent pays that note's compressions on the spot, as it is told
# to. The tree then holds MORE blocks than the snapshot needs: a level must
# never count as negative work, or the rest of the wake is refused with an
# impossible number.
while True:
    r = run("nap")
    if "Nothing left to compress" in r.stdout:
        break
    run("nap", nap_id(r.stdout), "settled mid-wake")
r = run("wake", "1", str(T0))
check(r.returncode == 0 and r.stdout == before.stdout,
      "a compression paid mid-wake broke the rest of the wake:\n"
      + r.stdout + r.stderr)
for T in list(range(1, 40)) + [T0 - 1, T0, T0 + 1]:
    check(cli.pending_count(d, T) == len(cli.pending(d, T)),
          "pending_count disagrees with pending at T=%d" % T)

# recall must not hand back more than a harness will carry
r = run("recall", "memory number")
check(len(r.stdout) < CAP_CHARS, "recall returned %d chars" % len(r.stdout))
check("Narrow the regex" in r.stdout, "recall did not say it had been capped")


def fmt_load(store):
    """Widths are process globals; load the store before a direct helper read."""
    cli.load_format(store)
    return cli.STORE_VERSION


def data_files(store):
    """Raw bytes of the files migration must copy, never rewrite in place."""
    names = ["LOG.txt", "config"]
    t = os.path.join(store, "TREE")
    names += ["TREE/" + f for f in sorted(os.listdir(t)) if f.isdigit()]
    return {n: open(os.path.join(store, n), "rb").read()
            for n in names if os.path.isfile(os.path.join(store, n))}


def tree_rows(store):
    """Decoded summary records at the store's own width."""
    fmt_load(store)
    t, rows = os.path.join(store, "TREE"), {}
    for f in os.listdir(t):
        if f.isdigit():
            with open(os.path.join(t, f), "rb") as h:
                buf = h.read()
            rows[int(f)] = [buf[i:i + cli.TREE_REC].decode().rstrip()
                            for i in range(0, len(buf) - cli.TREE_REC + 1,
                                           cli.TREE_REC)]
    return rows


def make_legacy_migration(prefix):
    """A legacy store rich enough to prove migration copies meaning, not bytes:
    distinct dates and ids, tombstones, a NUL-only block, whitespace, a #, UTF-8,
    a config override, file modes, and torn tails on both record files."""
    d = tempfile.mkdtemp(prefix=prefix)
    seed = os.path.join(d, "seed.txt")
    with open(seed, "w", encoding="utf-8") as f:
        f.write("2020-01-01 zero plain\n"
                "2020-02-02 one  spaces #tag café →\n"
                "2020-03-03 two tomb target\n"
                "2020-04-04 three tomb target\n"
                "2020-05-05 four reunião ação\n")
    r = run("import", seed, store=d)
    check(r.returncode == 0 and "Imported 5 memories" in r.stdout,
          "migration fixture import failed: " + r.stdout + r.stderr)
    for i in (2, 3):
        r = run("delete", str(i), store=d)
        check(r.returncode == 0, "migration fixture delete %d failed" % i)
    settle(d)
    check(tree_rows(d) == {2: ["settled", cli.EMPTY_SUMMARY],
                           4: ["settled"]},
          "migration fixture tree is not the expected legacy tree: %r"
          % tree_rows(d))
    check(cli.revision(d) == 2, "migration fixture revision is not 2")
    with open(os.path.join(d, "config"), "w", encoding="utf-8") as f:
        f.write("# kept by migration\nWAKE_LINES = 1\nENTRY_CHARS = 200\n"
                "SUMMARY_CHARS = 100\n")
    os.chmod(os.path.join(d, "LOG.txt"), 0o600)
    t = os.path.join(d, "TREE")
    for f in os.listdir(t):
        os.chmod(os.path.join(t, f), 0o640)
    with open(os.path.join(d, "LOG.txt"), "ab") as f:
        f.write(b"#9 2029-01-01 torn tail")
    with open(os.path.join(t, "2"), "ab") as f:
        f.write(b"torn summary")
    # A record that is only padding is a blank summary, not corruption to the
    # migration: its logical text is empty and must stay empty in format 2.
    with open(os.path.join(t, "8"), "wb") as f:
        f.write(b" " * (288 - 1) + b"\n")
    os.chmod(os.path.join(t, "8"), 0o640)
    return d


# ---- concurrency and crash recovery ----------------------------------

d2 = tempfile.mkdtemp(prefix="optmem-race-")
env2 = dict(os.environ, MEMORY_DIR=d2)
P = 16  # real processes: this is the cross-process lock under test
procs = [subprocess.Popen(memo + ["note", "parallel note %d" % i], env=env2,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
         for i in range(P)]
for p in procs:
    try:
        p.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        check(False, "a parallel note hung")
    check(p.returncode == 0, "a parallel note failed: %s" % p.returncode)
with open(os.path.join(d2, "LOG.txt"), "rb") as f:
    recs = [f.read(320) for _ in range(P)]
ids = [r.decode().split()[0] for r in recs if r.strip()]
check(len(ids) == P, "%d of %d parallel notes survived" % (len(ids), P))
check(len(set(ids)) == P, "parallel notes collided on an id: %s" % sorted(ids))
check(sorted(ids) == sorted("#%d" % i for i in range(P)),
      "parallel note ids are not 0..%d: %s" % (P - 1, sorted(ids)))

# a crash mid-append leaves a partial record; the next append must drop it,
# or every later record is misaligned forever
with open(os.path.join(d2, "LOG.txt"), "ab") as f:
    f.write(b"#99 2026-01-01 a half-written record killed by a power cut")
r = run("note", "the memory right after a torn write", store=d2)
check(r.returncode == 0, "note failed after a torn write: " + r.stderr)
sz = os.path.getsize(os.path.join(d2, "LOG.txt"))
check(sz % 320 == 0, "LOG.txt left misaligned after a torn write: %d" % sz)
check("Saved as #%d" % P in r.stdout, "torn record was counted as a memory")
r = run("recall", "right after a torn write", store=d2)
check("#%d " % P in r.stdout, "the memory after a torn write reads wrong")

# a memory small enough to fit one part must still end with the terminator
# the agent was told to wait for
while True:
    r = run("nap", store=d2)
    if "Nothing left to compress" in r.stdout:
        break
    bid = nap_id(r.stdout)
    run("nap", bid, "settled", store=d2)
r = run("wake", store=d2)
check(r.stdout.rstrip().endswith("You are awake."),
      "a one-part wake never says `You are awake.`:\n" + r.stdout)

# a blank summary record (a corrupt write) is work nap cannot see: wake must
# name the one exit, `forget`, instead of refusing forever
d3 = tempfile.mkdtemp(prefix="optmem-blank-")
for i in range(4):
    run("note", "corrupt store memory %d" % i, store=d3)
for bid, s in (("0-1", "one"), ("2-3", "two"), ("0-3", "all")):
    run("nap", bid, s, store=d3)
with open(os.path.join(d3, "config"), "w") as f:
    f.write("WAKE_LINES = 2\n")
with open(os.path.join(d3, "TREE", "2"), "r+b") as f:
    f.write(b" " * 287 + b"\n")
r = run("wake", store=d3)
check(r.returncode == 1 and "forget 0-1" in r.stderr
      and "None" not in r.stdout,
      "a blank summary must point at forget:\n" + r.stdout + r.stderr)

# an unreadable level is a filesystem failure and must surface as one --
# reading it as "not compressed yet" offers work that cannot be done
os.chmod(os.path.join(d3, "TREE", "2"), 0)
r_ = subprocess.run(memo + ["wake"], capture_output=True, text=True,
                    env=dict(os.environ, MEMORY_DIR=d3))
check(r_.returncode == 1 and "Permission denied" in r_.stderr
      and "not compressed" not in r_.stdout,
      "an unreadable level was read as pending work: " + r_.stdout + r_.stderr)
os.chmod(os.path.join(d3, "TREE", "2"), 0o644)

# an impossible calendar date would poison every later import: the store's
# order check compares against it forever
with open(os.path.join(d3, "bad.txt"), "w") as f:
    f.write("2027-99-99 an impossible date\n")
r = run("import", os.path.join(d3, "bad.txt"), store=d3)
check(r.returncode == 1 and "not a real date" in r.stderr,
      "import accepted an impossible date: " + r.stdout + r.stderr)
shutil.rmtree(d3)

# the same blank-record dead end at the other site: a big block's half
d4 = tempfile.mkdtemp(prefix="optmem-half-")
for i in range(32):
    run("note", "half probe memory %d" % i, store=d4)
while True:
    r = run("nap", store=d4)
    if "Compress memories #0-31 " in r.stdout:
        break
    run("nap", nap_id(r.stdout), "settled", store=d4)
with open(os.path.join(d4, "TREE", "16"), "r+b") as f:
    f.write(b" " * 287 + b"\n")
r = run("nap", store=d4)
check(r.returncode == 1 and "forget 0-15" in r.stderr,
      "a blank half summary must point at forget: " + r.stdout + r.stderr)
shutil.rmtree(d4)

# the store is UTF-8 whatever the locale says: without pinning the streams,
# one arrow in a memory made wake crash forever on a latin-1 machine
d5 = tempfile.mkdtemp(prefix="optmem-utf8-")
run("note", "an arrow \u2192 survives any locale", store=d5)
r_ = subprocess.run(memo + ["wake"], capture_output=True,
                    env=dict(os.environ, MEMORY_DIR=d5,
                             PYTHONIOENCODING="latin-1"))
check(r_.returncode == 0 and "\u2192".encode() in r_.stdout,
      "wake must print UTF-8 on a non-UTF-8 locale: "
      + repr(r_.stdout + r_.stderr))

# an import file that is not UTF-8 is refused -- neither a traceback nor,
# worse, silently mis-decoded into mojibake and stored forever
with open(os.path.join(d5, "latin1.txt"), "wb") as f:
    f.write(b"2027-01-01 caf\xe9 in latin-1\n")
r = run("import", os.path.join(d5, "latin1.txt"), store=d5)
check(r.returncode == 1 and "not UTF-8" in r.stderr,
      "a non-UTF-8 import file must be refused: " + r.stdout + r.stderr)

# a summary record holding invalid UTF-8 is the blank-record dead end in
# another coat: it must name forget, not print a traceback
run("note", "utf8 probe second memory", store=d5)
run("nap", "0-1", "both utf8 probes", store=d5)
with open(os.path.join(d5, "TREE", "2"), "r+b") as f:
    f.write(b"\xff\xfe corrupt bytes")
r = run("zoom", "0-3", store=d5)
check(r.returncode == 1 and "forget 0-1" in r.stderr,
      "a corrupt summary must point at forget: " + r.stdout + r.stderr)
shutil.rmtree(d5)

# re-running init on a lived-in store must not touch one byte of it: the
# whole setup is idempotent, so it is safe on every provision. `init` only
# ever makedirs(exist_ok), opens LOG.txt for APPEND, and writes `config`
# when absent -- never truncating, never overwriting a tuned config.
def fingerprint(path):
    out = {}
    for root, _, files in os.walk(path):
        for f in files:
            if f == ".lock":
                continue
            p = os.path.join(root, f)
            try:
                out[os.path.relpath(p, path)] = open(p, "rb").read()
            except FileNotFoundError:
                continue  # a broken fixture symlink is still a fixture file
    return out


# `memo config` is how a size is changed: it writes the file the tool reads
# back, an empty value restores the default, and a wake obeys immediately --
# nothing is recomputed, because a size only selects what gets printed.
r = run("config", "WAKE_LINES=12")
check("12" in r.stdout and "default 96" in r.stdout, "config did not set:\n" + r.stdout)
check(len(run("wake").stdout.splitlines()) <= 13, "wake ignored the new size")
r = run("config", "WAKE_LINES=")
check("default" not in r.stdout, "an empty value did not restore the default")
check(len(run("wake").stdout.splitlines()) > 13, "the default did not come back")
for bad in ("WAKE_LINES=0", "WAKE_LINES=x", "ENTRY_CHARS=999", "NOPE=1", "WAKE_LINES"):
    check(run("config", bad).returncode == 1, "config accepted %s" % bad)

with open(os.path.join(d, "config"), "a") as f:
    f.write("WAKE_LINES=12\n")           # a size the user tuned by hand
before = fingerprint(d)
check(len(before) > 3 and before["LOG.txt"], "the store under test is empty")
for _ in range(3):
    r = run("init")
    check(r.returncode == 0 and "Found" in r.stdout, "init on a live store failed")
check(fingerprint(d) == before, "init modified an existing memory")
r = run("wake")
check(r.stdout.rstrip().endswith("You are awake."), "wake broke after re-init")

# ---- edit and delete: stable ids, dates, records ---------------------


def nap_order(out):
    """The block id and revision from the compression order."""
    m = re.search(r'memo nap (\d+)-(\d+) "<your line>" (\d+)', out)
    return ("%s-%s" % (m.group(1), m.group(2)), m.group(3)) if m else None


def settle(store, summary="settled"):
    """Finish every compression with the revision the prompt offers."""
    for _ in range(10000):
        r = run("nap", store=store)
        offer = nap_order(r.stdout)
        if not offer:
            check(r.returncode == 0 and "Nothing left to compress" in r.stdout,
                  "nap stopped without an order: " + r.stdout + r.stderr)
            return
        r = run("nap", offer[0], summary, offer[1], store=store)
        check(r.returncode == 0, "nap %s failed: %s" % (offer[0], r.stderr))
    check(False, "settle did not finish")


def tree_counts(store):
    """Record count of every TREE level that exists."""
    cli.load_format(store)  # widths are process globals; load before a read
    t = os.path.join(store, "TREE")
    return {int(f): os.path.getsize(os.path.join(t, f)) // cli.TREE_REC
            for f in os.listdir(t) if f.isdigit()}


def log_records(store):
    cli.load_format(store)  # widths are process globals; load before a read
    with open(os.path.join(store, "LOG.txt"), "rb") as f:
        buf = f.read()
    return [buf[i:i + cli.LOG_REC]
            for i in range(0, len(buf), cli.LOG_REC)]


def data_state(store):
    tree = {}
    t = os.path.join(store, "TREE")
    for f in os.listdir(t):
        if f.isdigit():
            tree[f] = open(os.path.join(t, f), "rb").read()
    with open(os.path.join(store, "LOG.txt"), "rb") as f:
        log = f.read()
    return log, tree, cli.revision(store)


def leftovers(store):
    return [n for n in (".pending", ".LOG.tmp", ".revision.tmp")
            if os.path.exists(os.path.join(store, n))]


@contextlib.contextmanager
def patched(owner, name, fn):
    old = getattr(owner, name)
    setattr(owner, name, fn)
    try:
        yield
    finally:
        setattr(owner, name, old)


def run_crash(*args, store):
    """A command that raises OSError is the crash the journal must recover."""
    try:
        return run(*args, store=store)
    except OSError as e:
        return Result(1, "", "%s: %s" % (e.filename or "", e.strerror or e))


def crash_after_journal(store):
    """Crash exactly after the durable journal, before any target is replaced."""
    original = cli.recover_migration

    def wrapper(d):
        if os.path.exists(os.path.join(d, ".migration")):
            raise OSError(errno.EIO, "simulated crash after journal")
        return original(d)

    with patched(cli, "recover_migration", wrapper):
        return run_crash("migrate", store=store)


# ---- v2 stores and limits -------------------------------------------

# Only a nonexistent path can become a fresh v2 store. A mkdtemp directory is
# an existing, unmarked store, so every older fixture above stays legacy v1
# with its 320/288 record widths and 280-byte limits.
v2root = tempfile.mkdtemp(prefix="optmem-v2-")
v2 = os.path.join(v2root, "memory")
check(not os.path.exists(v2), "the fresh-store fixture must not exist first")
r = run_init(v2)
check(r.returncode == 0 and "Created" in r.stdout and "Format 2" in r.stdout,
      "a fresh init did not make a format 2 store: " + r.stdout + r.stderr)
check(json.load(open(os.path.join(v2, ".format")))["version"] == 2,
      "a fresh init did not write .format {version: 2}")
check(not os.path.exists(os.path.join(v2, ".config")),
      "the store grew an unknown .config file")
check(os.path.getsize(os.path.join(v2, "LOG.txt")) == 0,
      "a fresh store did not start with an empty log")
fresh_snap = fingerprint(v2)
r = run_init(v2)
check(r.returncode == 0 and "Found" in r.stdout and "Format 2" in r.stdout,
      "init on a fresh v2 store was not idempotent: " + r.stdout)
check(fingerprint(v2) == fresh_snap, "a second init changed the fresh store")

init_root = tempfile.mkdtemp(prefix="optmem-init-publish-")
init_path = os.path.join(init_root, "memory")
orig_init_atomic = cli.atomic_write


def fail_init_format(path, data):
    if os.path.basename(path) == ".format":
        raise OSError(errno.EIO, "simulated init format failure")
    return orig_init_atomic(path, data)


with patched(cli, "atomic_write", fail_init_format):
    try:
        run_init(init_path)
        check(False, "the fresh-init format failure was not raised")
    except OSError:
        check(True, "fresh-init format failure raised")
check(not os.path.exists(init_path),
      "failed fresh init exposed an unmarked store directory")
check(not os.listdir(init_root), "failed fresh init left its staging directory")
check(run_init(init_path).returncode == 0 and cli.read_format(init_path) == 2,
      "retry after interrupted fresh init did not create format 2")

race_init_path = os.path.join(init_root, "raced-memory")
orig_init_rename = cli.os.rename


def publish_legacy_before_init(source, target):
    if target == race_init_path:
        os.makedirs(os.path.join(target, "TREE"))
        with open(os.path.join(target, "LOG.txt"), "wb") as f:
            f.write(cli.pad("#0 2020-01-01 concurrent legacy memory", 320))
    return orig_init_rename(source, target)


with patched(cli.os, "rename", publish_legacy_before_init):
    r = run_init(race_init_path)
check(r.returncode == 0 and "Format 1" in r.stdout
      and not os.path.exists(os.path.join(race_init_path, ".format")),
      "fresh init overwrote a concurrent legacy store's format")
check(run("note", "later legacy memory", store=race_init_path).returncode == 0,
      "a legacy note failed after the init race")
check(os.path.getsize(os.path.join(race_init_path, "LOG.txt")) == 640
      and "concurrent legacy memory" in run("recall", "concurrent", store=race_init_path).stdout,
      "the init race lost a legacy memory or mixed record widths")
shutil.rmtree(init_root)

r = run("note", "x" * 2048, store=v2)
check(r.returncode == 0, "a 2048-byte note was rejected in format 2: " + r.stderr)
r = run("note", "x" * 2049, store=v2)
check(r.returncode == 1 and "2049 bytes" in r.stderr and "2048" in r.stderr,
      "a 2049-byte note was accepted in format 2: " + r.stderr)
r = run("note", "ã" * 682, store=v2)   # 2046 bytes
check(r.returncode == 0, "a multi-byte note under the v2 limit was rejected: "
      + r.stderr)
r = run("note", "€" * 683, store=v2)   # 2049 bytes
check(r.returncode == 1 and "2049 bytes" in r.stderr,
      "a multi-byte note over the v2 limit was accepted: " + r.stderr)
check(os.path.getsize(os.path.join(v2, "LOG.txt")) == 2 * 2112,
      "v2 records are not 2112 bytes: %d"
      % os.path.getsize(os.path.join(v2, "LOG.txt")))
r = run("edit", "0", "e" * 2048, store=v2)
check(r.returncode == 0, "a 2048-byte edit was rejected in format 2: " + r.stderr)
r = run("edit", "0", "e" * 2049, store=v2)
check(r.returncode == 1 and "2049 bytes" in r.stderr,
      "a 2049-byte edit was accepted in format 2: " + r.stderr)
r = run("edit", "0", "€" * 683, store=v2)
check(r.returncode == 1 and "2049 bytes" in r.stderr,
      "a multi-byte edit over the v2 limit was accepted: " + r.stderr)
v2_atomic = cli.atomic_write


def interrupt_large_edit(path, data):
    if os.path.basename(path) == ".revision":
        raise OSError(errno.EIO, "simulated large edit interruption")
    return v2_atomic(path, data)


with patched(cli, "atomic_write", interrupt_large_edit):
    r = run_crash("edit", "0", "recovered " + "z" * 2038, store=v2)
check(r.returncode == 1, "the long format-2 edit did not interrupt")
r = run("config", store=v2)
check(r.returncode == 0 and cli.log_get(v2, 0)[2] == "recovered " + "z" * 2038
      and not leftovers(v2), "format-2 recovery used the legacy record width")
check(os.path.getsize(os.path.join(v2, "LOG.txt")) == 2 * 2112,
      "a rejected v2 edit changed the log length")
with open(os.path.join(v2, "missing.txt"), "w") as f:
    f.write("")
with open(os.path.join(v2, "import.txt"), "w", encoding="utf-8") as f:
    f.write(datetime.date.today().isoformat()
            + " short \u00e3\u20ac\U0001f600\n")
r = run("import", os.path.join(v2, "import.txt"), store=v2)
check(r.returncode == 0 and "Imported 1 memory" in r.stdout,
      "a short v2 import failed: " + r.stdout + r.stderr)
with open(os.path.join(v2, "import-long.txt"), "w", encoding="utf-8") as f:
    f.write(datetime.date.today().isoformat() + " " + "y" * 2048 + "\n")
r = run("import", os.path.join(v2, "import-long.txt"), store=v2)
check(r.returncode == 0, "a 2048-byte import line was rejected: " + r.stderr)
with open(os.path.join(v2, "import-over.txt"), "w", encoding="utf-8") as f:
    f.write(datetime.date.today().isoformat() + " " + "y" * 2049 + "\n")
r = run("import", os.path.join(v2, "import-over.txt"), store=v2)
check(r.returncode == 1 and "limit 2048" in r.stderr,
      "a 2049-byte import line was accepted: " + r.stderr)

# The summary cap is independent of the memory cap. A small ENTRY_CHARS must
# not shrink SUMMARY_CHARS, and a summary may use its full 560 bytes.
vsum = os.path.join(tempfile.mkdtemp(prefix="optmem-v2-sum-"), "memory")
run_init(vsum)
run("note", "a", store=vsum)
run("note", "b", store=vsum)
run("config", "ENTRY_CHARS=1", store=vsum)
r = run("nap", store=vsum)
check("at most 560 bytes" in r.stdout,
      "the summary prompt did not use the v2 summary cap: " + r.stdout)
r = run("config", store=vsum)
check("ENTRY_CHARS   1" in r.stdout and "SUMMARY_CHARS 560" in r.stdout,
      "a small ENTRY_CHARS changed the summary default: " + r.stdout)
r = run("note", "ab", store=vsum)
check(r.returncode == 1 and "limit 1" in r.stderr,
      "a 1-byte memory limit did not reject 2 bytes: " + r.stderr)
r = run("nap", "0-1", "s" * 561, "0", store=vsum)
check(r.returncode == 1 and "561" in r.stderr and "560" in r.stderr,
      "a 561-byte summary was accepted: " + r.stderr)
r = run("nap", "0-1", "s" * 560, "0", store=vsum)
check(r.returncode == 0 and "saved" in r.stdout,
      "a 560-byte summary was rejected: " + r.stdout + r.stderr)
check(os.path.getsize(os.path.join(vsum, "TREE", "2")) == 576,
      "a v2 summary record is not 576 bytes")
for bad in ("ENTRY_CHARS=2049", "SUMMARY_CHARS=561"):
    r = run("config", bad, store=v2)
    check(r.returncode == 1 and "at most" in r.stderr,
          "format 2 accepted %s: %s" % (bad, r.stderr))

# Config is per store, and defaults are recomputed for the format. A v2
# override must not leak into the legacy store or change its ceilings. The
# override is applied to the same store that was just freshly initialized, so
# this also proves the ceilings are re-read after each store load.
over_v2 = os.path.join(tempfile.mkdtemp(prefix="optmem-cfg-v2-"), "memory")
run_init(over_v2)
run("config", "ENTRY_CHARS=700", store=over_v2)
run("config", "SUMMARY_CHARS=400", store=over_v2)
run("config", "WAKE_LINES=5", store=over_v2)
r = run("config", store=over_v2)
check("ENTRY_CHARS   700" in r.stdout and "SUMMARY_CHARS 400" in r.stdout
      and "WAKE_LINES    5" in r.stdout,
      "the v2 store lost its overrides: " + r.stdout)
r = run("note", "x" * 701, store=over_v2)
check(r.returncode == 1 and "limit 700" in r.stderr,
      "a v2 override did not raise the memory ceiling back up: " + r.stderr)
r = run("note", "x" * 700, store=over_v2)
check(r.returncode == 0, "a memory at the overridden ceiling failed: " + r.stderr)

legcfg = tempfile.mkdtemp(prefix="optmem-cfg-legacy-")
r = run("config", store=legcfg)
check(r.returncode == 0 and "ENTRY_CHARS   280" in r.stdout
      and "SUMMARY_CHARS 280" in r.stdout,
      "the legacy defaults are not 280/280: " + r.stdout)
r = run("config", "ENTRY_CHARS=700", store=legcfg)
check(r.returncode == 1 and "at most 280" in r.stderr and "migrate" in r.stderr,
      "a legacy store accepted a v2 memory ceiling: " + r.stderr)
run("config", "ENTRY_CHARS=100", store=legcfg)
run("config", "SUMMARY_CHARS=90", store=legcfg)
r = run("config", store=v2)
check("ENTRY_CHARS   2048" in r.stdout and "SUMMARY_CHARS 560" in r.stdout,
      "a legacy override leaked into the v2 store: " + r.stdout)
r = run("config", store=legcfg)
check("ENTRY_CHARS   100" in r.stdout and "(default 280)" in r.stdout,
      "the legacy store lost its own override: " + r.stdout)

# Direct helpers must read at the width of the store they are given. The
# widths are process globals, so a helper that skips load_format after another
# store loaded would slice a legacy log at 2112 bytes or a v2 log at 320.
mix_leg = tempfile.mkdtemp(prefix="optmem-mix-legacy-")
for i in range(2):
    run("note", "mixed legacy %d" % i, store=mix_leg)
check(run("nap", "0-1", "mixed legacy pair", "0", store=mix_leg).returncode == 0,
      "the legacy mixed fixture could not summarize")
mix_v2 = os.path.join(tempfile.mkdtemp(prefix="optmem-mix-v2-"), "memory")
run_init(mix_v2)
for i in range(2):
    run("note", "mixed v2 %d" % i, store=mix_v2)
check(run("nap", "0-1", "mixed v2 pair", "0", store=mix_v2).returncode == 0,
      "the v2 mixed fixture could not summarize")
check(log_records(mix_leg)[0].startswith(b"#0 ")
      and len(log_records(mix_leg)) == 2,
      "log_records read a legacy store at the wrong width")
check(cli.LOG_REC == 320 and cli.TREE_REC == 288,
      "log_records did not load the legacy width")
check(tree_counts(mix_leg) == {2: 1} and cli.LOG_REC == 320
      and cli.TREE_REC == 288,
      "tree_counts read a legacy store at the wrong width: %r"
      % (tree_counts(mix_leg),))
check(log_records(mix_v2)[0].startswith(b"#0 ")
      and len(log_records(mix_v2)) == 2 and cli.LOG_REC == 2112,
      "log_records did not load the v2 width")
check(tree_counts(mix_v2) == {2: 1} and cli.TREE_REC == 576,
      "tree_counts did not load the v2 width: %r" % (tree_counts(mix_v2),))
fmt_load(mix_leg)
check(cli.log_get(mix_leg, 0) == (0, cli.log_get(mix_leg, 0)[1],
                                 "mixed legacy 0"),
      "a direct legacy read after loading its width failed")
fmt_load(mix_v2)
check(cli.log_get(mix_v2, 0)[2] == "mixed v2 0",
      "a direct v2 read after loading its width failed")
check(cli.defaults()["ENTRY_CHARS"] == 2048
      and cli.defaults()["SUMMARY_CHARS"] == 560,
      "defaults() did not follow the loaded v2 format: %r" % (cli.defaults(),))
fmt_load(mix_leg)
check(cli.defaults()["ENTRY_CHARS"] == 280
      and cli.defaults()["SUMMARY_CHARS"] == 280,
      "defaults() did not follow the loaded legacy format: %r"
      % (cli.defaults(),))

# A legacy store has no .format. Init stays idempotent and must not migrate it.
legacy_init = tempfile.mkdtemp(prefix="optmem-legacy-init-")
r = run("note", "legacy before init", store=legacy_init)
check(r.returncode == 0, "a legacy note without init failed: " + r.stderr)
check(os.path.getsize(os.path.join(legacy_init, "LOG.txt")) == 320,
      "a legacy record is not 320 bytes")
r = run_init(legacy_init)
check(r.returncode == 0 and "Found" in r.stdout and "Format 1" in r.stdout,
      "init on a legacy store did not report format 1: " + r.stdout)
check("migrate" in r.stdout, "legacy init did not offer the migration")
check(not os.path.exists(os.path.join(legacy_init, ".format")),
      "init silently migrated a legacy store")
check(os.path.getsize(os.path.join(legacy_init, "LOG.txt")) == 320,
      "legacy init changed the log width")

# ---- raw-body compression budget -------------------------------------

# RAW_MAX is a count; RAW_BYTES is a byte budget. Four full v2 memories are
# over 8192 bytes raw but each half fits, so a block of four must show its
# child summaries instead of the raw text.
draw = os.path.join(tempfile.mkdtemp(prefix="optmem-raw-"), "memory")
run_init(draw)
for ch in "abcd":
    check(run("note", ch * 2048, store=draw).returncode == 0,
          "the raw budget fixture could not note")
r = run("nap", store=draw)
check("Compress memories #0-1 " in r.stdout
      and "aaaa" in r.stdout and len(r.stdout.encode()) <= 8192 + 400,
      "a small raw block did not show raw text: " + r.stdout[:300])
for bid in ("0-1", "2-3"):
    check(run("nap", bid, "child of " + bid, "0", store=draw).returncode == 0,
          "the raw budget fixture could not summarize %s" % bid)
r = run("nap", store=draw)
raw_lines = [l for l in r.stdout.splitlines() if re.match(r"  #\d+ ", l)]
check("Compress memories #0-3 " in r.stdout
      and "  #0-1 child of 0-1" in r.stdout
      and "  #2-3 child of 2-3" in r.stdout
      and "aaaa" not in r.stdout
      and not any(l.startswith("  #0 ") for l in r.stdout.splitlines()),
      "an over-budget raw block did not fall back to child summaries:\n"
      + r.stdout[:1200])
check(raw_lines == [], "the big raw prompt still listed raw memories")
check("at most 560 bytes" in r.stdout,
      "the fallback prompt lost the v2 summary cap: " + r.stdout[:200])
check(len(r.stdout) < CAP_CHARS and len(r.stdout.splitlines()) < CAP_LINES,
      "the fallback prompt exceeds a harness cap: %d chars, %d lines"
      % (len(r.stdout), len(r.stdout.splitlines())))
# PART_CHARS is a transport budget: a 560-byte summary plus its id must not
# be split or rejected just because an unrelated paging knob is tiny.
run("config", "PART_CHARS=8", store=draw)
r = run("nap", "0-3", "s" * 560, "0", store=draw)
check(r.returncode == 0 and "0-3 saved" in r.stdout,
      "PART_CHARS interfered with a 560-byte summary: " + r.stdout + r.stderr)

# Sixteen small memories fit both RAW_MAX and RAW_BYTES: the prompt keeps raw
# lines, and tombstones are omitted rather than printed as empty lines.
draw2 = os.path.join(tempfile.mkdtemp(prefix="optmem-raw16-"), "memory")
run_init(draw2)
for i in range(16):
    run("note", "raw sixteen %02d" % i, store=draw2)
settle(draw2)
run("delete", "5", store=draw2)
r = run("nap", store=draw2)
raw_lines = [l for l in r.stdout.splitlines() if re.match(r"  #\d+ ", l)]
check("Compress memories #4-5 " in r.stdout and "#4 " in r.stdout
      and not any(l.startswith("  #5 ") for l in raw_lines)
      and "\x00" not in r.stdout,
      "a raw prompt printed a tombstone or NUL: " + r.stdout[:600])
check("at most 560 bytes" in r.stdout,
      "the raw prompt lost the v2 summary cap: " + r.stdout[:300])

# ---- explicit migration ----------------------------------------------

dm = make_legacy_migration("optmem-migrate-")
source = data_files(dm)
fmt_load(dm)
old_records = list(cli.log_scan(dm))
old_rows, old_rev = tree_rows(dm), cli.revision(dm)
check(old_rev == 2 and old_rows == {2: ["settled", cli.EMPTY_SUMMARY],
                                   4: ["settled"], 8: [""]},
      "the migration fixture changed: rev=%r tree=%r" % (old_rev, old_rows))
r = crash_after_journal(dm)
check(r.returncode == 1, "the migration journal crash did not happen")
check(data_files(dm) == source, "staging changed a source file")
check(os.path.exists(os.path.join(dm, ".migration"))
      and os.path.isdir(os.path.join(dm, ".migrate")),
      "the journal crash left no journal or staging directory")
check(not os.path.exists(os.path.join(dm, ".format")),
      "the journal crash installed the format before the journal")
journal = json.load(open(os.path.join(dm, ".migration")))
check(journal.get("version") == 1 and journal.get("from") == 1
      and journal.get("to") == 2 and journal.get("revision") == old_rev + 1,
      "the migration journal header is wrong: %r" % (journal,))
check([f["path"] for f in journal["files"]]
      == ["LOG.txt", "TREE/2", "TREE/4", "TREE/8"],
      "the migration journal does not name LOG and every TREE level: %r"
      % (journal["files"],))
for item in journal["files"]:
    staged = os.path.join(dm, ".migrate", item["path"])
    check(os.path.getsize(staged) == item["bytes"],
          "staged %s has the wrong byte count: %r" % (item["path"], item))
    check(hashlib.sha256(open(staged, "rb").read()).hexdigest()
          == item["sha256"], "staged %s has the wrong digest" % item["path"])
check(os.stat(os.path.join(dm, ".migrate", "LOG.txt")).st_mode & 0o777 == 0o600
      and os.stat(os.path.join(dm, ".migrate", "TREE", "2")).st_mode & 0o777 == 0o640,
      "staging did not preserve file modes")

# The log alone can be installed first: its v2 length is a multiple of the old
# 320-byte width, so only .format can tell the two apart. Recovery must not
# mistake an installed log for an unfinished one.
os.replace(os.path.join(dm, ".migrate", "LOG.txt"),
           os.path.join(dm, "LOG.txt"))
r = run("config", store=dm)
check(r.returncode == 0, "recovery after a log-only install failed: " + r.stderr)
check(not os.path.exists(os.path.join(dm, ".migrate"))
      and not os.path.exists(os.path.join(dm, ".migration")),
      "recovery left staging or the journal behind")
check(json.load(open(os.path.join(dm, ".format")))["version"] == 2,
      "recovery did not install .format 2")
check(cli.revision(dm) == old_rev + 1, "migration did not bump the revision once")

# Meaning, not bytes: distinct dates and ids, tombstones, whitespace, a #,
# UTF-8, NUL-only summaries. The torn tails must be dropped, not copied.
fmt_load(dm)
check(cli.LOG_REC == 2112 and cli.TREE_REC == 576,
      "the migrated widths are not 2112/576")
new_records = [cli.log_get(dm, i) for i in range(cli.log_len(dm))]
check(len(new_records) == len(old_records),
      "migration lost a memory slot: %d old, %d new: %r vs %r"
      % (len(old_records), len(new_records), old_records, new_records))
for i, (old, new) in enumerate(zip(old_records, new_records)):
    check(old == new, "migration changed memory %d: %r -> %r" % (i, old, new))
check(new_records[0][1] != new_records[1][1],
      "the fixture lost its distinct dates")
check(new_records[2][1] == "2020-03-03" and new_records[2][2] == ""
      and new_records[3][1] == "2020-04-04" and new_records[3][2] == "",
      "migration lost a tombstone or its date")
check(new_records[1][2] == "one  spaces #tag café →"
      and new_records[4][2] == "four reunião ação",
      "migration changed whitespace, #, or UTF-8 text")
check(tree_rows(dm) == old_rows,
      "migration changed the logical summaries or the NUL marker")
check(os.path.getsize(os.path.join(dm, "LOG.txt")) == 5 * 2112,
      "the migrated log still contains its torn tail")
check(os.path.getsize(os.path.join(dm, "TREE", "2")) == 2 * 576,
      "the migrated summary level still contains its torn tail")
check(os.path.getsize(os.path.join(dm, "TREE", "8")) == 576
      and open(os.path.join(dm, "TREE", "8"), "rb").read().rstrip() == b"",
      "the migrated blank summary is not one blank 576-byte record")
check(open(os.path.join(dm, "TREE", "2"), "rb").read()[
      cli.TREE_REC:2 * cli.TREE_REC].rstrip()
      == cli.EMPTY_SUMMARY.encode(),
      "the migrated NUL-only summary lost its marker")
check(open(os.path.join(dm, "config"), "rb").read()
      == source["config"], "migration rewrote the config")
r = run("config", store=dm)
check("ENTRY_CHARS   200" in r.stdout and "SUMMARY_CHARS 100" in r.stdout
      and "WAKE_LINES    1" in r.stdout,
      "migration lost the store's overrides: " + r.stdout)
r = run("wake", store=dm)
check(r.returncode == 0 and "#0-3 settled" in r.stdout
      and "four reunião ação" in r.stdout and "tomb target" not in r.stdout,
      "wake after migration is wrong:\n" + r.stdout + r.stderr)
check(json.load(open(os.path.join(dm, ".format")))["version"] == 2,
      "migration did not leave format 2")

# Migrating again is a no-op: no rewrite, no revision bump.
snap, snap_rev = data_files(dm), cli.revision(dm)
r = run("migrate", store=dm)
check(r.returncode == 0 and "Already format 2" in r.stdout,
      "a second migrate did not say it was a no-op: " + r.stdout)
check(data_files(dm) == snap and cli.revision(dm) == snap_rev,
      "a second migrate changed the store")

# The migration invalidates old nap and wake revisions exactly once.
for args in (("nap", "0-1", "stale", str(old_rev)),
             ("wake", "2", "5", str(old_rev))):
    r = run(*args, store=dm)
    check(r.returncode == 1 and "revision is missing" in r.stderr,
          "a stale revision survived the migration: " + r.stderr)
r = run("nap", "0-1", "again", str(cli.revision(dm)), store=dm)
check(r.returncode == 0 and ("already settled" in r.stdout
                             or "Nothing left" in r.stdout),
      "a current revision failed after migration: "
      + r.stdout + r.stderr)

# A crash after some files were renamed must roll forward, including a source
# that is no longer staged. Remove one installed target to prove the other
# staged copies are still consumed in the same pass.
dm2 = make_legacy_migration("optmem-migrate-mid-")
fmt_load(dm2)
old2 = list(cli.log_scan(dm2))
r = crash_after_journal(dm2)
check(r.returncode == 1, "the mid-tree migration fixture did not crash")
os.replace(os.path.join(dm2, ".migrate", "LOG.txt"),
           os.path.join(dm2, "LOG.txt"))
os.replace(os.path.join(dm2, ".migrate", "TREE", "2"),
           os.path.join(dm2, "TREE", "2"))
r = run("config", store=dm2)
check(r.returncode == 0, "mid-tree migration recovery failed: " + r.stderr)
check(not os.path.exists(os.path.join(dm2, ".migrate"))
      and not os.path.exists(os.path.join(dm2, ".migration")),
      "mid-tree recovery left staging or the journal")
check(os.path.getsize(os.path.join(dm2, "LOG.txt")) == 5 * 2112
      and os.path.getsize(os.path.join(dm2, "TREE", "2")) == 2 * 576
      and os.path.getsize(os.path.join(dm2, "TREE", "4")) == 576,
      "mid-tree recovery did not install every v2 file")
fmt_load(dm2)
check([cli.log_get(dm2, i) for i in range(cli.log_len(dm2))] == old2,
      "mid-tree recovery changed the log")


# A journal without a valid manifest, or without the files it names, must stop
# before touching the source. Build a real journal through a staging crash,
# then mutate only the journal or only the stage.
dj = make_legacy_migration("optmem-migrate-journal-")
sourcej = data_files(dj)
r = crash_after_journal(dj)
check(r.returncode == 1 and os.path.exists(os.path.join(dj, ".migration")),
      "the bad-journal fixture did not leave a journal")
real_journal = open(os.path.join(dj, ".migration"), "rb").read()
base_journal = json.loads(real_journal)

def journal_rejected(label, raw=None, mutate=None):
    if mutate is not None:
        change = json.loads(json.dumps(base_journal))
        mutate(change)
        raw = json.dumps(change)
    with open(os.path.join(dj, ".migration"), "w") as f:
        f.write(raw)
    r = run("config", store=dj)
    check(r.returncode == 1 and "Restore" in r.stderr
          and ".migration" in r.stderr,
          "a %s journal was not rejected actionably: %s" % (label, r.stderr))
    check(data_files(dj) == sourcej,
          "a %s journal changed the source before rejecting it" % label)
    check(not os.path.exists(os.path.join(dj, ".format")),
          "a %s journal installed the format before rejecting it" % label)
    check(open(os.path.join(dj, "LOG.txt"), "rb").read().endswith(
        b"torn tail"), "a %s journal accepted a torn source tail" % label)

journal_rejected("invalid JSON", raw="{bad")
journal_rejected("non-object", raw="[]")
journal_rejected("wrong version", mutate=lambda c: c.update(version=2))
journal_rejected("boolean version", mutate=lambda c: c.update(version=True))
journal_rejected("missing level", mutate=lambda c: c.update(
    files=[x for x in c["files"] if x["path"] != "TREE/2"]))
journal_rejected("wrong source", mutate=lambda c: c.update(**{"from": 2}))
journal_rejected("empty files", mutate=lambda c: c.update(files=[]))
journal_rejected("no LOG", mutate=lambda c: c.update(
    files=[x for x in c["files"] if x["path"] != "LOG.txt"]))
journal_rejected("duplicate path", mutate=lambda c: c["files"].append(
    dict(c["files"][0])))
journal_rejected("bad level", mutate=lambda c: c["files"].append({
    "path": "TREE/3", "bytes": 0,
    "sha256": hashlib.sha256(b"").hexdigest()}))
journal_rejected("path escape", mutate=lambda c: c["files"][0].update(
    path="../LOG.txt"))
journal_rejected("odd bytes", mutate=lambda c: c["files"][0].update(
    bytes=c["files"][0]["bytes"] + 1))
journal_rejected("short digest", mutate=lambda c: c["files"][0].update(
    sha256="0" * 63))
journal_rejected("unexpected revision", mutate=lambda c: c.update(
    revision=99))

with open(os.path.join(dj, ".migration"), "wb") as f:
    f.write(real_journal)
staged_log = os.path.join(dj, ".migrate", "LOG.txt")
os.unlink(staged_log)
r = run("config", store=dj)
check(r.returncode == 1 and "missing or corrupt" in r.stderr,
      "a missing staged log was not rejected: " + r.stderr)
check(data_files(dj) == sourcej, "a missing staged log changed the source")
with open(staged_log, "wb") as f:
    f.write(b"x" * 10)
r = run("config", store=dj)
check(r.returncode == 1 and "missing or corrupt" in r.stderr,
      "a short staged log was not rejected: " + r.stderr)
with open(staged_log, "wb") as f:
    f.write(b"y" * base_journal["files"][0]["bytes"])
r = run("config", store=dj)
check(r.returncode == 1 and "missing or corrupt" in r.stderr,
      "a staged log with a bad digest was not rejected: " + r.stderr)
check(data_files(dj) == sourcej, "a bad stage changed the source")

# A concurrent .pending and .migration cannot both describe the log.
with open(os.path.join(dj, ".migration"), "wb") as f:
    f.write(real_journal)
with open(os.path.join(dj, ".pending"), "w") as f:
    f.write("{}")
r = run("config", store=dj)
check(r.returncode == 1 and "Both .migration and .pending" in r.stderr,
      "a concurrent pending change was not rejected: " + r.stderr)
os.unlink(os.path.join(dj, ".pending"))
os.unlink(os.path.join(dj, ".migration"))
shutil.rmtree(os.path.join(dj, ".migrate"))

# A corrupt or unknown .format is fail-closed, never reinterpreted as legacy.
with open(os.path.join(dj, ".format"), "w") as f:
    f.write("{bad")
r = run("config", store=dj)
check(r.returncode == 1 and ".format is corrupt" in r.stderr,
      "a corrupt .format was not rejected: " + r.stderr)
with open(os.path.join(dj, ".format"), "w") as f:
    json.dump({"version": 3}, f)
r = run("config", store=dj)
check(r.returncode == 1 and "unsupported format" in r.stderr,
      "an unknown .format version was not rejected: " + r.stderr)
with open(os.path.join(dj, ".format"), "w") as f:
    json.dump({"version": "2"}, f)
r = run("config", store=dj)
check(r.returncode == 1 and "unsupported format" in r.stderr,
      "a string .format version was not rejected: " + r.stderr)
os.unlink(os.path.join(dj, ".format"))

# Staging that dies before the journal leaves no journal; the next attempt
# discards the stale stage and starts clean. Exceptions propagate as OSError.
dstage = tempfile.mkdtemp(prefix="optmem-migrate-stage-")
for i in range(3):
    run("note", "stage source %d" % i, store=dstage)
check(run("note", "stage source 3", store=dstage).returncode == 0,
      "the staging crash fixture could not fill its tree")
for bid, text in (("0-1", "stage one"), ("2-3", "stage two")):
    check(run("nap", bid, text, "0", store=dstage).returncode == 0,
          "the staging crash fixture could not summarize")
os.makedirs(os.path.join(dstage, ".migrate", "TREE"))
with open(os.path.join(dstage, ".migrate", "LOG.txt"), "wb") as f:
    f.write(b"stale")
source_stage = data_files(dstage)
original_stage = cli.stage_records

def stage_then_fail(source, dest, old, new, log=False):
    result = original_stage(source, dest, old, new, log=log)
    if source.endswith("TREE" + os.sep + "2"):
        raise OSError(errno.EIO, "simulated staging failure")
    return result

with patched(cli, "stage_records", stage_then_fail):
    r = run_crash("migrate", store=dstage)
check(r.returncode == 1, "the staging failure did not propagate")
check(data_files(dstage) == source_stage,
      "a staging failure changed the source")
check(not os.path.exists(os.path.join(dstage, ".migration")),
      "a staging failure wrote the journal before all files were staged")
r = run("migrate", store=dstage)
check(r.returncode == 0 and "Migrated" in r.stdout,
      "the retry after a staging failure did not migrate: "
      + r.stdout + r.stderr)
check(not os.path.exists(os.path.join(dstage, ".migrate"))
      and "stale" not in open(os.path.join(dstage, "LOG.txt"), "rb").read().decode("utf-8", "ignore"),
      "the retry kept a stale stage")

# Migration must recover after each commit boundary, not only file renames.
for boundary in (".format", ".revision", "cleanup"):
    dcmt = tempfile.mkdtemp(prefix="optmem-migrate-commit-")
    for i in range(3):
        run("note", "commit source %d" % i, store=dcmt)
    settle(dcmt)
    expected = list(cli.log_scan(dcmt))
    real_atomic = cli.atomic_write
    real_rmtree = cli.shutil.rmtree

    def fail_after_metadata(path, data):
        result = real_atomic(path, data)
        if os.path.basename(path) == boundary:
            raise OSError(errno.EIO, "simulated migration metadata interruption")
        return result

    def fail_after_cleanup(path, *args, **kwargs):
        result = real_rmtree(path, *args, **kwargs)
        if os.path.basename(path) == ".migrate":
            raise OSError(errno.EIO, "simulated migration cleanup interruption")
        return result

    with patched(cli, "atomic_write", fail_after_metadata), \
            patched(cli.shutil, "rmtree", fail_after_cleanup if boundary == "cleanup"
                    else real_rmtree):
        r = run_crash("migrate", store=dcmt)
    check(r.returncode == 1 and os.path.exists(os.path.join(dcmt, ".migration")),
          "migration did not retain its journal after %s" % boundary)
    r = run_init(dcmt)
    check(r.returncode == 0 and cli.read_format(dcmt) == 2,
          "init did not recover migration after %s: %s" % (boundary, r.stderr))
    check(cli.revision(dcmt) == 1 and list(cli.log_scan(dcmt)) == expected,
          "migration recovery lost records or bumped twice after %s" % boundary)
    check(not os.path.exists(os.path.join(dcmt, ".migration"))
          and not os.path.exists(os.path.join(dcmt, ".migrate")),
          "migration recovery left files after %s" % boundary)
    snap = fingerprint(dcmt)
    check(run("migrate", store=dcmt).returncode == 0 and fingerprint(dcmt) == snap,
          "a second migration changed a recovered store after %s" % boundary)
    shutil.rmtree(dcmt)

# An existing valid stage wins even if a target already has the expected size.
dprefer = tempfile.mkdtemp(prefix="optmem-migrate-stage-preference-")
run("note", "staged content survives", store=dprefer)
check(crash_after_journal(dprefer).returncode == 1,
      "the stage-preference fixture did not interrupt")
with open(os.path.join(dprefer, "LOG.txt"), "wb") as f:
    f.write(b"x" * 2112)
r = run("config", store=dprefer)
check(r.returncode == 0 and cli.log_get(dprefer, 0)[2] == "staged content survives",
      "migration trusted an equal-size target instead of its valid stage")
shutil.rmtree(dprefer)

# Invalid UTF-8 in a source record, or a corrupt summary, must leave every
# source byte intact. Torn tails are dropped only in staged copies.
dbad_utf = tempfile.mkdtemp(prefix="optmem-migrate-utf8-")
os.makedirs(os.path.join(dbad_utf, "TREE"))
with open(os.path.join(dbad_utf, "LOG.txt"), "wb") as f:
    bad = b"#0 2020-01-01 bad \xff\xfe"
    f.write(bad + b" " * (320 - 1 - len(bad)) + b"\n")
bad_utf_source = open(os.path.join(dbad_utf, "LOG.txt"), "rb").read()
r = run("migrate", store=dbad_utf)
check(r.returncode == 1 and "record 0 is corrupt" in r.stderr,
      "invalid UTF-8 was not rejected: " + r.stderr)
check(open(os.path.join(dbad_utf, "LOG.txt"), "rb").read() == bad_utf_source
      and not os.path.exists(os.path.join(dbad_utf, ".migration")),
      "an invalid UTF-8 migration touched its source")
dbad_sum = tempfile.mkdtemp(prefix="optmem-migrate-sum-")
os.makedirs(os.path.join(dbad_sum, "TREE"))
open(os.path.join(dbad_sum, "LOG.txt"), "wb").close()
with open(os.path.join(dbad_sum, "TREE", "2"), "wb") as f:
    f.write(b"\xff" * 288)
bad_sum_source = open(os.path.join(dbad_sum, "TREE", "2"), "rb").read()
r = run("migrate", store=dbad_sum)
check(r.returncode == 1 and "forget" in r.stderr,
      "a corrupt summary was not rejected with its exit: " + r.stderr)
check(open(os.path.join(dbad_sum, "TREE", "2"), "rb").read()
      == bad_sum_source, "a corrupt-summary migration touched its source")

# A pending edit is replayed before migration, so the staged copy includes it.
dpend = tempfile.mkdtemp(prefix="optmem-migrate-pending-")
for i in range(4):
    run("note", "pending base %d" % i, store=dpend)
pending_date = cli.log_get(dpend, 1)[1]
settle(dpend)
original_atomic = cli.atomic_write

def atomic_after_revision(path, data):
    result = original_atomic(path, data)
    if os.path.basename(path) == ".revision":
        raise OSError(errno.EIO, "simulated crash after revision")
    return result

with patched(cli, "atomic_write", atomic_after_revision):
    r = run_crash("edit", "1", "pending edited one", store=dpend)
check(r.returncode == 1 and os.path.exists(os.path.join(dpend, ".pending")),
      "the pending-edit fixture lost its journal")
r = run("migrate", store=dpend)
check(r.returncode == 0 and not os.path.exists(os.path.join(dpend, ".pending")),
      "migrate did not recover the pending edit first: " + r.stderr)
fmt_load(dpend)
check(cli.log_get(dpend, 1) == (1, pending_date, "pending edited one"),
      "migration lost the pending edit or its date: %r"
      % (cli.log_get(dpend, 1),))
check(cli.log_get(dpend, 0)[2] == "pending base 0",
      "migration damaged an unrelated memory")
check(json.load(open(os.path.join(dpend, ".format")))["version"] == 2,
      "the pending-edit migration did not finish")

# Real processes: migrate races note, edit, and another init. The lock makes
# one win at a time, and nothing may time out or lose a record.
drace = tempfile.mkdtemp(prefix="optmem-migrate-race-")
envrace = dict(os.environ, MEMORY_DIR=drace)
for i in range(6):
    p = subprocess.run(memo + ["note", "migrate race base %d" % i],
                       env=envrace, capture_output=True, text=True, timeout=30)
    check(p.returncode == 0, "the migration race could not seed: " + p.stderr)
procs = [subprocess.Popen(memo + ["migrate"], env=envrace,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)]
procs += [subprocess.Popen(memo + ["note", "migrate race new %d" % i],
                           env=envrace, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE) for i in range(3)]
procs += [subprocess.Popen(memo + ["edit", str(i), "migrate race edit %d" % i],
                           env=envrace, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE) for i in range(2)]
procs += [subprocess.Popen(memo + ["init"], env=envrace,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)]
for p in procs:
    try:
        _, err = p.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        p.kill()
        _, err = p.communicate()
        check(False, "a migrate/note/edit/init race hung")
    check(p.returncode == 0,
          "a migrate/note/edit/init race failed: %s" % (err or b""))
check(json.load(open(os.path.join(drace, ".format")))["version"] == 2,
      "the migration race left the store at format 1")
check(not os.path.exists(os.path.join(drace, ".migrate"))
      and not os.path.exists(os.path.join(drace, ".migration")),
      "the migration race left staging or the journal")
fmt_load(drace)
check(cli.log_len(drace) == 9, "the migration race lost a memory: %d"
      % cli.log_len(drace))
race_recs = [cli.log_get(drace, i) for i in range(cli.log_len(drace))]
check([e[2] for e in race_recs[:2]] == ["migrate race edit 0",
                                        "migrate race edit 1"],
      "the migration race lost an edit: %r" % (race_recs[:2],))
check(sorted(e[2] for e in race_recs[6:]) == ["migrate race new %d" % i
                                              for i in range(3)],
      "the migration race lost a note: %r" % (race_recs[6:],))
# Migration contributes one revision; each of the two edits contributes one.
check(cli.revision(drace) == 3, "the migration race lost a revision: %d"
      % cli.revision(drace))


de = tempfile.mkdtemp(prefix="optmem-edit-")
seed = os.path.join(de, "seed.txt")
with open(seed, "w") as f:
    f.write("2020-01-01 old zero\n2020-01-02 old one\n"
            "2020-01-03 old two\n2020-01-04 old three\n")
r = run("import", seed, store=de)
check(r.returncode == 0 and "Imported 4 memories" in r.stdout,
      "edit fixture import failed: " + r.stdout + r.stderr)

before = fingerprint(de)
for args, wanted in (
        (("edit", "x", "text"), "not a memory id"),
        (("edit", "#99", "text"), "No memory at"),
        (("edit", "0"), "usage"),
        (("delete", "x"), "not a memory id"),
        (("delete", "99"), "No memory at"),
        (("delete",), "usage"),
        (("edit", "0", "   "), "Empty"),
        (("edit", "0", "a\nb"), "one line"),
        (("edit", "0", "a\x00b"), "NUL"),
        (("edit", "0", "x" * 281), "Too long")):
    r = run(*args, store=de)
    check(r.returncode == 1 and wanted in r.stderr,
          "edit/delete validation missed %r: %s" % (args, r.stderr))
check(fingerprint(de) == before and cli.revision(de) == 0,
      "a rejected edit or delete mutated the store")

r = run("edit", "#1", "replaced caf\u00e9", store=de)
check(r.returncode == 0 and "id and date are unchanged" in r.stdout,
      "edit failed: " + r.stdout + r.stderr)
i, date, text = cli.log_get(de, 1)
check((i, date, text) == (1, "2020-01-02", "replaced caf\u00e9"),
      "edit changed the id or date: %r" % ((i, date, text),))
log = open(os.path.join(de, "LOG.txt"), "rb").read()
check(os.path.getsize(os.path.join(de, "LOG.txt")) == 4 * cli.LOG_REC,
      "edit changed the log length")
check(b"old one" not in log and b"replaced caf\xc3\xa9" in log,
      "edit left the old text or lost the new one")
check(cli.revision(de) == 1, "edit did not bump the revision")
r = run("recall", "old one", store=de)
check("No match." in r.stdout, "recall found an edited-away text")
r = run("recall", "replaced", store=de)
check("#1 2020-01-02 replaced caf\u00e9" in r.stdout,
      "recall missed the edited text: " + r.stdout)

snap = fingerprint(de)
r = run("edit", "3", "old three", store=de)
check(r.returncode == 0 and "unchanged" in r.stdout,
      "a no-op edit was not reported: " + r.stdout + r.stderr)
check(fingerprint(de) == snap and cli.revision(de) == 1,
      "a no-op edit changed the revision or the tree")

r = run("delete", "2", store=de)
check(r.returncode == 0 and "Deleted #2" in r.stdout,
      "delete failed: " + r.stdout + r.stderr)
i, date, text = cli.log_get(de, 2)
check((i, date, text) == (2, "2020-01-03", ""),
      "delete did not leave a dated tombstone: %r" % ((i, date, text),))
log = open(os.path.join(de, "LOG.txt"), "rb").read()
check(os.path.getsize(os.path.join(de, "LOG.txt")) == 4 * cli.LOG_REC,
      "delete changed the log length")
check(b"old two" not in log, "delete left the old text")
check(cli.revision(de) == 2, "delete did not bump the revision")

snap = fingerprint(de)
r = run("delete", "2", store=de)
check(r.returncode == 0 and "already deleted" in r.stdout,
      "a no-op delete was not reported: " + r.stdout + r.stderr)
r = run("edit", "2", "revive", store=de)
check(r.returncode == 1 and "was deleted" in r.stderr,
      "edit revived a deleted memory: " + r.stderr)
check(fingerprint(de) == snap and cli.revision(de) == 2,
      "a no-op delete or a deleted edit mutated the store")

r = run("note", "fresh one", store=de)
check(r.returncode == 0 and "Saved as #4" in r.stdout,
      "the id after a delete did not stay stable: " + r.stdout + r.stderr)
check(cli.log_get(de, 4)[2] == "fresh one", "the appended memory reads wrong")
r = run("wake", store=de)
check(r.returncode == 0 and "#2 " not in r.stdout
      and "#1 2020-01-02 replaced caf\u00e9" in r.stdout,
      "wake included a deleted memory or lost an edited one:\n" + r.stdout)
r = run("zoom", "2-3", store=de)
check(r.returncode == 0 and "#2 " not in r.stdout and "#3 " in r.stdout,
      "zoom included a deleted memory: " + r.stdout + r.stderr)
r = run("recall", "old two", store=de)
check("No match." in r.stdout, "recall found a deleted memory")
shutil.rmtree(de)

# The tombstone keeps its date, or a later import would be out of order.
di = tempfile.mkdtemp(prefix="optmem-import-")
seed = os.path.join(di, "seed.txt")
with open(seed, "w") as f:
    f.write("2020-01-01 first\n2020-01-02 second\n2020-01-03 third\n")
check(run("import", seed, store=di).returncode == 0, "import fixture failed")
check(run("delete", "2", store=di).returncode == 0, "import tombstone delete failed")
check(cli.log_get(di, 2)[1] == "2020-01-03", "delete changed the tombstone date")
bad = os.path.join(di, "bad.txt")
with open(bad, "w") as f:
    f.write("2020-01-01 before the tombstone\n")
r = run("import", bad, store=di)
check(r.returncode == 1 and "precedes the previous memory (2020-01-03)"
      in r.stderr, "import ignored the tombstone date: " + r.stderr)
good = os.path.join(di, "good.txt")
with open(good, "w") as f:
    f.write("2020-01-03 same day\n2020-01-04 next day\n")
r = run("import", good, store=di)
check(r.returncode == 0 and "Imported 2 memories" in r.stdout,
      "import after a tombstone failed: " + r.stdout + r.stderr)
check(cli.log_get(di, 2)[1] == "2020-01-03"
      and cli.log_get(di, 3)[1] == "2020-01-03"
      and cli.log_get(di, 4)[1] == "2020-01-04",
      "import order or the tombstone date changed")
shutil.rmtree(di)

# ---- revisions: stale prompts and paginated wake ---------------------

dr = tempfile.mkdtemp(prefix="optmem-revision-")
for i in range(8):
    run("note", "revision memory %d" % i, store=dr)
settle(dr)
check(cli.revision(dr) == 0, "a fresh store did not start at revision 0")
r = run("nap", "0-1", "legacy summary", store=dr)
check(r.returncode == 0, "revision 0 must allow a nap without a revision")
r = run("wake", "1", "8", store=dr)
check(r.returncode == 0, "revision 0 must allow a paginated wake without one")

check(run("edit", "6", "revision memory 6 edited", store=dr).returncode == 0,
      "revision fixture edit failed")
check(cli.revision(dr) == 1, "the fixture edit did not bump the revision")
for args in (("nap", "0-1", "attempt"), ("nap", "0-1", "attempt", "0")):
    r = run(*args, store=dr)
    check(r.returncode == 1 and "revision is missing" in r.stderr,
          "a stale or missing nap revision on a settled block was accepted: "
          + r.stdout + r.stderr)
r = run("nap", "0-1", "attempt", "1", store=dr)
check(r.returncode == 0 and "already settled" in r.stdout,
      "a current nap revision on a settled block was rejected: "
      + r.stdout + r.stderr)
for args in (("nap", "6-7", "attempt"), ("nap", "6-7", "attempt", "0")):
    r = run(*args, store=dr)
    check(r.returncode == 1 and "revision is missing" in r.stderr,
          "a stale or missing nap revision on a pending block was accepted: "
          + r.stdout + r.stderr)
r = run("nap", "6-7", "revision six and seven", "1", store=dr)
check(r.returncode == 0 and "6-7 saved" in r.stdout,
      "a current nap revision on a pending block was rejected: "
      + r.stdout + r.stderr)
check(cli.revision(dr) == 1, "a nap changed the revision")
settle(dr)

run("config", "WAKE_LINES=2", store=dr)
run("config", "PART_CHARS=8", store=dr)
r = run("wake", store=dr)
check(r.returncode == 0 and "part 1 of 2" in r.stdout,
      "the paginated fixture did not paginate: " + r.stdout + r.stderr)
for args in (("wake", "2", "8"), ("wake", "2", "8", "0")):
    r = run(*args, store=dr)
    check(r.returncode == 1 and "revision is missing" in r.stderr,
          "a stale or missing wake revision was accepted: "
          + r.stdout + r.stderr)
r = run("wake", "2", "8", "1", store=dr)
check(r.returncode == 0 and "part 2 of 2" in r.stdout,
      "a current wake revision was rejected: " + r.stdout + r.stderr)
r = run("note", "revision append after parts", store=dr)
check(r.returncode == 0 and cli.revision(dr) == 1,
      "an append changed the revision")
r = run("wake", "2", "8", "1", store=dr)
check(r.returncode == 0 and "part 2 of 2" in r.stdout,
      "an append staled an already-rendered part: " + r.stdout + r.stderr)
check(run("delete", "7", store=dr).returncode == 0,
      "the between-parts deletion failed")
snap = data_state(dr)
r = run("wake", "2", "8", "1", store=dr)
check(r.returncode == 1 and "memory changed" in r.stderr,
      "a deletion between parts did not reject the old revision")
r = run("nap", "0-1", "stale summary", "1", store=dr)
check(r.returncode == 1 and data_state(dr) == snap,
      "a stale continuation or summary changed the store")
settle(dr)
r = run("wake", "2", "8", str(cli.revision(dr)), store=dr)
check(r.returncode == 0 and "You are awake." in r.stdout,
      "a fresh revision could not finish reading after deletion")
shutil.rmtree(dr)

# ---- tree invalidation: exact prefixes at 32 memories ----------------

dt = tempfile.mkdtemp(prefix="optmem-tree32-")
for i in range(32):
    run("note", "tree record %02d old" % i, store=dt)
settle(dt)
full = {2: 16, 4: 8, 8: 4, 16: 2, 32: 1}
check(tree_counts(dt) == full, "the 32-record tree is not full: %r"
      % tree_counts(dt))
check(run("edit", "2", "tree record 02 edited", store=dt).returncode == 0,
      "tree fixture edit 2 failed")
check(cli.revision(dt) == 1, "edit 2 did not bump the revision")
check(tree_counts(dt) == {2: 1, 4: 0, 8: 0, 16: 0, 32: 0},
      "edit 2 did not drop the exact prefix: %r" % tree_counts(dt))
r = run("nap", store=dt)
offer = nap_order(r.stdout)
check(offer and offer[1] == str(cli.revision(dt)),
      "the rebuilt prompt did not carry the current revision: " + r.stdout)
settle(dt)
check(tree_counts(dt) == full, "the tree did not rebuild after edit 2")
log = open(os.path.join(dt, "LOG.txt"), "rb").read()
check(os.path.getsize(os.path.join(dt, "LOG.txt")) == 32 * cli.LOG_REC,
      "edit 2 changed the log length")
check(b"tree record 02 old" not in log and b"tree record 02 edited" in log,
      "edit 2 left the old text or lost the new one")
check("No match." in run("recall", "tree record 02 old", store=dt).stdout,
      "recall found edit 2's old text")

prefixes = {f: open(os.path.join(dt, "TREE", f), "rb").read()
            for f in os.listdir(os.path.join(dt, "TREE"))}
check(run("edit", "31", "tree record 31 edited", store=dt).returncode == 0,
      "tree fixture edit 31 failed")
check(cli.revision(dt) == 2, "edit 31 did not bump the revision")
check(tree_counts(dt) == {2: 15, 4: 7, 8: 3, 16: 1, 32: 0},
      "edit 31 did not drop the exact prefix: %r" % tree_counts(dt))
for level, n in tree_counts(dt).items():
    with open(os.path.join(dt, "TREE", str(level)), "rb") as f:
        check(f.read() == prefixes[str(level)][:n * cli.TREE_REC],
              "edit 31 changed an unaffected summary prefix at level %d" % level)
settle(dt)
check(tree_counts(dt) == full, "the tree did not rebuild after edit 31")
check(os.path.getsize(os.path.join(dt, "LOG.txt")) == 32 * cli.LOG_REC,
      "edit 31 changed the log length")
r = run("wake", store=dt)
check(r.returncode == 0 and "tree record 31 edited" in r.stdout
      and "tree record 31 old" not in r.stdout,
      "wake lost an edit or kept the old text:\n" + r.stdout)
shutil.rmtree(dt)

# ---- deleted memories in summaries and output ------------------------

dn = tempfile.mkdtemp(prefix="optmem-nul-")
for i in range(4):
    run("note", "deleted source %d" % i, store=dn)
settle(dn)
check(run("delete", "0", store=dn).returncode == 0, "NUL fixture delete 0 failed")
r = run("nap", store=dn)
offer = nap_order(r.stdout)
check(offer and offer[0] == "0-1" and "#1 " in r.stdout
      and "#0 " not in r.stdout and "\x00" not in r.stdout,
      "a block prompt did not omit its deleted source:\n" + r.stdout)
check(run("nap", offer[0], "living half summary", offer[1],
          store=dn).returncode == 0, "the mixed block nap failed")
check(run("delete", "1", store=dn).returncode == 0, "NUL fixture delete 1 failed")
raw2 = open(os.path.join(dn, "TREE", "2"), "rb").read()
check(raw2[:cli.TREE_REC].rstrip() == b"\x00",
      "a deleted-only block did not get an internal NUL summary")
check("\x00" not in run("wake", store=dn).stdout,
      "wake printed an internal NUL")
r = run("nap", store=dn)
offer = nap_order(r.stdout)
check(r.returncode == 0 and offer and offer[0] == "2-3"
      and "\x00" not in r.stdout,
      "the next prompt after a deleted-only block is wrong:\n" + r.stdout)
check(run("nap", offer[0], "right half summary", offer[1],
          store=dn).returncode == 0, "the right half nap failed")
r = run("nap", store=dn)
offer = nap_order(r.stdout)
check(offer and offer[0] == "0-3" and "#2 " in r.stdout and "#3 " in r.stdout
      and "#0 " not in r.stdout and "#1 " not in r.stdout
      and "\x00" not in r.stdout,
      "a raw block prompt included a deleted source:\n" + r.stdout)
check(run("nap", offer[0], "mixed block summary", offer[1],
          store=dn).returncode == 0, "the mixed big block nap failed")
raw4 = open(os.path.join(dn, "TREE", "4"), "rb").read()
check(b"\x00" not in raw4 and b"mixed block summary" in raw4,
      "a living big block got a NUL summary")
run("config", "WAKE_LINES=1", store=dn)
r = run("wake", store=dn)
check(r.returncode == 0 and "#0-3 mixed block summary" in r.stdout
      and "#0 " not in r.stdout and "#1 " not in r.stdout,
      "wake did not render the mixed summary correctly:\n" + r.stdout)
r = run("zoom", "0-1", store=dn)
check(r.returncode == 0 and r.stdout.strip() == "",
      "zoom printed a deleted-only block: " + r.stdout)
r = run("zoom", "0-3", store=dn)
check(r.returncode == 0 and "2-3" in r.stdout and "0-1" not in r.stdout
      and "\x00" not in r.stdout,
      "zoom printed an empty half: " + r.stdout)
shutil.rmtree(dn)

# A big block whose whole first half is deleted must omit that half.
dh = tempfile.mkdtemp(prefix="optmem-empty-half-")
for i in range(32):
    run("note", "empty half source %02d" % i, store=dh)
settle(dh)
for i in range(16):
    check(run("delete", str(i), store=dh).returncode == 0,
          "empty-half fixture delete %d failed" % i)
big = None
for _ in range(1000):
    r = run("nap", store=dh)
    offer = nap_order(r.stdout)
    if not offer:
        break
    if offer[0] == "0-31":
        big = r.stdout
        break
    check(run("nap", offer[0], "right half %s" % offer[0], offer[1],
              store=dh).returncode == 0,
          "empty-half nap %s failed" % offer[0])
check(big and "#16-31" in big and "#0-15" not in big
      and "#0 " not in big and "#1 " not in big and "\x00" not in big,
      "a big prompt did not omit its empty half:\n" + (big or ""))
check(run("nap", "0-31", "empty first half summary",
          str(cli.revision(dh)), store=dh).returncode == 0,
      "the empty-half summary was rejected")
raw = open(os.path.join(dh, "TREE", "32"), "rb").read()
check(b"empty first half summary" in raw and b"\x00" not in raw,
      "the big summary is not a normal record")
run("config", "WAKE_LINES=1", store=dh)
r = run("wake", store=dh)
check(r.returncode == 0 and "#0-31 empty first half summary" in r.stdout
      and "\x00" not in r.stdout,
      "wake did not render the empty-first-half summary:\n" + r.stdout)
r = run("zoom", "0-31", store=dh)
check(r.returncode == 0 and "16-31" in r.stdout and "0-15" not in r.stdout
      and "\x00" not in r.stdout,
      "zoom printed the empty first half: " + r.stdout)
r = run("zoom", "0-15", store=dh)
check(r.returncode == 0 and r.stdout.strip() == "",
      "zoom printed a deleted-only half: " + r.stdout)
shutil.rmtree(dh)

da = tempfile.mkdtemp(prefix="optmem-all-deleted-")
for i in range(4):
    run("note", "all deleted %d" % i, store=da)
settle(da)
for i in range(4):
    check(run("delete", str(i), store=da).returncode == 0,
          "all-deleted fixture delete %d failed" % i)
check(cli.revision(da) == 4, "four deletes did not make four revisions")
r = run("nap", store=da)
check(r.returncode == 0 and "Nothing left to compress" in r.stdout,
      "deleted-only work was left pending: " + r.stdout + r.stderr)
raw2 = open(os.path.join(da, "TREE", "2"), "rb").read()
check(all(raw2[i:i + cli.TREE_REC].rstrip() == b"\x00"
          for i in range(0, len(raw2), cli.TREE_REC)),
      "the deleted-only level 2 is not all NUL")
raw4 = open(os.path.join(da, "TREE", "4"), "rb").read()
check(raw4.rstrip() == b"\x00", "the deleted-only root is not NUL")
for args in (("wake",), ("zoom", "0-1"), ("zoom", "0-3"),
             ("recall", "all deleted")):
    r = run(*args, store=da)
    check(r.returncode == 0 and "\x00" not in r.stdout,
          "%s printed an internal NUL: %s" % (args[0], r.stdout))
check("No memories remain." in run("wake", store=da).stdout,
      "an all-deleted wake must say no memories remain")
check("No match." in run("recall", "all deleted", store=da).stdout,
      "recall found an all-deleted memory")
run("config", "WAKE_LINES=1", store=da)
check("No memories remain." in run("wake", store=da).stdout,
      "an all-deleted paginated wake is wrong")
r = run("note", "after all deleted", store=da)
check(r.returncode == 0 and "Saved as #4" in r.stdout,
      "the id after all deletes is not stable: " + r.stdout + r.stderr)
check("after all deleted" in run("wake", store=da).stdout,
      "the memory after all deletes is not visible")
shutil.rmtree(da)

# ---- crash journal: interrupted edits recover before any command -----

dc = tempfile.mkdtemp(prefix="optmem-crash-")
for i in range(4):
    run("note", "crash base %d stable" % i, store=dc)
settle(dc)
base = log_records(dc)
check(cli.revision(dc) == 0, "the crash fixture did not start at revision 0")

orig_drop = cli.tree_drop

def drop_then_fail(*args, **kwargs):
    orig_drop(*args, **kwargs)
    raise OSError(errno.EIO, "simulated crash after tree invalidation")

with patched(cli, "tree_drop", drop_then_fail):
    r = run_crash("edit", "1", "crash edited one", store=dc)
check(r.returncode == 1, "the tree-invalidation crash did not happen")
check(os.path.exists(os.path.join(dc, ".pending")),
      "the edit was not journaled before the tree crash")
with open(os.path.join(dc, ".pending"), encoding="utf-8") as f:
    journal = json.load(f)
check(journal.get("version") == 1 and journal.get("id") == 1
      and journal.get("text") == "crash edited one"
      and journal.get("revision") == 1
      and journal.get("date") == cli.parse(base[1].decode().rstrip())[1],
      "the journal does not hold version/id/date/text/revision: %r" % journal)
check(cli.revision(dc) == 0 and log_records(dc) == base,
      "the tree-invalidation crash changed the log or revision")
check(tree_counts(dc) == {2: 0, 4: 0},
      "the tree-invalidation crash did not drop the tree")
r = run("config", store=dc)
check(r.returncode == 0, "recovery after tree invalidation failed: " + r.stderr)
check(not leftovers(dc), "recovery left a journal or a temporary file")
check(cli.revision(dc) == 1, "recovery did not commit the revision")
recs = log_records(dc)
check(cli.parse(recs[1].decode().rstrip())[1]
      == cli.parse(base[1].decode().rstrip())[1]
      and cli.parse(recs[1].decode().rstrip())[2] == "crash edited one",
      "recovery did not keep edit 1's date")
check(b"crash base 1 stable" not in b"".join(recs),
      "recovery left the old text after tree invalidation")
check(all(recs[i] == base[i] for i in (0, 2, 3)),
      "recovery changed an unrelated record")
settle(dc)

orig_copy = cli.shutil.copyfileobj

def copy_then_fail(src, dst, *args, **kwargs):
    dst.write(src.read(cli.LOG_REC))
    raise OSError(errno.EIO, "simulated crash mid log copy")

with patched(cli.shutil, "copyfileobj", copy_then_fail):
    r = run_crash("edit", "2", "crash partial log", store=dc)
check(r.returncode == 1, "the mid-copy crash did not happen")
check(os.path.exists(os.path.join(dc, ".pending"))
      and os.path.exists(os.path.join(dc, ".LOG.tmp")),
      "the mid-copy crash left no journal or temp log")
check(cli.revision(dc) == 1, "the mid-copy crash committed the revision")
check(b"crash base 2 stable" in b"".join(log_records(dc)),
      "the mid-copy crash changed the real log")
r = run("config", store=dc)
check(r.returncode == 0, "recovery after a mid-copy crash failed: " + r.stderr)
check(not leftovers(dc), "mid-copy recovery left a journal or temp log")
check(cli.revision(dc) == 2
      and b"crash partial log" in b"".join(log_records(dc))
      and b"crash base 2 stable" not in b"".join(log_records(dc)),
      "mid-copy recovery did not finish the atomic replacement")
settle(dc)

orig_atomic = cli.atomic_write

def atomic_fail_after_log(path, data):
    if os.path.basename(path) == ".revision":
        raise OSError(errno.EIO, "simulated crash after log replacement")
    return orig_atomic(path, data)

before = log_records(dc)
with patched(cli, "atomic_write", atomic_fail_after_log):
    r = run_crash("edit", "2", "crash edited two", store=dc)
check(r.returncode == 1, "the log-replacement crash did not happen")
check(os.path.exists(os.path.join(dc, ".pending")),
      "the edit was not journaled before the log crash")
check(cli.revision(dc) == 2, "the log crash committed the revision")
check(b"crash edited two" in b"".join(log_records(dc)),
      "the log crash did not replace the record")
check(b"crash base 2 stable" not in b"".join(log_records(dc)),
      "the log crash left the old text")
check(not os.path.exists(os.path.join(dc, ".LOG.tmp"))
      and not os.path.exists(os.path.join(dc, ".revision.tmp")),
      "the log crash left a temporary file")
check(all(log_records(dc)[i] == before[i] for i in (0, 1, 3))
      and log_records(dc)[2] != before[2],
      "the log crash changed an unrelated record")
r = run("config", store=dc)
check(r.returncode == 0, "recovery after log replacement failed: " + r.stderr)
check(not leftovers(dc) and cli.revision(dc) == 3,
      "log recovery did not commit or clean up")
check(b"crash edited two" in b"".join(log_records(dc)),
      "log recovery lost the new text")
settle(dc)

orig_atomic = cli.atomic_write

def atomic_fail_after_revision(path, data):
    result = orig_atomic(path, data)
    if os.path.basename(path) == ".revision":
        raise OSError(errno.EIO, "simulated crash after revision write")
    return result

with patched(cli, "atomic_write", atomic_fail_after_revision):
    r = run_crash("edit", "3", "crash edited three", store=dc)
check(r.returncode == 1, "the revision-write crash did not happen")
check(os.path.exists(os.path.join(dc, ".pending")),
      "the revision-write crash lost the journal")
check(cli.revision(dc) == 4, "the revision-write crash did not commit")
check(b"crash edited three" in b"".join(log_records(dc)),
      "the revision-write crash did not replace the record")
T = cli.log_len(dc)
while True:
    todo = cli.pending(dc, T)
    if not todo:
        break
    check(cli.tree_put(dc, todo[0][0], todo[0][1], "pre-recovery rebuild"),
          "could not rebuild the tree before recovery")
tree_before = {f: open(os.path.join(dc, "TREE", f), "rb").read()
               for f in os.listdir(os.path.join(dc, "TREE"))}
log_before = open(os.path.join(dc, "LOG.txt"), "rb").read()
r = run("config", store=dc)
check(r.returncode == 0, "idempotent commit recovery failed: " + r.stderr)
check(not leftovers(dc), "idempotent commit left a journal or temp file")
check({f: open(os.path.join(dc, "TREE", f), "rb").read()
       for f in os.listdir(os.path.join(dc, "TREE"))} == tree_before,
      "idempotent commit invalidated the rebuilt tree again")
check(open(os.path.join(dc, "LOG.txt"), "rb").read() == log_before
      and cli.revision(dc) == 4,
      "idempotent commit rewrote the log or revision")

orig_unlink = cli.os.unlink

def unlink_then_fail(path, *args, **kwargs):
    orig_unlink(path, *args, **kwargs)
    raise OSError(errno.EIO, "simulated crash after journal unlink")

with patched(cli.os, "unlink", unlink_then_fail):
    r = run_crash("edit", "0", "crash edited zero", store=dc)
check(r.returncode == 1, "the journal-unlink crash did not happen")
check(not os.path.exists(os.path.join(dc, ".pending")),
      "the journal-unlink crash left a journal")
check(cli.revision(dc) == 5 and b"crash edited zero" in b"".join(log_records(dc)),
      "the journal-unlink crash did not commit")
check(not leftovers(dc), "the journal-unlink crash left a temporary file")
snap = data_state(dc)
r = run("config", store=dc)
check(r.returncode == 0 and data_state(dc) == snap,
      "a command after a completed commit changed the store")

with patched(cli, "atomic_write", atomic_fail_after_log):
    r = run_crash("delete", "1", store=dc)
check(r.returncode == 1 and os.path.exists(os.path.join(dc, ".pending")),
      "an interrupted deletion lost its recovery journal")
r = run("init", store=dc)
check(r.returncode == 0 and cli.log_get(dc, 1)[2] == ""
      and cli.revision(dc) == 6 and not leftovers(dc),
      "init did not recover an interrupted deletion")
check(b"crash edited one" not in b"".join(log_records(dc)),
      "recovery of deletion retained the deleted text")

with patched(cli, "tree_drop", drop_then_fail):
    r = run_crash("edit", "2", "journal replay ignores new limits", store=dc)
check(r.returncode == 1, "the pending-change limit fixture did not interrupt")
with open(os.path.join(dc, "config"), "w") as f:
    f.write("ENTRY_CHARS=1\n")
r = run("config", store=dc)
check(r.returncode == 0 and cli.log_get(dc, 2)[2]
      == "journal replay ignores new limits" and not leftovers(dc),
      "a lower size limit blocked journal replay")
shutil.rmtree(dc)

# A bad journal is refused before it can touch the log, tree or revision.
dbad = tempfile.mkdtemp(prefix="optmem-bad-journal-")
for i in range(2):
    run("note", "journal base %d" % i, store=dbad)
settle(dbad)
check(run("edit", "0", "journal edited zero", store=dbad).returncode == 0,
      "bad-journal fixture edit failed")
settle(dbad)
state = data_state(dbad)
rev = cli.revision(dbad)
check(rev == 1, "the bad-journal fixture revision is wrong")
base_journal = {"version": 1, "id": 0, "date": "2020-01-01",
                "text": "journal text", "revision": rev + 1}
for label, raw in (
        ("invalid JSON", "{bad"),
        ("non-object", "[]"),
        ("out-of-range id", json.dumps(dict(base_journal, id=99))),
        ("NUL text", json.dumps(dict(base_journal, text="a\x00b"))),
        ("wrong version", json.dumps(dict(base_journal, version=2))),
        ("missing field", json.dumps({k: v for k, v in base_journal.items()
                                      if k != "text"})),
        ("bad date", json.dumps(dict(base_journal, date="2020-1-1"))),
        ("two lines", json.dumps(dict(base_journal, text="a\nb"))),
        ("long text", json.dumps(dict(base_journal, text="x" * 400))),
        ("future revision", json.dumps(dict(base_journal, revision=rev + 2)))):
    with open(os.path.join(dbad, ".pending"), "w") as f:
        f.write(raw)
    r = run("config", store=dbad)
    check(r.returncode == 1 and ".pending" in r.stderr
          and ("corrupt" in r.stderr or "unexpected revision" in r.stderr)
          and "Restore it from a backup" in r.stderr,
          "a %s journal was not reported actionably: %s"
          % (label, r.stderr))
    check(data_state(dbad) == state,
          "a %s journal changed the log, tree or revision" % label)
    check(not os.path.exists(os.path.join(dbad, ".LOG.tmp"))
          and not os.path.exists(os.path.join(dbad, ".revision.tmp")),
          "a %s journal wrote a temporary file" % label)
    os.unlink(os.path.join(dbad, ".pending"))
check(data_state(dbad) == state, "bad journals mutated the store")
with open(os.path.join(dbad, ".pending"), "w") as f:
    f.write(json.dumps(dict(base_journal, revision=rev)))
r = run("config", store=dbad)
check(r.returncode == 0 and not leftovers(dbad) and data_state(dbad) == state,
      "a journal whose revision already committed was not discarded cleanly")
shutil.rmtree(dbad)

# ---- locking across notes, edits, deletes and wakes ------------------
dp = tempfile.mkdtemp(prefix="optmem-mixed-race-")
envp = dict(os.environ, MEMORY_DIR=dp)
for i in range(12):
    p = subprocess.run(memo + ["note", "lock base %d" % i], env=envp,
                       capture_output=True, text=True, timeout=30)
    check(p.returncode == 0, "mixed-race seed note failed: " + p.stderr)
procs = []
procs += [subprocess.Popen(memo + ["note", "lock new %d" % i], env=envp,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
          for i in range(4)]
procs += [subprocess.Popen(memo + ["edit", str(i), "lock edited %d" % i],
                           env=envp, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE) for i in range(4)]
procs += [subprocess.Popen(memo + ["delete", str(4 + i)], env=envp,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
          for i in range(4)]
procs += [subprocess.Popen(memo + ["wake"], env=envp,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
          for _ in range(4)]
for p in procs:
    try:
        _, err = p.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        p.kill()
        _, err = p.communicate()
        check(False, "a parallel edit/delete/wake hung")
    check(p.returncode == 0,
          "a parallel edit/delete/wake failed: %s" % (err or b""))
check(cli.revision(dp) == 8, "parallel edits and deletes lost a revision: %d"
      % cli.revision(dp))
check(cli.log_len(dp) == 16, "parallel notes lost a record: %d"
      % cli.log_len(dp))
recs = log_records(dp)
ids = [cli.parse(rec.decode().rstrip())[0] for rec in recs]
check(ids == list(range(16)), "parallel ids are not dense: %r" % ids)
check([cli.parse(recs[i].decode().rstrip())[2] for i in range(4)]
      == ["lock edited %d" % i for i in range(4)],
      "a parallel edit was lost")
check(all(cli.parse(recs[i].decode().rstrip())[2] == "" for i in range(4, 8)),
      "a parallel delete was lost")
check(all(cli.parse(recs[i].decode().rstrip())[2] == "lock base %d" % i
          for i in range(8, 12)),
      "a parallel edit/delete damaged an unrelated record")
check(sorted(cli.parse(recs[i].decode().rstrip())[2] for i in range(12, 16))
      == ["lock new %d" % i for i in range(4)],
      "a parallel note is missing")
check(not leftovers(dp), "parallel commands left a journal or temp file")
r = run("wake", store=dp)
check(r.returncode == 0 and "lock edited 0" in r.stdout
      and "lock base 4" not in r.stdout and "lock new 3" in r.stdout,
      "final wake after parallel edits/deletes is wrong:\n" + r.stdout)
# ---- installations, managed stores, and manual-only skills ----------

# Every probe below uses a temporary HOME and an offline source. No test may
# touch the real store: the repo memo is unmanaged and defaults to
# <repo>/memory.
INSTALL_ENV = {k: v for k, v in os.environ.items() if k != "MEMORY_DIR"}
INSTALL_ROOT = tempfile.mkdtemp(prefix="optmem-install-")
INSTALL_ENV["HOME"] = os.path.join(INSTALL_ROOT, "home")
INSTALL_ENV["OPTMEM_SOURCE"] = MEMO
INSTALL_ENV["PYTHONDONTWRITEBYTECODE"] = "1"
os.makedirs(INSTALL_ENV["HOME"])


def install(*args, source=None, cwd=None, home=None):
    """Run install.sh offline against a temporary HOME and source file."""
    env = dict(INSTALL_ENV)
    if source is not None:
        env["OPTMEM_SOURCE"] = source
    if home is not None:
        env["HOME"] = home
    return subprocess.run([INSTALL_SH] + list(args), env=env, cwd=cwd,
                          capture_output=True, text=True, timeout=120)


def command(*args):
    return [sys.executable] + list(args)


def file_map(path):
    """Every regular file under path except locks: frozen state snapshots."""
    out = {}
    for root, _, files in os.walk(path):
        for f in files:
            if f == ".lock":
                continue
            p = os.path.join(root, f)
            try:
                out[os.path.relpath(p, path)] = open(p, "rb").read()
            except FileNotFoundError:
                continue  # a broken fixture symlink is still a fixture file
    return out


def pycache_dirs(root):
    return [os.path.join(dp, d) for dp, dns, _ in os.walk(root) for d in dns
            if d == "__pycache__"]


def template_variant(path, version):
    """Write the current tool with one real TEMPLATE string change."""
    base = open(MEMO, "rb").read()
    anchor = b'\n"""\n\n\ndef markdown_code(text):'
    check(base.count(anchor) == 1, "the template source boundary is ambiguous")
    changed = base.replace(
        anchor, b"\nTemplate version " + version.encode()
        + b'.\n"""\n\n\ndef markdown_code(text):')
    with open(path, "wb") as f:
        f.write(changed)
    return path


# -- skill template and owner format, without touching a store (memo skill
# needs no memory directory at all)
plain_skill = cli.render_skill(cli.shell_command("/x/memo"), "/x/memory")
FRONTMATTER = ("---\nname: optmem\n"
               "description: Load and maintain this installation's memory for "
               "the current session only after explicit user activation.\n"
               "disable-model-invocation: true\n---\n\n")
check(plain_skill.startswith(FRONTMATTER),
      "skill frontmatter is not the exact manual-only header")
check("disable-model-invocation: true\n" in plain_skill.split("---")[1],
      "skill does not disable model invocation")
check(plain_skill.count("disable-model-invocation") == 1,
      "skill repeats the invocation flag")
check("\n## Memory\n" in plain_skill and "### Activate for the current session" in plain_skill,
      "skill does not embed the manual session policy")
check("mandatory" not in plain_skill.lower()
      and "subagent" not in plain_skill.lower()
      and "AGENTS.md" not in plain_skill
      and "CLAUDE.md" not in plain_skill,
      "skill still contains mandatory startup or subagent rules")
check(plain_skill.count("<!-- optmem-managed: ") == 1
      and plain_skill.endswith(" -->\n"),
      "skill owner comment is not one trailing marker")
owner = cli.skill_owner(plain_skill)
check(owner is not None and owner["command"] == "/x/memo"
      and owner["memory"] == "/x/memory" and owner["version"] == 1,
      "skill_owner does not read its own marker: %r" % (owner,))
check(set(owner) == {"version", "command", "memory", "sha256"},
      "the owner metadata has extra or missing fields: %r" % (sorted(owner),))
check(plain_skill.rstrip("\n").endswith("-->")
      and "<!-- optmem-managed:" in plain_skill.rstrip("\n").splitlines()[-1],
      "the owner marker is not the single trailing line")
body, _, tail = plain_skill.rpartition("\n<!-- optmem-managed: ")
check(body == FRONTMATTER + cli.TEMPLATE.format(memo="/x/memo",
                                                data="/x/memory"),
      "the skill body is not exactly frontmatter plus the manual template")
check(owner["sha256"] == hashlib.sha256(body.encode()).hexdigest(),
      "skill owner hash does not cover the body")
# json quoting uses double quotes, so a hash edit must preserve them; a
# trailing line or any changed byte after the marker breaks the single-marker
# contract that ownership relies on.
for bad in (plain_skill[:-1], plain_skill.replace("\n", "\r\n"),
            plain_skill + "trailing\n", plain_skill.replace('"version": 1', '"version": 2'),
            plain_skill.replace(" -->\n", "", 1), "",
            plain_skill.replace("These are agent instructions",
                                "These are not the agent instructions", 1),
            plain_skill + "<!-- optmem-managed: {} -->\n"):
    check(cli.skill_owner(bad) is None,
          "a corrupt or duplicated skill owner was accepted")

spaced = "/tmp/a b/it's; $(touch PWNED)/memo"
quoted = cli.render_skill(cli.shell_command(spaced), "/tmp/a b/it's; $(touch PWNED)/memory")
check("The tool is `%s`" % shlex.quote(spaced) in quoted,
      "a path with metacharacters is not shell-quoted in the skill")

# `memo skill` must generate without a store, and write nothing
skill_root = tempfile.mkdtemp(prefix="optmem-skill-gen-")
skill_target = os.path.join(skill_root, "uncreated")
r_ = subprocess.run(command(MEMO, "skill"),
                    env=dict(os.environ, MEMORY_DIR=skill_target),
                    capture_output=True, text=True)
check(r_.returncode == 0 and "name: optmem" in r_.stdout,
      "memo skill failed without a store: " + r_.stderr)
check(not os.path.exists(skill_target), "memo skill created a memory store")
r_ = subprocess.run(command(MEMO, "skill", "extra"), capture_output=True, text=True)
check(r_.returncode == 1 and "usage" in r_.stderr, "memo skill accepted arguments")
check(cli.skill_owner(r_.stdout) is None, "a failed skill wrote a document")
# unmanaged commands pin the override even when it is unset (bind_command)
plain_env = {k: v for k, v in os.environ.items() if k != "MEMORY_DIR"}
plain = subprocess.run(command(MEMO, "skill"), env=plain_env,
                       capture_output=True, text=True)
check(plain.returncode == 0, "unmanaged skill failed: " + plain.stderr)
check("MEMORY_DIR=%s " % shlex.quote(os.path.join(HERE, "memory"))
      in plain.stdout, "an unmanaged skill did not pin its default memory")
# main() binds the command from memory_dir() before dispatch, so even the
# usage text names a command that runs without a configured PATH
plain_usage = subprocess.run(command(MEMO), env=plain_env,
                             capture_output=True, text=True)
check(plain_usage.returncode == 0
      and "MEMORY_DIR=%s %s init" %
      (shlex.quote(os.path.join(HERE, "memory")), shlex.quote(MEMO))
      in plain_usage.stdout,
      "the no-command usage did not use the main bind_command: "
      + plain_usage.stdout + plain_usage.stderr)
shutil.rmtree(skill_root)


# -- one installed instance: marker, sibling memory, pinned commands --
A = os.path.join(INSTALL_ROOT, "A")
A_SKILL = os.path.join(INSTALL_ROOT, "skills", "optmem")
r_ = install(A, "--skill-dir", A_SKILL)
check(r_.returncode == 0, "a fresh install failed: " + r_.stderr)
check(open(os.path.join(A, ".optmem-install"), encoding="utf-8").read()
      == '{"version": 1}\n', "the installation marker is not {version: 1}")
check(not os.path.islink(os.path.join(A, "memo")), "the installed memo is a symlink")
check(os.access(os.path.join(A, "memo"), os.X_OK),
      "the installed memo is not executable")
check(os.path.exists(os.path.join(A, "memory", "config")),
      "install did not initialize the sibling memory")
check(open(os.path.join(A, "memo"), "rb").read() == open(MEMO, "rb").read(),
      "the installed memo is not the source")
check(not pycache_dirs(A) and not pycache_dirs(A_SKILL),
      "installation left __pycache__ behind")
A_MEMO = os.path.join(A, "memo")
A_USAGE = subprocess.run(command(A_MEMO), env=INSTALL_ENV,
                        capture_output=True, text=True)
check(A_USAGE.returncode == 0 and "%s init" % shlex.quote(A_MEMO) in A_USAGE.stdout
      and "MEMORY_DIR=" not in A_USAGE.stdout,
      "managed usage did not use the bare bound command: " + A_USAGE.stdout)
foreign = os.path.join(INSTALL_ROOT, "foreign-memory")
A_ENV = dict(INSTALL_ENV, MEMORY_DIR=foreign)
# a managed copy ignores an ambient MEMORY_DIR entirely, even one that exists
os.makedirs(foreign)
r_ = subprocess.run(command(A_MEMO, "note", "alpha in A"), env=A_ENV,
                    capture_output=True, text=True)
check(r_.returncode == 0 and "Saved as #0" in r_.stdout,
      "managed note failed: " + r_.stdout + r_.stderr)
r_ = subprocess.run(command(A_MEMO, "note", "beta in A"), env=A_ENV,
                    capture_output=True, text=True)
check(r_.returncode == 0, "managed second note failed: " + r_.stderr)
check(not os.listdir(foreign), "a managed copy wrote to an ambient MEMORY_DIR"
      + repr(os.listdir(foreign)))
check(os.path.getsize(os.path.join(A, "memory", "LOG.txt")) == 2 * 2112,
      "managed notes did not land beside the executable")
r_ = subprocess.run(command(A_MEMO, "wake"), env=dict(INSTALL_ENV, MEMORY_DIR=os.path.join(INSTALL_ROOT, "absent")),
                    capture_output=True, text=True)
check(r_.returncode == 0 and "alpha in A" in r_.stdout,
      "a managed copy did not pin its sibling memory")

# every command a managed copy prints names only its absolute path, with no
# MEMORY_DIR prefix: its installation marker is the pin
r_ = subprocess.run(command(A_MEMO, "nap"), env=A_ENV, capture_output=True, text=True)
check(r_.returncode == 0 and "Compress memories #0-1" in r_.stdout,
      "managed nap prompt failed: " + r_.stdout + r_.stderr)
order = [l[5:] for l in r_.stdout.splitlines() if l.startswith("Run: ")]
check(len(order) == 1 and order[0].startswith(shlex.quote(A_MEMO) + " nap "),
      "managed command is not the bare quoted executable: %r" % order)
check("MEMORY_DIR=" not in order[0], "a managed command added a MEMORY_DIR prefix")
obeyed = subprocess.run(order[0].replace('"<your line>"', '"both A memories"'),
                        shell=True, env=dict(A_ENV, MEMORY_DIR="/nowhere"),
                        capture_output=True, text=True)
check(obeyed.returncode == 0 and "saved" in obeyed.stdout,
      "a managed nap command did not run: " + obeyed.stderr)
r_ = subprocess.run(command(A_MEMO, "init"), env=A_ENV, capture_output=True, text=True)
check(r_.returncode == 0 and "The tool is `%s`" % shlex.quote(A_MEMO) in r_.stdout
      and "- The memory directory is `%s`" % os.path.join(A, "memory") in r_.stdout,
      "managed init does not name the bare path and sibling memory")
check("MEMORY_DIR=" not in r_.stdout, "managed init printed an env prefix")

# generated skill is byte-identical to the renderer for this installation
A_skill_text = open(os.path.join(A_SKILL, "SKILL.md"), encoding="utf-8").read()
check(A_skill_text == cli.render_skill(shlex.quote(A_MEMO),
                                       os.path.join(A, "memory")),
      "installed skill is not render_skill of this installation")
A_owner = cli.skill_owner(A_skill_text)
check(A_owner is not None and A_owner["command"] == shlex.quote(A_MEMO)
      and A_owner["memory"] == os.path.join(A, "memory"),
      "installed skill owner names the wrong installation: %r" % (A_owner,))
check(os.path.basename(A_SKILL) == "optmem"
      and not os.path.exists(os.path.join(A_SKILL, "optmem")),
      "the installer treated the skill parent, not the full folder, as the skill")
check(sorted(os.listdir(A_SKILL)) == ["SKILL.md"],
      "the skill folder holds files besides SKILL.md: %r"
      % (sorted(os.listdir(A_SKILL)),))

# --skill-dir names the full skill folder, never its parent: passing an empty
# folder writes SKILL.md directly there, with no nested optmem/
EXACT_SKILL = os.path.join(INSTALL_ROOT, "exact skill folder")
os.makedirs(EXACT_SKILL)
EXACT_INST = os.path.join(INSTALL_ROOT, "exact-inst")
r_ = install(EXACT_INST, "--skill-dir", EXACT_SKILL)
check(r_.returncode == 0, "exact-folder install failed: " + r_.stderr)
check(os.path.exists(os.path.join(EXACT_SKILL, "SKILL.md"))
      and not os.path.exists(os.path.join(EXACT_SKILL, "optmem")),
      "install.sh treated the skill parent as the skill folder")

# -- a second, separate instance: no ambient env can mix the two ---------
B = os.path.join(INSTALL_ROOT, "B")
r_ = install(B)
check(r_.returncode == 0, "the second install failed: " + r_.stderr)
B_MEMO = os.path.join(B, "memo")
r_ = subprocess.run(command(B_MEMO, "note", "beta only in B"),
                    env=dict(INSTALL_ENV, MEMORY_DIR=os.path.join(A, "memory")),
                    capture_output=True, text=True)
check(r_.returncode == 0, "second instance note failed: " + r_.stderr)
r_ = subprocess.run(command(B_MEMO, "recall", "alpha in A"),
                    env=dict(INSTALL_ENV, MEMORY_DIR=os.path.join(A, "memory")),
                    capture_output=True, text=True)
check(r_.returncode == 0 and "No match" in r_.stdout
      and "alpha" not in r_.stdout,
      "an ambient MEMORY_DIR mixed two installed instances: " + r_.stdout)
r_ = subprocess.run(command(A_MEMO, "recall", "beta only in B"),
                    env=dict(INSTALL_ENV, MEMORY_DIR=os.path.join(B, "memory")),
                    capture_output=True, text=True)
check(r_.returncode == 0 and "No match" in r_.stdout,
      "instance A read instance B through MEMORY_DIR: " + r_.stdout)
check(os.path.getsize(os.path.join(B, "memory", "LOG.txt")) == 2112,
      "instance B did not write its own store")

# IDs, revision, config, and lock are local to each instance
check(not os.path.exists(os.path.join(B, "memory", ".revision")),
      "a fresh instance started with a revision")
subprocess.run(command(B_MEMO, "note", "beta second"), env=INSTALL_ENV,
               capture_output=True)
subprocess.run(command(B_MEMO, "edit", "0", "beta edited"), env=INSTALL_ENV,
               capture_output=True)
check(open(os.path.join(B, "memory", ".revision"), encoding="ascii").read().strip() == "1",
      "the second instance lost its own revision")
check(not os.path.exists(os.path.join(A, "memory", ".revision")),
      "instance B's revision leaked into A")
r_ = subprocess.run(command(B_MEMO, "config", "WAKE_LINES=5"), env=INSTALL_ENV,
                    capture_output=True, text=True)
check(r_.returncode == 0 and "WAKE_LINES    5" in r_.stdout,
      "per-instance config failed: " + r_.stdout + r_.stderr)
r_ = subprocess.run(command(A_MEMO, "config"), env=INSTALL_ENV,
                    capture_output=True, text=True)
check("WAKE_LINES    96" in r_.stdout, "config leaked between instances: " + r_.stdout)
r_ = subprocess.run(command(A_MEMO, "note", "alpha second"), env=INSTALL_ENV,
                    capture_output=True, text=True)
check(r_.returncode == 0 and "Saved as #2" in r_.stdout,
      "instance A's second note failed: " + r_.stdout + r_.stderr)
r_ = subprocess.run(command(A_MEMO, "note", "alpha third"), env=INSTALL_ENV,
                    capture_output=True, text=True)
check(r_.returncode == 0 and "Saved as #3" in r_.stdout,
      "instance A's third note failed: " + r_.stdout + r_.stderr)
check(os.path.getsize(os.path.join(A, "memory", "LOG.txt")) == 4 * 2112,
      "instance A's second/third notes are missing")
check(os.path.getsize(os.path.join(B, "memory", "LOG.txt")) == 2 * 2112,
      "instance A's notes landed in B")
# a lock in one instance does not block the other
lockA = open(os.path.join(A, "memory", ".lock"), "a")
try:
    try:
        import fcntl
    except ImportError:
        fcntl = None  # Windows: skip the advisory-lock probe, not the test
    if fcntl is not None:
        fcntl.flock(lockA, fcntl.LOCK_EX)
    r_ = subprocess.run(command(B_MEMO, "wake"), env=INSTALL_ENV,
                        capture_output=True, text=True, timeout=30)
    check(r_.returncode == 0, "instance B waited on instance A's lock: " + r_.stderr)
finally:
    lockA.close()
# a corrupt journal is local: instance B fails closed, instance A keeps working
CORRUPT_JOURNAL = os.path.join(B, "memory", ".pending")
open(CORRUPT_JOURNAL, "w", encoding="utf-8").write("{not json")
r_ = subprocess.run(command(B_MEMO, "wake"), env=INSTALL_ENV,
                    capture_output=True, text=True)
check(r_.returncode == 1 and ".pending" in r_.stderr and "corrupt" in r_.stderr,
      "a corrupt journal was not reported: " + r_.stdout + r_.stderr)
r_ = subprocess.run(command(A_MEMO, "wake"), env=INSTALL_ENV,
                    capture_output=True, text=True)
check(r_.returncode == 0 and "alpha in A" in r_.stdout,
      "instance B's corrupt journal blocked instance A: " + r_.stdout + r_.stderr)
os.unlink(CORRUPT_JOURNAL)
r_ = subprocess.run(command(B_MEMO, "wake"), env=INSTALL_ENV,
                    capture_output=True, text=True)
check(r_.returncode == 0 and "beta edited" in r_.stdout,
      "instance B did not recover after its journal was removed: "
      + r_.stdout + r_.stderr)

# -- unmanaged copies keep MEMORY_DIR, pinned in every printed command ----
U = os.path.join(INSTALL_ROOT, "unmanaged dir with 'quote' and ; chars")
os.makedirs(U)
shutil.copy2(MEMO, os.path.join(U, "memo"))
U_MEMO = os.path.join(U, "memo")
U_STORE = os.path.join(INSTALL_ROOT, "unmanaged-store")
U_ENV = dict(INSTALL_ENV, MEMORY_DIR=U_STORE)
r_ = subprocess.run(command(U_MEMO, "init"), env=U_ENV, capture_output=True, text=True)
check(r_.returncode == 0, "unmanaged init failed: " + r_.stderr)
check("- The tool is `MEMORY_DIR=%s %s`" %
      (shlex.quote(U_STORE), shlex.quote(U_MEMO)) in r_.stdout,
      "unmanaged init does not quote and pin its executable")
check("- The memory directory is `%s`" % U_STORE in r_.stdout,
      "unmanaged init did not print the MEMORY_DIR store")
check(("MEMORY_DIR=%s %s" % (shlex.quote(U_STORE), shlex.quote(U_MEMO)))
      in r_.stdout or ("MEMORY_DIR=%s %s" % (shlex.quote(U_STORE), U_MEMO))
      in r_.stdout, "unmanaged init did not pin MEMORY_DIR in commands")
subprocess.run(command(U_MEMO, "note", "unmanaged one"), env=U_ENV,
               capture_output=True)
r_ = subprocess.run(command(U_MEMO, "note", "unmanaged two"), env=U_ENV,
                    capture_output=True, text=True)
check(r_.returncode == 0, "unmanaged note failed: " + r_.stderr)
order = [l[5:] for l in r_.stdout.splitlines() if l.startswith("Run: ")]
check(len(order) == 1 and order[0].startswith(
          "MEMORY_DIR=%s %s nap " % (shlex.quote(U_STORE), shlex.quote(U_MEMO))),
      "an unmanaged command does not pin MEMORY_DIR: %r" % order)
obeyed = subprocess.run(order[0].replace('"<your line>"', '"both unmanaged"'),
                        shell=True,
                        env=dict(U_ENV, MEMORY_DIR=os.path.join(INSTALL_ROOT, "other")),
                        capture_output=True, text=True)
check(obeyed.returncode == 0 and "saved" in obeyed.stdout,
      "an unmanaged pinned command did not run: " + obeyed.stderr)
check(os.path.exists(os.path.join(U_STORE, "TREE", "2")),
      "the pinned command wrote to the wrong store")
check(not os.path.exists(os.path.join(INSTALL_ROOT, "other")),
      "a changed MEMORY_DIR captured an unmanaged command")
# with MEMORY_DIR unset, an unmanaged copy defaults beside itself and still
# pins that absolute default, so a later override cannot move it
NOENV = {k: v for k, v in INSTALL_ENV.items() if k != "MEMORY_DIR"}
r_ = subprocess.run(command(U_MEMO, "wake"), env=NOENV, capture_output=True, text=True)
check(r_.returncode == 1 and "No memory at %s" % os.path.join(U, "memory") in r_.stderr,
      "an unmanaged copy without MEMORY_DIR did not use its sibling default")
check("MEMORY_DIR=%s" % shlex.quote(os.path.join(U, "memory")) in r_.stderr,
      "the init hint did not pin the absolute default memory")

# -- the generated orders run from elsewhere with hostile path characters --
# A path with spaces, apostrophes, and metacharacters must survive shell
# execution, and none of it may execute.
HOST = os.path.join(INSTALL_ROOT, "hostile 'dir'; $(touch PWNED) space")
HOST_SKILL = os.path.join(INSTALL_ROOT, "hostile skills 'x';", "optmem")
r_ = install(HOST, "--skill-dir", HOST_SKILL)
check(r_.returncode == 0, "install into a hostile path failed: " + r_.stderr)
HOST_MEMO = os.path.join(HOST, "memo")
HOST_CWD = os.path.join(INSTALL_ROOT, "elsewhere")
os.makedirs(HOST_CWD)
for t in ("hostile one", "hostile two", "hostile three", "hostile four"):
    r_ = subprocess.run(command(HOST_MEMO, "note", t), env=INSTALL_ENV,
                        cwd=HOST_CWD, capture_output=True, text=True)
    check(r_.returncode == 0, "hostile note failed: " + r_.stderr)
spoken = []
for step in range(20):
    r_ = subprocess.run(command(HOST_MEMO, "nap"), env=INSTALL_ENV,
                        cwd=HOST_CWD, capture_output=True, text=True)
    lines = [l[5:] for l in r_.stdout.splitlines() if l.startswith("Run: ")]
    if not lines:
        check("Nothing left to compress" in r_.stdout,
              "hostile nap stopped early: " + r_.stdout + r_.stderr)
        break
    spoken.append(lines[0])
    x = subprocess.run(lines[0].replace('"<your line>"', '"hostile summary"'),
                       shell=True, env=INSTALL_ENV, cwd=HOST_CWD,
                       capture_output=True, text=True)
    check(x.returncode == 0, "a hostile nap order did not run: " + x.stderr)
check(len(spoken) == 3, "expected 3 hostile compressions, got %d" % len(spoken))
# make wake paginate by narrowing parts, then run its order from the other
# directory
subprocess.run(command(HOST_MEMO, "config", "WAKE_LINES=4", "PART_LINES=2"),
               env=INSTALL_ENV, cwd=HOST_CWD, capture_output=True)
r_ = subprocess.run(command(HOST_MEMO, "wake"), env=INSTALL_ENV,
                    cwd=HOST_CWD, capture_output=True, text=True)
check(r_.returncode == 0 and "Not awake yet" in r_.stdout,
      "hostile wake did not paginate: " + r_.stdout + r_.stderr)
orders = [l.split("Run: ", 1)[1] for l in r_.stdout.splitlines() if "Run: " in l]
check(len(orders) == 1 and "wake 2" in orders[0],
      "hostile wake did not order its next part: %r" % orders)
x = subprocess.run(orders[0], shell=True, env=INSTALL_ENV, cwd=HOST_CWD,
                   capture_output=True, text=True)
check(x.returncode == 0 and "You are awake." in x.stdout,
      "a hostile wake order did not run: " + x.stdout + x.stderr)
check(not os.path.exists(os.path.join(HOST_CWD, "PWNED"))
      and not os.path.exists(os.path.join(INSTALL_ROOT, "PWNED"))
      and not os.path.exists(os.path.join(HOST, "PWNED")),
      "a generated command executed an injected shell word")

# Markdown command spans must survive backticks and braces in real paths.
TICK_INST = os.path.join(INSTALL_ROOT, "tick`dir {memo}")
TICK_SKILL = os.path.join(INSTALL_ROOT, "tick-skill")
r_ = install(TICK_INST, "--skill-dir", TICK_SKILL)
check(r_.returncode == 0, "a backtick installation path failed: " + r_.stderr)
tick_command = shlex.quote(os.path.join(TICK_INST, "memo"))
tick_text = open(os.path.join(TICK_SKILL, "SKILL.md"), encoding="utf-8").read()
check("- The tool is ``" + tick_command + "``" in tick_text,
      "a backtick broke the skill's executable command span")
check("Run ``" + tick_command + " wake`` immediately" in tick_text,
      "a backtick broke the skill's wake command span")
check("memory directory is ``" + os.path.join(TICK_INST, "memory") + "``" in tick_text,
      "a backtick broke the skill's data path span")
check("{memo}" in tick_text, "a literal placeholder in a path was expanded")

# -- alias and symlink resolution: one real instance, one memory ----------
ALIAS = os.path.join(INSTALL_ROOT, "alias-A")
os.symlink(A, ALIAS)
r_ = subprocess.run(command(os.path.join(ALIAS, "memo"), "wake"), env=INSTALL_ENV,
                    capture_output=True, text=True)
check(r_.returncode == 0 and "alpha in A" in r_.stdout,
      "reading through a realpath alias missed the pinned store: " + r_.stderr)
r_ = subprocess.run(command(os.path.join(ALIAS, "memo"), "skill"), env=INSTALL_ENV,
                    capture_output=True, text=True)
check(r_.returncode == 0 and "The tool is `%s`" % shlex.quote(A_MEMO) in r_.stdout,
      "a skill generated through an alias did not use the real path")
LINK_MEMO = os.path.join(INSTALL_ROOT, "linked-memo")
os.symlink(A_MEMO, LINK_MEMO)
r_ = subprocess.run(command(LINK_MEMO, "wake"),
                    env=dict(INSTALL_ENV, MEMORY_DIR=os.path.join(B, "memory")),
                    capture_output=True, text=True)
check(r_.returncode == 0 and "alpha in A" in r_.stdout,
      "a symlinked executable was not resolved to its real store: " + r_.stderr)

# -- marker corruption fails closed for every command and the installer --
CORRUPT = os.path.join(INSTALL_ROOT, "corrupt-marker")
os.makedirs(CORRUPT)
shutil.copy2(MEMO, os.path.join(CORRUPT, "memo"))
os.makedirs(os.path.join(CORRUPT, "memory"))
CORRUPT_MEMO = os.path.join(CORRUPT, "memo")
for value in ('{"version": 2}\n', '{"version": true}\n', '[]\n',
              '{"version": 1, "extra": 1}\n', 'not json\n', '\n'):
    open(os.path.join(CORRUPT, ".optmem-install"), "w", encoding="utf-8").write(value)
    for c in ("wake", "note", "init", "skill", "config"):
        r_ = subprocess.run(command(CORRUPT_MEMO, c), env=INSTALL_ENV,
                            capture_output=True, text=True)
        check(r_.returncode == 1 and "corrupt" in r_.stderr
              and "remove the damaged marker" in r_.stderr and "Traceback" not in r_.stderr,
              "marker value %r let %s run: %s" % (value, c, r_.stdout + r_.stderr))
    before = file_map(CORRUPT)
    r_ = install(CORRUPT)
    check(r_.returncode == 1 and "corrupt" in r_.stderr,
          "the installer accepted marker value %r: %s" % (value, r_.stderr))
    check(file_map(CORRUPT) == before, "a corrupt marker was overwritten")
# a directory or symlink marker is not a regular installation marker
os.unlink(os.path.join(CORRUPT, ".optmem-install"))
os.makedirs(os.path.join(CORRUPT, ".optmem-install"))
r_ = subprocess.run(command(CORRUPT_MEMO, "wake"), env=INSTALL_ENV,
                    capture_output=True, text=True)
check(r_.returncode == 1 and "regular installation marker" in r_.stderr,
      "a directory marker was accepted: " + r_.stderr)
os.rmdir(os.path.join(CORRUPT, ".optmem-install"))
os.symlink(os.path.join(INSTALL_ROOT, "missing-marker-target"),
           os.path.join(CORRUPT, ".optmem-install"))
r_ = subprocess.run(command(CORRUPT_MEMO, "wake"), env=INSTALL_ENV,
                    capture_output=True, text=True)
check(r_.returncode == 1 and "regular installation marker" in r_.stderr,
      "a symlink marker was accepted: " + r_.stderr)
os.unlink(os.path.join(CORRUPT, ".optmem-install"))
open(os.path.join(CORRUPT, ".optmem-install"), "w", encoding="utf-8").write(
    '{"version": 1}\n')

# A managed store without its marker is unmanaged: MEMORY_DIR works again.
UNMARKED = os.path.join(INSTALL_ROOT, "unmarked-copy")
os.makedirs(UNMARKED)
shutil.copy2(MEMO, os.path.join(UNMARKED, "memo"))
UNMARKED_MEMO = os.path.join(UNMARKED, "memo")
UNMARKED_STORE = os.path.join(INSTALL_ROOT, "unmarked-store")
r_ = subprocess.run(command(UNMARKED_MEMO, "init"),
                    env=dict(INSTALL_ENV, MEMORY_DIR=UNMARKED_STORE),
                    capture_output=True, text=True)
check(r_.returncode == 0, "unmarked copy init failed: " + r_.stderr)
subprocess.run(command(UNMARKED_MEMO, "note", "unmarked memory"),
               env=dict(INSTALL_ENV, MEMORY_DIR=UNMARKED_STORE), capture_output=True)
r_ = subprocess.run(command(UNMARKED_MEMO, "recall", "unmarked memory"),
                    env=dict(INSTALL_ENV, MEMORY_DIR=UNMARKED_STORE),
                    capture_output=True, text=True)
check(r_.returncode == 0 and "#0 " in r_.stdout,
      "an unmarked copy ignored MEMORY_DIR: " + r_.stdout + r_.stderr)

# -- unmanaged conversion refuses an ambient foreign store without changes --
CONV = os.path.join(INSTALL_ROOT, "unmanaged-convert")
os.makedirs(CONV)
shutil.copy2(MEMO, os.path.join(CONV, "memo"))
CONV_MEMO = os.path.join(CONV, "memo")
CONV_STORE = os.path.join(INSTALL_ROOT, "unmanaged-convert-store")
CONV_ENV = dict(INSTALL_ENV, MEMORY_DIR=CONV_STORE)
r_ = subprocess.run(command(CONV_MEMO, "init"), env=CONV_ENV,
                    capture_output=True, text=True)
check(r_.returncode == 0, "conversion fixture init failed: " + r_.stderr)
for t in ("conversion one", "conversion two"):
    r_ = subprocess.run(command(CONV_MEMO, "note", t), env=CONV_ENV,
                        capture_output=True, text=True)
    check(r_.returncode == 0, "conversion fixture note failed: " + r_.stderr)
conv_orders = [l[5:] for l in r_.stdout.splitlines() if l.startswith("Run: ")]
CONV_BEFORE = file_map(CONV)
CONV_STORE_BEFORE = fingerprint(CONV_STORE)
r_ = subprocess.run([INSTALL_SH, CONV], env=CONV_ENV,
                    capture_output=True, text=True)
check(r_.returncode == 1 and "MEMORY_DIR=%s" % CONV_STORE in r_.stderr
      and "no memories were moved" in r_.stderr,
      "an unmanaged copy with an external store was converted: " + r_.stderr)
check(file_map(CONV) == CONV_BEFORE and not os.path.exists(os.path.join(CONV, "memory"))
      and not os.path.exists(os.path.join(CONV, ".optmem-install"))
      and not os.path.exists(os.path.join(CONV, ".optmem-skills")),
      "a refused unmanaged conversion changed files or created a sibling store")
check(fingerprint(CONV_STORE) == CONV_STORE_BEFORE,
      "a refused unmanaged conversion changed the external store")
check(len(conv_orders) == 1, "the unmanaged fixture printed no pinned command")
conv_run = subprocess.run(
    conv_orders[0].replace('"<your line>"', '"both conversion memories"'),
    shell=True, env=dict(CONV_ENV, MEMORY_DIR=os.path.join(INSTALL_ROOT, "other")),
    capture_output=True, text=True)
check(conv_run.returncode == 0 and "saved" in conv_run.stdout
      and os.path.exists(os.path.join(CONV_STORE, "TREE", "2")),
      "the old unmanaged command no longer reached its external store: "
      + conv_run.stdout + conv_run.stderr)
check(not os.path.exists(os.path.join(INSTALL_ROOT, "other")),
      "a changed MEMORY_DIR captured the refused unmanaged command")

# a managed copy without its store names its own memory, not MEMORY_DIR
MISSING = os.path.join(INSTALL_ROOT, "missing-managed")
r_ = install(MISSING)
check(r_.returncode == 0, "missing-store fixture install failed: " + r_.stderr)
shutil.rmtree(os.path.join(MISSING, "memory"))
r_ = subprocess.run(command(os.path.join(MISSING, "memo"), "wake"),
                    env=dict(INSTALL_ENV, MEMORY_DIR=os.path.join(INSTALL_ROOT, "foreign-missing")),
                    capture_output=True, text=True)
check(r_.returncode == 1 and "No memory at %s" % os.path.join(MISSING, "memory") in r_.stderr
      and "MEMORY_DIR" not in r_.stderr
      and "Use this installation's own memory directory." in r_.stderr,
      "a managed missing store suggested MEMORY_DIR: " + r_.stderr)
check(not os.path.exists(os.path.join(INSTALL_ROOT, "foreign-missing")),
      "a managed missing-store error created the ambient MEMORY_DIR")

# -- installer validation: bad sources and targets change nothing ---------
for label, source in (
        ("missing", os.path.join(INSTALL_ROOT, "no-such-source")),
        ("syntax", None),
        ("helper-missing", None),
        ("runtime", None)):
    src = source
    if label == "syntax":
        src = os.path.join(INSTALL_ROOT, "syntax-source")
        open(src, "w", encoding="utf-8").write("this is not python !!!\n")
    if label == "helper-missing":
        src = os.path.join(INSTALL_ROOT, "helper-source")
        open(src, "w", encoding="utf-8").write("value = 1\n")
    if label == "runtime":
        src = os.path.join(INSTALL_ROOT, "runtime-source")
        open(src, "w", encoding="utf-8").write(
            "raise RuntimeError('import failure')\n")
    target = os.path.join(INSTALL_ROOT, "bad-source-" + label)
    r_ = install(target, source=src)
    check(r_.returncode == 1, "install accepted a %s source" % label)
    check(not os.path.exists(target), "a %s source left a target" % label)
    if label in ("helper-missing", "runtime"):
        check("Traceback" not in r_.stderr,
              "a %s source printed a traceback: " % label
              + r_.stderr.splitlines()[0])
    if label == "runtime":
        check("Install failed:" in r_.stderr and "import failure" in r_.stderr,
              "a runtime source error was not reported cleanly: " + r_.stderr)

# OPTMEM_SOURCE is copied as ./NAME, so a source whose name starts with '-'
# must work without being parsed as a cp option.
DASH_ROOT = tempfile.mkdtemp(prefix="optmem-dash-source-")
try:
    shutil.copy2(MEMO, os.path.join(DASH_ROOT, "-memo-source"))
    DASH_INST = os.path.join(DASH_ROOT, "dash-instance")
    r_ = install(DASH_INST, source="-memo-source", cwd=DASH_ROOT)
    check(r_.returncode == 0
          and open(os.path.join(DASH_INST, "memo"), "rb").read()
          == open(MEMO, "rb").read(),
          "a relative OPTMEM_SOURCE with a leading dash failed: "
          + r_.stdout + r_.stderr)
finally:
    shutil.rmtree(DASH_ROOT)

# -- installer flag parsing and defaults -----------------------------------
for args in (["--help"], ["-h"]):
    r_ = install(*args)
    check(r_.returncode == 0 and "usage: install.sh" in r_.stdout
          and r_.stderr.strip() == "",
          "help did not print usage: " + r_.stdout + r_.stderr)
for args in (["--skill-dir"], ["--skill-dir", ""], ["--bad"], ["a", "b"],
             ["a", "--skill-dir"], ["a", "--skill-dir", "s", "--skill-dir", "t"],
             ["--skill-dir", "s", "a", "b"], [""]):
    r_ = install(*args)
    check(r_.returncode == 1 and "usage: install.sh" in r_.stderr
          and r_.stdout.strip() == "",
          "install accepted bad flags %r: %s" % (args, r_.stdout + r_.stderr))
FLAG_INST = os.path.join(INSTALL_ROOT, "flag-order-inst")
FLAG_SKILL = os.path.join(INSTALL_ROOT, "flag-order-skill")
r_ = install("--skill-dir", FLAG_SKILL, FLAG_INST)
check(r_.returncode == 0 and os.path.exists(os.path.join(FLAG_INST, "memo"))
      and os.path.exists(os.path.join(FLAG_SKILL, "SKILL.md")),
      "install rejected --skill-dir before the install directory: "
      + r_.stdout + r_.stderr)
no_home = {k: v for k, v in INSTALL_ENV.items() if k != "HOME"}
r_ = subprocess.run([INSTALL_SH], env=no_home, capture_output=True, text=True)
check(r_.returncode == 1 and "HOME is not set" in r_.stderr,
      "install without HOME or a directory did not ask for one: " + r_.stderr)
NO_HOME_INST = os.path.join(INSTALL_ROOT, "no-home-explicit")
r_ = subprocess.run([INSTALL_SH, NO_HOME_INST], env=no_home,
                    capture_output=True, text=True)
check(r_.returncode == 0 and os.path.exists(os.path.join(NO_HOME_INST, "memo")),
      "an explicit install directory still required HOME: " + r_.stderr)
DEFAULT_HOME = tempfile.mkdtemp(prefix="optmem-default-home-")
r_ = install(home=DEFAULT_HOME)
check(r_.returncode == 0
      and os.path.exists(os.path.join(DEFAULT_HOME, ".optmem", "memo"))
      and os.path.exists(os.path.join(DEFAULT_HOME, ".optmem", "memory", "config")),
      "the default ~/.optmem install failed: " + r_.stderr)
check(not os.path.exists(os.path.join(HERE, ".optmem-install")),
      "the default install leaked into the repo")
shutil.rmtree(DEFAULT_HOME)

# -- install target validation: overlap, symlinks, nonempty directories ----
OVER = os.path.join(INSTALL_ROOT, "overlap")
r_ = install(OVER, "--skill-dir", os.path.join(OVER, "skill"))
check(r_.returncode == 1 and "must not overlap" in r_.stderr,
      "overlapping install and skill dirs were accepted: " + r_.stderr)
check(not os.path.exists(OVER), "an overlapping install left files")
r_ = install(os.path.join(OVER, "skill"), "--skill-dir", OVER)
check(r_.returncode == 1 and "must not overlap" in r_.stderr,
      "reverse overlap was accepted: " + r_.stderr)
check(not os.path.exists(OVER), "a reverse-overlap install left files")
ANC_REAL = os.path.join(INSTALL_ROOT, "ancestor-real")
os.makedirs(ANC_REAL)
ANC_ALIAS = os.path.join(INSTALL_ROOT, "ancestor-alias")
os.symlink(ANC_REAL, ANC_ALIAS)
ANC_INST = os.path.join(ANC_REAL, "instance")
ANC_SKILL = os.path.join(ANC_ALIAS, "instance", "skill")
r_ = install(ANC_INST, "--skill-dir", ANC_SKILL)
check(r_.returncode == 1 and "must not overlap" in r_.stderr,
      "an ancestor symlink hid an overlapping skill directory: " + r_.stderr)
check(not os.path.exists(ANC_INST)
      and not os.path.exists(os.path.join(ANC_REAL, "instance", "skill")),
      "an ancestor symlink overlap refusal wrote files")

INST_FILE = os.path.join(INSTALL_ROOT, "install-is-a-file")
open(INST_FILE, "w", encoding="utf-8").write("not a directory\n")
r_ = install(INST_FILE)
check(r_.returncode == 1 and open(INST_FILE, encoding="utf-8").read()
      == "not a directory\n",
      "a regular-file install destination was accepted or changed: "
      + r_.stdout + r_.stderr)
SYM_TARGET = os.path.join(INSTALL_ROOT, "symlink-install-target")
SYM_ALIAS = os.path.join(INSTALL_ROOT, "symlink-install-alias")
os.makedirs(SYM_TARGET)
os.symlink(SYM_TARGET, SYM_ALIAS)
r_ = install(SYM_ALIAS)
check(r_.returncode == 0
      and os.path.islink(SYM_ALIAS)
      and os.path.exists(os.path.join(SYM_TARGET, "memo"))
      and open(os.path.join(SYM_TARGET, ".optmem-install"), encoding="utf-8").read()
      == '{"version": 1}\n',
      "an install-directory symlink was not resolved to its real target: "
      + r_.stdout + r_.stderr)
SYM_INST = os.path.join(INSTALL_ROOT, "symlink-install")
os.makedirs(SYM_INST)
os.symlink("/bin/true", os.path.join(SYM_INST, "memo"))
r_ = install(SYM_INST)
check(r_.returncode == 1 and "non-regular executable" in r_.stderr,
      "a symlink memo destination was overwritten: " + r_.stderr)
check(os.path.islink(os.path.join(SYM_INST, "memo")),
      "a refused symlink memo destination changed")
SYM_FOLDER = os.path.join(INSTALL_ROOT, "symlink-skill-folder")
os.makedirs(os.path.join(INSTALL_ROOT, "real-skill-folder"))
os.symlink(os.path.join(INSTALL_ROOT, "real-skill-folder"), SYM_FOLDER)
r_ = install(os.path.join(INSTALL_ROOT, "symlink-folder-inst"),
             "--skill-dir", SYM_FOLDER)
check(r_.returncode == 1 and "symlink skill destination" in r_.stderr,
      "a symlink skill folder was accepted: " + r_.stderr)
check(not os.path.exists(os.path.join(INSTALL_ROOT, "symlink-folder-inst"))
      and not os.path.exists(os.path.join(INSTALL_ROOT, "real-skill-folder",
                                          "SKILL.md")),
      "a symlink skill folder refusal wrote files")
SYM_SKILL = os.path.join(INSTALL_ROOT, "symlink-skill")
os.makedirs(SYM_SKILL)
os.symlink(os.path.join(INSTALL_ROOT, "elsewhere"), os.path.join(SYM_SKILL, "SKILL.md"))
r_ = install(os.path.join(INSTALL_ROOT, "symlink-skill-inst"), "--skill-dir", SYM_SKILL)
check(r_.returncode == 1 and "symlink" in r_.stderr,
      "a symlink SKILL.md was accepted: " + r_.stderr)
check(not os.path.exists(os.path.join(INSTALL_ROOT, "symlink-skill-inst")),
      "a symlink skill target left an installation")
# a skill destination that is an existing regular file is not a skill folder;
# the file must stay untouched
SKILL_FILE = os.path.join(INSTALL_ROOT, "skill-is-a-file")
open(SKILL_FILE, "w", encoding="utf-8").write("not a directory\n")
FILE_INST = os.path.join(INSTALL_ROOT, "skill-file-inst")
r_ = install(FILE_INST, "--skill-dir", SKILL_FILE)
check(r_.returncode == 1
      and open(SKILL_FILE, encoding="utf-8").read() == "not a directory\n",
      "a regular-file skill destination was accepted or changed")
check(not os.path.exists(FILE_INST),
      "a regular-file skill destination left the executable and "
      "marker: %r"
      % (sorted(os.listdir(FILE_INST)) if os.path.isdir(FILE_INST) else None,))
NONEMPTY = os.path.join(INSTALL_ROOT, "nonempty-skill")
os.makedirs(NONEMPTY)
open(os.path.join(NONEMPTY, "other.txt"), "w").write("x")
r_ = install(os.path.join(INSTALL_ROOT, "nonempty-inst"), "--skill-dir", NONEMPTY)
check(r_.returncode == 1 and "nonempty" in r_.stderr,
      "a nonempty unmanaged skill directory was accepted: " + r_.stderr)
check(not os.path.exists(os.path.join(INSTALL_ROOT, "nonempty-inst")),
      "a nonempty skill refusal left an installation")
PARENT = os.path.join(INSTALL_ROOT, "parent-skill")
os.makedirs(PARENT)
open(os.path.join(PARENT, "SKILL.md"), "w").write("# another skill\n")
r_ = install(os.path.join(INSTALL_ROOT, "child-inst"),
             "--skill-dir", os.path.join(PARENT, "optmem"))
check(r_.returncode == 1 and "already a skill directory" in r_.stderr,
      "a skill nested inside another skill was accepted: " + r_.stderr)

# -- skill ownership: custom or modified skills are refused, intact --------
CUSTOM = os.path.join(INSTALL_ROOT, "custom-skill")
os.makedirs(CUSTOM)
open(os.path.join(CUSTOM, "SKILL.md"), "w", encoding="utf-8").write("# my own skill\n")
CUSTOM_INST = os.path.join(INSTALL_ROOT, "custom-skill-inst")
r_ = install(CUSTOM_INST, "--skill-dir", CUSTOM)
check(r_.returncode == 1 and "custom or modified" in r_.stderr,
      "a custom SKILL.md was overwritten: " + r_.stderr)
check(not os.path.exists(CUSTOM_INST)
      and open(os.path.join(CUSTOM, "SKILL.md")).read() == "# my own skill\n",
      "a refused custom skill changed files")

# an owner JSON is not ownership: a custom body, a foreign command, a foreign
# memory, or a broken hash must all be refused before the tool replaces
# anything
MODIFIED = os.path.join(INSTALL_ROOT, "modified-skill")
os.makedirs(MODIFIED)
FOREIGN_INST = os.path.join(INSTALL_ROOT, "foreign-owner-inst")
r_ = install(FOREIGN_INST, "--skill-dir", MODIFIED)
check(r_.returncode == 0, "foreign-owner fixture install failed: " + r_.stderr)
MOD_PATH = os.path.join(MODIFIED, "SKILL.md")
MOD_TEXT = open(MOD_PATH, encoding="utf-8").read()
mod_body, _, mod_tail = MOD_TEXT.rpartition("\n<!-- optmem-managed: ")
mod_owner = json.loads(mod_tail[:-5])
MOD_BEFORE = file_map(FOREIGN_INST)
MOD_STATE = open(MOD_PATH, "rb").read()
# (0) a user edit without a new owner hash is refused against its own target
open(MOD_PATH, "w", encoding="utf-8").write(
    mod_body + "\nA note the user added.\n" + mod_tail)
r_ = install(FOREIGN_INST, "--skill-dir", MODIFIED)
check(r_.returncode == 1 and "custom or modified" in r_.stderr,
      "a modified skill body was overwritten: " + r_.stderr)
check(file_map(FOREIGN_INST) == MOD_BEFORE
      and open(MOD_PATH, "rb").read() != MOD_STATE,
      "a refused modified skill changed the installation")
open(MOD_PATH, "wb").write(MOD_STATE)
# (1) a validly hashed body offered to a different installation, and a broken
# hash offered to ours, are both owner mismatches
edited = mod_body + "\nA note the user added.\n"
edited_owner = dict(mod_owner, sha256=hashlib.sha256(edited.encode()).hexdigest())
EDITED_STATE = (edited + "\n<!-- optmem-managed: "
                + json.dumps(edited_owner) + " -->\n").encode()
open(MOD_PATH, "wb").write(EDITED_STATE)
# the same edited skill is refused for a different target before any change
r_ = install(FOREIGN_INST + "-edited", "--skill-dir", MODIFIED)
check(r_.returncode == 1 and "custom or modified" in r_.stderr,
      "an edited skill with a self-valid hash was accepted for another "
      "installation: " + r_.stderr)
check(not os.path.exists(FOREIGN_INST + "-edited")
      and open(MOD_PATH, "rb").read() == EDITED_STATE
      and file_map(FOREIGN_INST) == MOD_BEFORE,
      "a foreign-target refusal changed the installation or skill")
r_ = install(FOREIGN_INST, "--skill-dir", MODIFIED)
check(r_.returncode == 1 and "custom or modified" in r_.stderr
      and file_map(FOREIGN_INST) == MOD_BEFORE
      and open(MOD_PATH, "rb").read() == EDITED_STATE,
      "a modified skill with a recomputed self-hash was "
      "overwritten: " + r_.stdout + r_.stderr)
open(MOD_PATH, "wb").write(MOD_STATE)
# (2) a valid self-hash with a foreign command is refused
FOREIGN_SKILL = os.path.join(INSTALL_ROOT, "foreign-command-skill")
os.makedirs(FOREIGN_SKILL)
foreign_text = cli.render_skill(shlex.quote("/elsewhere/memo"), "/elsewhere/memory")
open(os.path.join(FOREIGN_SKILL, "SKILL.md"), "w", encoding="utf-8").write(foreign_text)
r_ = install(FOREIGN_INST + "-2", "--skill-dir", FOREIGN_SKILL)
check(r_.returncode == 1 and "custom or modified" in r_.stderr,
      "a skill from another installation was overwritten: " + r_.stderr)
check(not os.path.exists(FOREIGN_INST + "-2"),
      "a foreign-owner refusal left an installation")
# (3) a broken hash is refused even with the right command and memory
BROKEN = os.path.join(INSTALL_ROOT, "broken-hash-skill")
os.makedirs(BROKEN)
broken_owner = dict(mod_owner, sha256="0" * 64)
open(os.path.join(BROKEN, "SKILL.md"), "w", encoding="utf-8").write(
    mod_body + "\n<!-- optmem-managed: " + json.dumps(broken_owner) + " -->\n")
r_ = install(FOREIGN_INST + "-3", "--skill-dir", BROKEN)
check(r_.returncode == 1 and "custom or modified" in r_.stderr,
      "a broken owner hash was accepted: " + r_.stderr)
check(not os.path.exists(FOREIGN_INST + "-3"),
      "a broken hash refusal left an installation")
# (4) a valid hash with no command or memory is not an ownership claim
SHAPELESS = os.path.join(INSTALL_ROOT, "shapeless-owner-skill")
os.makedirs(SHAPELESS)
shape_owner = {"version": 1,
               "sha256": hashlib.sha256(mod_body.encode()).hexdigest()}
open(os.path.join(SHAPELESS, "SKILL.md"), "w", encoding="utf-8").write(
    mod_body + "\n<!-- optmem-managed: " + json.dumps(shape_owner) + " -->\n")
r_ = install(FOREIGN_INST + "-4", "--skill-dir", SHAPELESS)
check(r_.returncode == 1 and "custom or modified" in r_.stderr,
      "an owner without command/memory was accepted: " + r_.stderr)
check(not os.path.exists(FOREIGN_INST + "-4"),
      "a shapeless-owner refusal left an installation")

# -- updates: skill untouched without --skill-dir, config/memory preserved --
UPDATE_SRC = os.path.join(INSTALL_ROOT, "updated-memo")
open(UPDATE_SRC, "wb").write(open(MEMO, "rb").read() + b"\n# test update marker\n")
UPDATE_SUM = hashlib.sha256(open(UPDATE_SRC, "rb").read()).hexdigest()
A_SKILL_BYTES = open(os.path.join(A_SKILL, "SKILL.md"), "rb").read()
A_CONFIG = open(os.path.join(A, "memory", "config"), "rb").read()
A_LOG = open(os.path.join(A, "memory", "LOG.txt"), "rb").read()
A_MEM_SNAP = fingerprint(os.path.join(A, "memory"))
# a rejected source must not touch a healthy installation or its skill
BAD_SRC = os.path.join(INSTALL_ROOT, "bad-update-source")
open(BAD_SRC, "w", encoding="utf-8").write("not python at all !!!\n")
A_BEFORE_BAD = file_map(A)
r_ = install(A, "--skill-dir", A_SKILL, source=BAD_SRC)
check(r_.returncode == 1 and "invalid syntax" in r_.stderr,
      "a bad update source was accepted: " + r_.stdout + r_.stderr)
check(file_map(A) == A_BEFORE_BAD
      and open(os.path.join(A_SKILL, "SKILL.md"), "rb").read() == A_SKILL_BYTES,
      "a rejected source changed the installation or its skill")
NO_HELPER_SRC = os.path.join(INSTALL_ROOT, "no-helper-source")
open(NO_HELPER_SRC, "w", encoding="utf-8").write("value = 1\n")
r_ = install(A, "--skill-dir", A_SKILL, source=NO_HELPER_SRC)
check(r_.returncode == 1 and file_map(A) == A_BEFORE_BAD,
      "a source without install_tool changed the installation")
r_ = install(A, source=UPDATE_SRC)
check(r_.returncode == 0, "an update without --skill-dir failed: " + r_.stderr)
check(hashlib.sha256(open(os.path.join(A, "memo"), "rb").read()).hexdigest()
      == UPDATE_SUM, "an update did not replace the executable")
check(open(os.path.join(A_SKILL, "SKILL.md"), "rb").read() == A_SKILL_BYTES,
      "an update without --skill-dir touched the skill")
check(open(os.path.join(A, "memory", "config"), "rb").read() == A_CONFIG
      and open(os.path.join(A, "memory", "LOG.txt"), "rb").read() == A_LOG
      and fingerprint(os.path.join(A, "memory")) == A_MEM_SNAP,
      "an update changed memories or config")
check(open(os.path.join(A, ".optmem-install"), encoding="utf-8").read()
      == '{"version": 1}\n', "an update changed the marker")
# update with the same owner and a pristine generated skill is allowed
r_ = install(A, "--skill-dir", A_SKILL, source=UPDATE_SRC)
check(r_.returncode == 0, "a same-owner pristine skill update was refused: " + r_.stderr)
check(cli.skill_owner(open(os.path.join(A_SKILL, "SKILL.md"), encoding="utf-8").read())
      is not None, "an allowed update left an unowned skill")
check(not pycache_dirs(A) and not pycache_dirs(A_SKILL),
      "an update left __pycache__ behind")

# a pristine store is byte-for-byte stable across an update, and a custom
# config survives in a different instance
PRISTINE = os.path.join(INSTALL_ROOT, "pristine-instance")
r_ = install(PRISTINE)
check(r_.returncode == 0, "pristine-instance install failed: " + r_.stderr)
PRISTINE_SNAP = fingerprint(os.path.join(PRISTINE, "memory"))
PRISTINE_MARKER = open(os.path.join(PRISTINE, ".optmem-install"), "rb").read()
r_ = install(PRISTINE, source=UPDATE_SRC)
check(r_.returncode == 0, "pristine-instance update failed: " + r_.stderr)
check(fingerprint(os.path.join(PRISTINE, "memory")) == PRISTINE_SNAP
      and open(os.path.join(PRISTINE, ".optmem-install"), "rb").read()
      == PRISTINE_MARKER,
      "an update rewrote a pristine store or marker")
check(os.path.getsize(os.path.join(PRISTINE, "memory", "LOG.txt")) == 0,
      "a pristine store has memories after an update")
B_CONFIG = open(os.path.join(B, "memory", "config"), "rb").read()
B_SNAP = fingerprint(os.path.join(B, "memory"))
r_ = install(B, source=UPDATE_SRC)
check(r_.returncode == 0, "instance B update failed: " + r_.stderr)
check(open(os.path.join(B, "memory", "config"), "rb").read() == B_CONFIG,
      "an update rewrote instance B's config")
check(fingerprint(os.path.join(B, "memory")) == B_SNAP,
      "an update modified instance B's memory")

# -- prompt-only updates preserve the old generated-skill hash ------------
PROMPT_ROOT = tempfile.mkdtemp(prefix="optmem-prompt-")
try:
    PROMPT_SRC = [template_variant(os.path.join(PROMPT_ROOT, "memo-v%d" % n),
                                   "v%d" % n) for n in (1, 2, 3)]
    PROMPT_INST = os.path.join(PROMPT_ROOT, "instance")
    PROMPT_SKILL = os.path.join(PROMPT_ROOT, "skill")
    r_ = install(PROMPT_INST, "--skill-dir", PROMPT_SKILL, source=PROMPT_SRC[0])
    check(r_.returncode == 0, "prompt v1 install failed: " + r_.stderr)
    prompt_old = open(os.path.join(PROMPT_SKILL, "SKILL.md"), "rb").read()
    prompt_old_hash = cli.skill_owner(prompt_old.decode())["sha256"]
    prompt_map = os.path.join(PROMPT_INST, ".optmem-skills")
    check(json.load(open(prompt_map, encoding="utf-8"))
          == {PROMPT_SKILL: [prompt_old_hash]},
          "the first prompt install did not record its skill hash")
    prompt_record = json.load(open(prompt_map, encoding="utf-8"))
    check(list(prompt_record) == [os.path.abspath(PROMPT_SKILL)]
          and all(re.fullmatch(r"[0-9a-f]{64}", h)
                  for h in prompt_record[PROMPT_SKILL]),
          "the skill ownership map is not an absolute folder to hashes: %r"
          % (prompt_record,))
    for version, src in zip((2, 3), PROMPT_SRC[1:]):
        r_ = install(PROMPT_INST, source=src)
        check(r_.returncode == 0,
              "prompt v%d tool-only update failed: %s" % (version, r_.stderr))
        check(open(os.path.join(PROMPT_INST, "memo"), "rb").read()
              == open(src, "rb").read(),
              "prompt v%d did not replace the executable" % version)
        check(open(os.path.join(PROMPT_SKILL, "SKILL.md"), "rb").read()
              == prompt_old,
              "prompt v%d changed the skill without --skill-dir" % version)
        check(json.load(open(prompt_map, encoding="utf-8"))
              == {PROMPT_SKILL: [prompt_old_hash]},
              "prompt v%d forgot the old skill hash" % version)
    r_ = install(PROMPT_INST, "--skill-dir", PROMPT_SKILL, source=PROMPT_SRC[2])
    check(r_.returncode == 0,
          "upgrading a two-version-old skill failed: " + r_.stderr)
    prompt_new = open(os.path.join(PROMPT_SKILL, "SKILL.md"), "rb").read()
    prompt_new_hash = cli.skill_owner(prompt_new.decode())["sha256"]
    check(prompt_new != prompt_old and prompt_new_hash != prompt_old_hash,
          "the prompt upgrade did not publish the new skill")
    check(json.load(open(prompt_map, encoding="utf-8"))
          == {PROMPT_SKILL: sorted([prompt_old_hash, prompt_new_hash])},
          "the prompt upgrade did not keep both known hashes")
    # a user body change with a recomputed self-hash is still not owned
    prompt_body, _, prompt_tail = prompt_new.decode().rpartition(
        "\n<!-- optmem-managed: ")
    prompt_owner = json.loads(prompt_tail[:-5])
    prompt_edited = prompt_body + "\nA later user edit.\n"
    prompt_bad = (prompt_edited + "\n<!-- optmem-managed: "
                  + json.dumps(dict(prompt_owner, sha256=hashlib.sha256(
                      prompt_edited.encode()).hexdigest())) + " -->\n").encode()
    open(os.path.join(PROMPT_SKILL, "SKILL.md"), "wb").write(prompt_bad)
    prompt_before = file_map(PROMPT_INST)
    r_ = install(PROMPT_INST, "--skill-dir", PROMPT_SKILL, source=PROMPT_SRC[2])
    check(r_.returncode == 1 and "custom or modified" in r_.stderr,
          "a recomputed self-hash was accepted after prompt updates: " + r_.stderr)
    check(file_map(PROMPT_INST) == prompt_before
          and open(os.path.join(PROMPT_SKILL, "SKILL.md"), "rb").read()
          == prompt_bad,
          "a rejected edited skill changed the installation or the file")
finally:
    shutil.rmtree(PROMPT_ROOT)

# the core helper, loaded from a staged source, owns its own globals
RUNPY_SRC = os.path.join(INSTALL_ROOT, "runpy-memo-source")
shutil.copy2(MEMO, RUNPY_SRC)
RUN_INSTALLER = runpy.run_path(RUNPY_SRC, run_name="optmem_installer")
check("install_tool" in RUN_INSTALLER and "render_skill" in RUN_INSTALLER
      and "skill_owner" in RUN_INSTALLER and "installation_managed" in RUN_INSTALLER,
      "the memo does not expose its installer helpers")

# -- a failed final skill publish keeps the prior and next hashes ----------
RETRY_ROOT = tempfile.mkdtemp(prefix="optmem-retry-")
try:
    RETRY_V1 = template_variant(os.path.join(RETRY_ROOT, "memo-v1"), "v1")
    RETRY_V2 = template_variant(os.path.join(RETRY_ROOT, "memo-v2"), "v2")
    RETRY_INST = os.path.join(RETRY_ROOT, "instance")
    RETRY_SKILL = os.path.join(RETRY_ROOT, "skill")
    retry_run = runpy.run_path(RETRY_V1, run_name="optmem_retry_installer")
    helper = retry_run["install_tool"]
    helper_globals = helper.__globals__
    check(helper_globals["render_skill"] is retry_run["render_skill"]
          and callable(helper_globals["atomic_write"]),
          "the staged helper does not own the installer globals")
    with contextlib.redirect_stdout(io.StringIO()) as helper_out:
        helper(RETRY_V1, RETRY_INST, RETRY_SKILL)
    check("Installed %s" % os.path.join(RETRY_INST, "memo")
          in helper_out.getvalue(),
          "the direct helper did not report its install: " + helper_out.getvalue())
    retry_old = open(os.path.join(RETRY_SKILL, "SKILL.md"), "rb").read()
    retry_old_hash = cli.skill_owner(retry_old.decode())["sha256"]
    retry_map = os.path.join(RETRY_INST, ".optmem-skills")
    retry_user = os.path.join(RETRY_INST, "user-file")
    retry_user_skill = os.path.join(RETRY_SKILL, "user-file")
    open(retry_user, "wb").write(b"keep installation user file\n")
    open(retry_user_skill, "wb").write(b"keep skill user file\n")
    retry_next = runpy.run_path(RETRY_V2, run_name="optmem_retry_next")
    next_helper = retry_next["install_tool"]
    next_globals = next_helper.__globals__
    real_atomic = next_globals["atomic_write"]
    def fail_final_skill(path, data):
        if os.path.abspath(path) == os.path.join(os.path.abspath(RETRY_SKILL),
                                                 "SKILL.md"):
            raise OSError("simulated final skill publish failure")
        return real_atomic(path, data)
    next_globals["atomic_write"] = fail_final_skill
    try:
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                next_helper(RETRY_V2, RETRY_INST, RETRY_SKILL)
                retry_code = 0
            except OSError as e:
                retry_code, retry_error = 1, str(e)
    finally:
        next_globals["atomic_write"] = real_atomic
    check(retry_code == 1 and "simulated final skill" in retry_error,
          "the patched helper did not fail on the final skill publish")
    check(open(os.path.join(RETRY_INST, "memo"), "rb").read()
          == open(RETRY_V2, "rb").read(),
          "a failed skill publish did not leave the new executable")
    check(open(os.path.join(RETRY_SKILL, "SKILL.md"), "rb").read()
          == retry_old,
          "a failed skill publish replaced the old skill")
    retry_new_hash = retry_next["skill_owner"](
        retry_next["render_skill"](shlex.quote(os.path.join(RETRY_INST, "memo")),
                                   os.path.join(RETRY_INST, "memory")))["sha256"]
    retry_known = json.load(open(retry_map, encoding="utf-8"))[RETRY_SKILL]
    check(retry_known == sorted([retry_old_hash, retry_new_hash]),
          "a failed skill publish lost the prior or next hash: %r"
          % (retry_known,))
    check(open(retry_user, "rb").read() == b"keep installation user file\n"
          and open(retry_user_skill, "rb").read() == b"keep skill user file\n",
          "a failed skill publish overwrote a user file")
    check(not os.path.islink(os.path.join(RETRY_INST, ".optmem-install"))
          and open(os.path.join(RETRY_INST, ".optmem-install"), "rb").read()
          == b'{"version": 1}\n',
          "a failed skill publish damaged the installation marker")
    # the current installer retry is safe after the interrupted publish
    r_ = install(RETRY_INST, "--skill-dir", RETRY_SKILL)
    check(r_.returncode == 0, "the current installer retry failed: " + r_.stderr)
    retry_final = open(os.path.join(RETRY_SKILL, "SKILL.md"), "rb").read()
    retry_final_hash = cli.skill_owner(retry_final.decode())["sha256"]
    retry_after = json.load(open(retry_map, encoding="utf-8"))[RETRY_SKILL]
    check(retry_old_hash in retry_after and retry_final_hash in retry_after,
          "the installer retry lost an old or new known hash: %r"
          % (retry_after,))
    check(open(retry_user, "rb").read() == b"keep installation user file\n"
          and open(retry_user_skill, "rb").read() == b"keep skill user file\n",
          "the installer retry overwrote a user file")
    retry_memo_after = open(os.path.join(RETRY_INST, "memo"), "rb").read()
    retry_skill_after = open(os.path.join(RETRY_SKILL, "SKILL.md"), "rb").read()
    retry_map_after = json.load(open(retry_map, encoding="utf-8"))[RETRY_SKILL]

    # the next hash is recorded before the binary publish too: a failed
    # marker publish leaves the old tool and skill usable, and the retry sees
    # the hash of the source that never reached the executable
    RETRY_V3 = template_variant(os.path.join(RETRY_ROOT, "memo-v3"), "v3")
    retry_third = runpy.run_path(RETRY_V3, run_name="optmem_retry_third")
    third_helper = retry_third["install_tool"]
    third_globals = third_helper.__globals__
    real_third_atomic = third_globals["atomic_write"]
    def fail_marker_publish(path, data):
        if os.path.abspath(path) == os.path.join(os.path.abspath(RETRY_INST),
                                                 ".optmem-install"):
            raise OSError("simulated binary publish failure")
        return real_third_atomic(path, data)
    third_globals["atomic_write"] = fail_marker_publish
    try:
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                third_helper(RETRY_V3, RETRY_INST, RETRY_SKILL)
                third_code = 0
            except OSError as e:
                third_code, third_error = 1, str(e)
    finally:
        third_globals["atomic_write"] = real_third_atomic
    check(third_code == 1 and "simulated binary" in third_error,
          "the patched helper did not fail on the marker publish")
    third_hash = retry_third["skill_owner"](
        retry_third["render_skill"](shlex.quote(os.path.join(RETRY_INST, "memo")),
                                    os.path.join(RETRY_INST, "memory")))["sha256"]
    third_map = json.load(open(retry_map, encoding="utf-8"))[RETRY_SKILL]
    check(third_hash in third_map and all(h in third_map for h in retry_map_after),
          "a failed binary publish lost the next or an old hash: %r"
          % (third_map,))
    check(open(os.path.join(RETRY_INST, "memo"), "rb").read() == retry_memo_after
          and open(os.path.join(RETRY_SKILL, "SKILL.md"), "rb").read()
          == retry_skill_after,
          "a failed binary publish changed the tool or skill")
    check(not os.path.islink(os.path.join(RETRY_INST, ".optmem-install"))
          and open(os.path.join(RETRY_INST, ".optmem-install"), "rb").read()
          == b'{"version": 1}\n',
          "a failed binary publish damaged the marker")
    r_ = install(RETRY_INST, "--skill-dir", RETRY_SKILL)
    check(r_.returncode == 0,
          "the installer retry after a binary failure failed: " + r_.stderr)
    final_map = json.load(open(retry_map, encoding="utf-8"))[RETRY_SKILL]
    check(third_hash in final_map and retry_old_hash in final_map,
          "the retry after a binary failure lost a known hash: %r"
          % (final_map,))
    check(open(retry_user, "rb").read() == b"keep installation user file\n"
          and open(retry_user_skill, "rb").read() == b"keep skill user file\n",
          "the binary-failure retry overwrote a user file")
finally:
    shutil.rmtree(RETRY_ROOT)


# -- path+'.tmp' symlinks cannot redirect an atomic publish ----------------
LINK_ROOT = tempfile.mkdtemp(prefix="optmem-temp-link-")
try:
    CANARY = os.path.join(LINK_ROOT, "outside-canary")
    open(CANARY, "wb").write(b"outside canary stays intact\n")
    LINK_INST = os.path.join(LINK_ROOT, "instance")
    LINK_SKILL = os.path.join(LINK_ROOT, "skill")
    os.makedirs(LINK_INST)
    os.makedirs(LINK_SKILL)
    os.symlink(CANARY, os.path.join(LINK_INST, ".optmem-install.tmp"))
    os.symlink(CANARY, os.path.join(LINK_INST, ".optmem-skills.tmp"))
    r_ = install(LINK_INST, "--skill-dir", LINK_SKILL)
    check(r_.returncode == 0, "install beside temp symlinks failed: " + r_.stderr)
    check(open(CANARY, "rb").read() == b"outside canary stays intact\n",
          "an install wrote through .optmem-install.tmp")
    check(open(os.path.join(LINK_INST, ".optmem-install"), "rb").read()
          == b'{"version": 1}\n'
          and not os.path.islink(os.path.join(LINK_INST, ".optmem-install")),
          "an install replaced the marker with a temp symlink")
    check(not os.path.islink(os.path.join(LINK_INST, ".optmem-skills")),
          "an install replaced the skill map with a temp symlink")
    check(os.path.isfile(os.path.join(LINK_SKILL, "SKILL.md"))
          and not os.path.islink(os.path.join(LINK_SKILL, "SKILL.md")),
          "an install made SKILL.md a symlink")
    os.symlink(CANARY, os.path.join(LINK_SKILL, "SKILL.md.tmp"))
    LINK_UPDATE = os.path.join(LINK_ROOT, "memo-update")
    open(LINK_UPDATE, "wb").write(open(MEMO, "rb").read() + b"\n# update\n")
    r_ = install(LINK_INST, "--skill-dir", LINK_SKILL, source=LINK_UPDATE)
    check(r_.returncode == 0, "update beside temp symlinks failed: " + r_.stderr)
    check(open(CANARY, "rb").read() == b"outside canary stays intact\n",
          "an update wrote through SKILL.md.tmp or a marker temp link")
    check(open(os.path.join(LINK_INST, ".optmem-install"), "rb").read()
          == b'{"version": 1}\n'
          and not os.path.islink(os.path.join(LINK_INST, ".optmem-install")),
          "an update replaced the main marker with a symlink")
    check(not os.path.islink(os.path.join(LINK_INST, ".optmem-skills")),
          "an update replaced the skill map with a symlink")
    check(open(os.path.join(LINK_INST, "memo"), "rb").read()
          == open(LINK_UPDATE, "rb").read()
          and os.path.isfile(os.path.join(LINK_SKILL, "SKILL.md"))
          and not os.path.islink(os.path.join(LINK_SKILL, "SKILL.md")),
          "an update published the executable or skill through a temp symlink")
    for planted in (os.path.join(LINK_INST, ".optmem-install.tmp"),
                    os.path.join(LINK_INST, ".optmem-skills.tmp"),
                    os.path.join(LINK_SKILL, "SKILL.md.tmp")):
        check(os.path.islink(planted) and os.readlink(planted) == CANARY,
              "an installer changed a planted temp symlink: " + planted)
finally:
    shutil.rmtree(LINK_ROOT)


# -- install-tool core: relative paths, realpath, and explicit skill dir ---
REL_ROOT = tempfile.mkdtemp(prefix="optmem-rel-")
REL_SRC = os.path.join(REL_ROOT, "source-memo")
shutil.copy2(MEMO, REL_SRC)
OLD_CWD = os.getcwd()
try:
    os.chdir(REL_ROOT)
    with contextlib.redirect_stdout(io.StringIO()) as rel_out:
        RUN_INSTALLER["install_tool"](REL_SRC, "relative instance",
                                       "relative skills/it's here")
    rel_exe = os.path.join(os.path.realpath("relative instance"), "memo")
    rel_data = os.path.join(os.path.realpath("relative instance"), "memory")
    check("Installed %s" % rel_exe in rel_out.getvalue(),
          "install_tool did not realpath its install dir: " + rel_out.getvalue())
    rel_skill = os.path.abspath("relative skills/it's here")
    check(os.path.exists(os.path.join(rel_skill, "SKILL.md")),
          "install_tool did not abspath its skill dir")
    check(cli.skill_owner(open(os.path.join(rel_skill, "SKILL.md")).read())
          ["command"] == shlex.quote(rel_exe),
          "install_tool wrote a skill with the wrong executable")
    check(RUN_INSTALLER["render_skill"](shlex.quote(rel_exe), rel_data)
          == open(os.path.join(rel_skill, "SKILL.md")).read(),
          "install_tool did not write exactly render_skill")
    # install_tool replaces files but does not create or initialize a store;
    # only the explicit init below publishes the sibling memory
    check(not os.path.exists(rel_data),
          "install_tool created a memory store without init")
    rel_foreign = os.path.join(REL_ROOT, "rel-foreign")
    r_ = subprocess.run(command(rel_exe, "skill"),
                        env=dict(INSTALL_ENV, MEMORY_DIR=rel_foreign),
                        capture_output=True, text=True)
    check(r_.returncode == 0 and "name: optmem" in r_.stdout,
          "an installed skill before init failed: " + r_.stderr)
    check(not os.path.exists(rel_data) and not os.path.exists(rel_foreign),
          "skill generation before init created a store")
    r_ = subprocess.run(command(rel_exe, "init"),
                        env=dict(INSTALL_ENV, MEMORY_DIR=rel_foreign),
                        capture_output=True, text=True)
    check(r_.returncode == 0 and os.path.exists(os.path.join(rel_data, "config")),
          "installed init did not create the sibling memory: " + r_.stderr)
    check(not os.path.exists(rel_foreign),
          "installed init used an ambient MEMORY_DIR")
    check(not pycache_dirs(REL_ROOT), "runpy or install_tool left __pycache__")
finally:
    os.chdir(OLD_CWD)
shutil.rmtree(REL_ROOT)

# install_tool refuses a line break in a path before creating anything; the
# shell installer copies to a temp file first, so its own exit path is
# covered by the core helper test below
NL_ROOT = tempfile.mkdtemp(prefix="optmem-newline-")
try:
    nl_source = os.path.join(NL_ROOT, "source")
    shutil.copy2(MEMO, nl_source)
    saved_stdout = io.StringIO()
    with contextlib.redirect_stdout(saved_stdout), contextlib.redirect_stderr(io.StringIO()):
        try:
            RUN_INSTALLER["install_tool"](nl_source,
                                          os.path.join(NL_ROOT, "line\nbreak"))
            nl_code = 0
        except SystemExit as e:
            nl_code = e.code if isinstance(e.code, int) else 0
    check(nl_code == 1 and not os.path.exists(os.path.join(NL_ROOT, "line\nbreak")),
          "install_tool accepted a line break in the install path")
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            RUN_INSTALLER["install_tool"](nl_source,
                                          os.path.join(NL_ROOT, "ok-inst"),
                                          os.path.join(NL_ROOT, "line\nskill"))
            nl_code = 0
        except SystemExit as e:
            nl_code = e.code if isinstance(e.code, int) else 0
    check(nl_code == 1 and not os.path.exists(os.path.join(NL_ROOT, "line\nskill")),
          "install_tool accepted a line break in the skill path")
finally:
    shutil.rmtree(NL_ROOT)


# -- installation_managed and memory_dir helpers --------------------------
own = (cli.EXECUTABLE, cli.ME, cli.memory_dir())
try:
    check(own[0] == os.path.realpath(MEMO), "EXECUTABLE is not the real script")
    check(own[1] == shlex.quote(own[0]),
          "ME is not the shell-quoted real script at load time")
    managed_dir = tempfile.mkdtemp(prefix="optmem-managed-probe-")
    shutil.copy2(MEMO, os.path.join(managed_dir, "memo"))
    open(os.path.join(managed_dir, ".optmem-install"), "w").write('{"version": 1}\n')
    cli.EXECUTABLE = os.path.join(managed_dir, "memo")
    os.environ["MEMORY_DIR"] = os.path.join(INSTALL_ROOT, "ignored")
    check(cli.installation_managed() is True, "a valid marker was not managed")
    check(cli.installation_managed(managed_dir) is True,
          "installation_managed ignored its explicit directory")
    check(cli.memory_dir() == os.path.join(managed_dir, "memory"),
          "a managed store did not ignore MEMORY_DIR")
    cli.bind_command(cli.memory_dir())
    check(cli.ME == shlex.quote(cli.EXECUTABLE),
          "a managed bind_command added an env prefix")
    os.unlink(os.path.join(managed_dir, ".optmem-install"))
    check(cli.installation_managed() is False, "a missing marker was managed")
    check(cli.installation_managed(managed_dir) is False,
          "installation_managed found a missing marker in an explicit directory")
    check(cli.memory_dir() == os.path.join(INSTALL_ROOT, "ignored"),
          "an unmanaged store ignored MEMORY_DIR")
    cli.bind_command(cli.memory_dir())
    check(cli.ME == "MEMORY_DIR=%s %s" % (shlex.quote(os.path.join(INSTALL_ROOT, "ignored")),
                                            shlex.quote(cli.EXECUTABLE)),
          "an unmanaged bind_command did not pin its store: %r" % cli.ME)
    os.environ.pop("MEMORY_DIR", None)
    cli.bind_command(cli.memory_dir())
    check(cli.ME == "MEMORY_DIR=%s %s" %
          (shlex.quote(os.path.join(os.path.dirname(cli.EXECUTABLE), "memory")),
           shlex.quote(cli.EXECUTABLE)),
          "an unset override was not pinned as the absolute default: %r" % cli.ME)
finally:
    cli.EXECUTABLE, cli.ME = own[0], own[1]
    os.environ.pop("MEMORY_DIR", None)
    shutil.rmtree(managed_dir)

# -- migration and config stay independent between installed instances ----
# A legacy store must remain legacy when a managed instance runs beside it:
# the marker pins only that instance's own memory directory.
LEGACY = os.path.join(INSTALL_ROOT, "legacy-store")
os.makedirs(LEGACY)
for i in range(5):
    r_ = run("note", "legacy %d" % i, store=LEGACY)
    check(r_.returncode == 0, "legacy fixture note failed: " + r_.stderr)
check(os.path.getsize(os.path.join(LEGACY, "LOG.txt")) == 5 * 320,
      "a legacy fixture is not format 1")
r_ = subprocess.run(command(A_MEMO, "wake"), env=dict(INSTALL_ENV, MEMORY_DIR=LEGACY),
                    capture_output=True, text=True)
check(r_.returncode == 0 and "alpha in A" in r_.stdout
      and "legacy 0" not in r_.stdout,
      "a managed instance read a legacy store through MEMORY_DIR")
check(os.path.getsize(os.path.join(LEGACY, "LOG.txt")) == 5 * 320,
      "a managed instance changed a legacy store")
# the unmarked copy migrates only the store it is told to use
r_ = subprocess.run(command(UNMARKED_MEMO, "config"),
                    env=dict(INSTALL_ENV, MEMORY_DIR=LEGACY), capture_output=True, text=True)
check(r_.returncode == 0 and "ENTRY_CHARS   280" in r_.stdout,
      "an unmarked copy lost MEMORY_DIR: " + r_.stdout + r_.stderr)

# an installed copy can own a legacy format-1 store: a safe update preserves
# its bytes, and its explicit migration changes no other instance
LEG_I = os.path.join(INSTALL_ROOT, "legacy-instance")
r_ = install(LEG_I)
check(r_.returncode == 0, "legacy-instance install failed: " + r_.stderr)
shutil.rmtree(os.path.join(LEG_I, "memory"))
LEG_I_STORE = os.path.join(INSTALL_ROOT, "legacy-instance-memory")
LEG_I_STORE_TMP = tempfile.mkdtemp(prefix="optmem-legacy-owned-")
for i in range(3):
    r_ = run("note", "legacy managed %d" % i, store=LEG_I_STORE_TMP)
    check(r_.returncode == 0, "legacy-instance note failed: " + r_.stderr)
r_ = run("delete", "1", store=LEG_I_STORE_TMP)
check(r_.returncode == 0, "legacy-instance delete failed: " + r_.stderr)
r_ = run("config", "WAKE_LINES=7", store=LEG_I_STORE_TMP)
check(r_.returncode == 0, "legacy-instance config failed: " + r_.stderr)
shutil.move(LEG_I_STORE_TMP, LEG_I_STORE)
shutil.move(LEG_I_STORE, os.path.join(LEG_I, "memory"))
LEG_I_MEMO = os.path.join(LEG_I, "memo")
LEG_I_SNAP = fingerprint(os.path.join(LEG_I, "memory"))
r_ = subprocess.run(command(LEG_I_MEMO, "recall", "legacy managed"),
                    env=INSTALL_ENV, capture_output=True, text=True)
check(r_.returncode == 0 and "#0 " in r_.stdout and "#2 " in r_.stdout
      and "legacy managed 1" not in r_.stdout,
      "an installed copy could not read its own legacy store: "
      + r_.stdout + r_.stderr)
r_ = install(LEG_I, source=UPDATE_SRC)
check(r_.returncode == 0, "updating an installed legacy store failed: " + r_.stderr)
check(fingerprint(os.path.join(LEG_I, "memory")) == LEG_I_SNAP,
      "an update rewrote an installed legacy store")
A_FROZEN = fingerprint(os.path.join(A, "memory"))
B_FROZEN = fingerprint(os.path.join(B, "memory"))
r_ = subprocess.run(command(LEG_I_MEMO, "migrate"), env=INSTALL_ENV,
                    capture_output=True, text=True)
check(r_.returncode == 0 and "Migrated to format 2" in r_.stdout,
      "an installed legacy store did not migrate: " + r_.stdout + r_.stderr)
check(cli.read_format(os.path.join(LEG_I, "memory")) == 2
      and os.path.getsize(os.path.join(LEG_I, "memory", "LOG.txt")) == 3 * 2112,
      "migration did not produce a format-2 store")
check("legacy managed 0" in
      subprocess.run(command(LEG_I_MEMO, "recall", "legacy managed 0"),
                     env=INSTALL_ENV, capture_output=True, text=True).stdout,
      "migration lost the installed legacy memories")
check(re.search(r"^  WAKE_LINES\s+= 7\b",
                open(os.path.join(LEG_I, "memory", "config"), encoding="utf-8").read(),
                re.M), "migration lost the installed legacy config override")
check(fingerprint(os.path.join(A, "memory")) == A_FROZEN
      and fingerprint(os.path.join(B, "memory")) == B_FROZEN,
      "one instance's migration changed another instance")


# -- activation policy: the full template says exactly what may run --------
TEMPLATE = cli.TEMPLATE.format(memo=shlex.quote(A_MEMO), data=os.path.join(A, "memory"))
for phrase in ("available only after explicit user activation in this session",
               "When the user invokes this skill or asks you to read these instructions",
               "activate OptMem for the current session",
               "Do not carry activation into a new session",
               "Run `%s wake` immediately" % shlex.quote(A_MEMO),
               "Follow every pagination command until the tool",
               "prints `You are awake.`",
               "If reading requires summaries, complete the printed",
               "Copy every printed command in full, including its revision number",
               "Use `%s recall <regex>`" % shlex.quote(A_MEMO),
               "Use `%s zoom <lo>-<hi>`" % shlex.quote(A_MEMO),
               "without separate approval",
               "Create missing summaries with `%s nap`" % shlex.quote(A_MEMO),
               "Summary maintenance is allowed after activation and needs no separate approval",
               "It is not a\nstrictly read-only operation",
               "Activation does not authorize adding, importing, editing, or deleting memories",
               "Run `note`, `import`, `edit`, or `delete` only for a user's explicit request or\napproval covering that specific change",
               "already authorizes the stated addition",
               "Do not automatically save facts learned during work",
               "Do not interpret a request to read memory as permission to change its contents",
               "After an approved change, complete any summary maintenance",
               "Do not change sizes, discard summaries with `forget`, initialize another store,\nor migrate storage without a user's explicit request or approval",
               "Never edit memory files directly",
               "Use only this installation's command path",
               "Do not read or write another installation's memories unless the user requests it",
               "These are agent instructions, not a technical permission or security boundary"):
    check(phrase in TEMPLATE, "the activation policy is missing: %r" % phrase)
check("mandatory" not in TEMPLATE.lower(), "the policy still mandates startup")
check("subagent" not in TEMPLATE.lower(), "the policy still bans subagents")
check("wake" not in TEMPLATE.split("### Change memories")[1].split("###")[0],
      "the writing section still orders a wake")
# reading and maintenance are automatic after activation; writing is not
read_part = TEMPLATE.split("### Read and maintain")[1].split("### Change memories")[0]
write_part = TEMPLATE.split("### Change memories")[1]
check("wake" not in write_part.split("###")[0],
      "the writing section requires an automatic wake")
check("nap" in read_part and "no separate approval" in read_part,
      "maintenance is not automatic after activation")
for forbidden in ("Do not activate it at session startup",
                  "before any other tool call", "every session", "must run",
                  "first command", "Skip everything", "Don't run memo"):
    check(forbidden not in TEMPLATE, "the policy retained %r" % forbidden)
check("explicit request or\napproval" in write_part
      or "explicit request or approval" in " ".join(write_part.split()),
      "writing lacks an explicit approval rule")

# an explicit writing request is already authorization: no extra flag or
# confirmation text is inserted by any command
r_ = subprocess.run(command(A_MEMO, "note", "explicitly requested fact"),
                    env=INSTALL_ENV, capture_output=True, text=True)
check(r_.returncode == 0 and "approval" not in r_.stdout.lower()
      and "confirm" not in r_.stdout.lower()
      and "--yes" not in r_.stdout,
      "a directly requested write asked for another approval: " + r_.stdout)
check("auth" not in r_.stdout.lower(), "a requested write added an auth flag")
IMPORT_FILE = os.path.join(INSTALL_ROOT, "explicit-import.txt")
open(IMPORT_FILE, "w", encoding="utf-8").write(
    "2099-01-01 an explicitly requested imported fact\n")
for args in (["note", "another explicitly requested fact"],
             ["import", IMPORT_FILE],
             ["edit", "0", "an explicitly requested correction"],
             ["delete", "1"]):
    r_ = subprocess.run(command(A_MEMO) + args, env=INSTALL_ENV,
                        capture_output=True, text=True)
    check(r_.returncode == 0
          and not any(word in r_.stdout.lower()
                      for word in ("approval", "confirm", "auth", "--yes")),
          "%s inserted an approval or auth flag: %s"
          % (args[0], r_.stdout + r_.stderr))

# -- global agent and harness files are untouched -------------------------
GUARD_HOME = tempfile.mkdtemp(prefix="optmem-guard-home-")
for name in ("AGENTS.md", "CLAUDE.md", ".claude", ".config"):
    path = os.path.join(GUARD_HOME, name)
    os.makedirs(os.path.dirname(path) or GUARD_HOME, exist_ok=True)
    if name.startswith("."):
        os.makedirs(path, exist_ok=True)
        open(os.path.join(path, "settings.json"), "w").write('{"keep": true}\n')
    else:
        open(path, "w").write("# keep me\n")
GUARD_BEFORE = file_map(GUARD_HOME)
r_ = install(os.path.join(GUARD_HOME, ".optmem"), "--skill-dir",
             os.path.join(GUARD_HOME, ".agents", "skills", "optmem"),
             home=GUARD_HOME)
check(r_.returncode == 0, "a guarded install failed: " + r_.stderr)
GUARD_AFTER = file_map(GUARD_HOME)
for name in ("AGENTS.md", "CLAUDE.md", ".claude/settings.json",
             ".config/settings.json"):
    check(GUARD_AFTER.get(name) == GUARD_BEFORE.get(name),
          "install changed global file %s" % name)
check(set(GUARD_AFTER) - set(GUARD_BEFORE)
      == {".optmem/.optmem-install", ".optmem/.optmem-skills", ".optmem/memo",
          ".optmem/memory/.format", ".optmem/memory/LOG.txt",
          ".optmem/memory/config", ".agents/skills/optmem/SKILL.md"},
      "install created unexpected global files: %r"
      % (set(GUARD_AFTER) - set(GUARD_BEFORE),))
shutil.rmtree(GUARD_HOME)

# no test may leave bytecode anywhere under the repo or the fixtures
check(not os.path.exists(os.path.join(HERE, ".optmem-install"))
      and not os.path.exists(os.path.join(HERE, "memory")),
      "a test touched the repo's real default store")
check(not pycache_dirs(HERE), "the repo gained __pycache__")
check(not pycache_dirs(INSTALL_ROOT), "an installation gained __pycache__")

shutil.rmtree(INSTALL_ROOT)

shutil.rmtree(d2)
shutil.rmtree(d)
print("\n%d passed, %d failed" % (ok, fail))
sys.exit(1 if fail else 0)
