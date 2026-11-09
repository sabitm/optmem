#!/usr/bin/env python3
"""OptMem invariants, checked against a synthetic life of 5000 memories.

Uses a fake compressor (join + truncate) so the run is deterministic and free.
"""

import contextlib
import datetime
import errno
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.realpath(__file__))
MEMO = os.path.join(HERE, "memo")
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
            ("ENTRY_CHARS", "WAKE_LINES", "PART_CHARS", "PART_LINES")}

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
    return Result(code, out.getvalue(), err.getvalue())


def nap_id(out):
    """The block id from the command a nap prompt offers."""
    m = re.search(r"memo nap (\d+)-(\d+)", out)
    return "%s-%s" % m.groups() if m else None


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
# prints the paste block, and is idempotent
fresh = {k: v for k, v in os.environ.items() if k != "MEMORY_DIR"}
fresh["HOME"] = tempfile.mkdtemp()
noenv = subprocess.run(memo + ["wake"], capture_output=True, text=True, env=fresh)
check(noenv.returncode == 1 and "memo init" in noenv.stderr,
      "with no MEMORY_DIR and no memory, wake must point at init")
init = subprocess.run(memo + ["init"], capture_output=True, text=True, env=fresh)
check(init.returncode == 0 and "## Memory" in init.stdout
      and "You are a" in init.stdout, "init must print the AGENTS.md block")
check(os.path.exists(os.path.join(fresh["HOME"], ".optmem", "memory", "config")),
      "init must create ~/.optmem/memory with its config")
again = subprocess.run(memo + ["init"], capture_output=True, text=True, env=fresh)
check(again.returncode == 0 and "Found" in again.stdout, "init must be idempotent")
woke = subprocess.run(memo + ["wake"], capture_output=True, text=True, env=fresh)
check(woke.returncode == 0 and "You are awake." in woke.stdout,
      "after init, wake must work with zero configuration")

# Every command the tool prints must RUN on the machine it printed it on.
# `curl | sh` puts nothing on PATH, so a bare `memo nap ...` would not: the
# whole loop (note -> merge prompt -> nap) dies on `command not found`.
# Nix has no python3 in /usr/bin: keep the interpreter's own directory on
# PATH, but never the store's directory -- the order uses the memo path.
bare = dict(fresh, PATH=os.pathsep.join([os.path.dirname(sys.executable),
                                         "/usr/bin", "/bin"]))
subprocess.run(memo + ["note", "the first thing that happened"], env=bare,
               capture_output=True)
asked = subprocess.run(memo + ["note", "the second thing that happened"],
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
    r_ = subprocess.run(memo + c, capture_output=True, text=True, env=fresh)
    check(r_.returncode == 1 and "config line" in r_.stderr
          and "WAKE_LNES" in r_.stderr,
          "a typo in config does not say where it is: " + r_.stderr)
open(badcfg, "w").write("")

# the filesystem is the one thing the tool does not control: report it in the
# tool's own voice, never as a Python traceback
r_ = subprocess.run(memo + ["init"], capture_output=True, text=True,
                    env=dict(fresh, MEMORY_DIR=MEMO))   # a file, not a store
check(r_.returncode == 1 and "Traceback" not in r_.stderr
      and "Not a directory" in r_.stderr,
      "a filesystem error printed a traceback: " + r_.stderr)


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
check(re.search(r"Run: \S*memo wake 2", run("wake").stdout),
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
            out[os.path.relpath(p, path)] = open(p, "rb").read()
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
    t = os.path.join(store, "TREE")
    return {int(f): os.path.getsize(os.path.join(t, f)) // cli.TREE_REC
            for f in os.listdir(t) if f.isdigit()}


def log_records(store):
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
shutil.rmtree(dp)

shutil.rmtree(d2)
shutil.rmtree(d)
print("\n%d passed, %d failed" % (ok, fail))
sys.exit(1 if fail else 0)
