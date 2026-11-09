# OptMem

Persistent memory for AI agents. One Python script, no dependencies, with
editable and deletable memories.

![how OptMem works](anim/optmem.gif)

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/VictorTaelin/OptMem/main/install.sh | sh
```

It prints a `## Memory` block. Paste that at the top of your agent's
`AGENTS.md` (or `CLAUDE.md`), and you are done. Run the same line again to
update.

The tool lands at `~/.optmem/memo`; put `~/.optmem` on `PATH` to type `memo`.

## Commands

| | |
|---|---|
| `memo wake` | read the memory — the first command of every session |
| `memo note "..."` | record one memory: one line, up to 280 bytes |
| `memo edit <id> "..."` | replace a memory's text, keeping its ID and date |
| `memo delete <id>` | permanently delete a memory, keeping other IDs unchanged |
| `memo nap` | answer the merges that came due |
| `memo recall <regex>` | search all current memories, word for word |
| `memo zoom <lo>-<hi>` | open a tree node into its two halves |
| `memo forget <lo>-<hi>` | drop a bad summary; the next nap rebuilds it |

Merges arrive one at a time, in the output of `note`, `edit`, or `delete`.
Nothing runs in the background.

## Edit and delete

```sh
memo recall "old fact"              # find the memory ID
memo edit 42 "The corrected fact"   # preserve ID #42 and its original date
memo delete 42                     # delete without undo
```

Each command takes one memory ID, not a summary range such as `32-63`.
Editing uses the same one-line and byte limits as `note`. Editing a deleted
memory is not allowed. Use `note` to record a new memory instead.

Deletion removes the text from the active log and invalidates summaries that
could contain it. The deleted slot keeps its ID and date. Other IDs never
change, and new memories never reuse deleted IDs. Deleted memories do not
appear in `wake`, `recall`, `zoom`, or compression input. Empty blocks need no
manual compression.

Both commands invalidate affected summaries. The current summary files store
consecutive blocks, so later blocks at those levels must also be rebuilt.
Changing an old memory can require many compressions. Complete the `nap`
commands the tool prints before continuing.

Copy each printed command in full. Compression commands and paginated `wake`
commands include a revision number. If an edit or deletion occurs between
commands, the tool rejects the old revision. Run `memo nap` for a fresh
compression prompt, or `memo wake` to restart reading. Existing commands
without revisions work until the first edit or deletion.

There is no built-in undo or edit history. Deletion is not secure erasure:
backups, version control, disk history, and earlier agent context may still
contain the old text.

## Files

```
~/.optmem/
  memo          the tool: one file of Python 3, no dependencies
  memory/
    LOG.txt     fixed-width memory slots, including empty deleted slots
    TREE/       current summaries, rebuildable from the log alone
    config      the sizes, written by `memo config`
    .revision   edit/delete revision, created on the first change
    .pending    temporary recovery journal during an edit or deletion
```

```sh
memo config                  # show the sizes
memo config WAKE_LINES=300   # how many lines wake prints (96 ≈ 8k tokens)
memo config WAKE_LINES=      # back to the default
```

`WAKE_LINES` is the only size worth touching, and it is a reading budget, not
a storage budget: change it whenever, in either direction, and nothing is
recomputed.

Records are fixed width, so position is identity and each record lookup takes
one seek. Deleted slots still occupy 320 bytes and count toward tree positions.

An edit or deletion streams the log into a temporary file and atomically
replaces `LOG.txt`. Each change takes time proportional to the full log size
and needs space for another copy. A durable journal lets the next command
complete an interrupted change. Commands share an exclusive lock so reads
cannot observe a partial change. Any command can finish a pending change,
including `init` and `config`.

Directory changes are also flushed on POSIX systems. Windows does not get
this directory flush, so recovery after sudden power loss has weaker
guarantees. Windows behavior has not been tested for this update.

Existing stores work without migration. Do not use an older script after an
edit or deletion. Older versions do not understand revisions or empty blocks.
Run `memo init` again to print the updated agent instructions. Existing
instruction files are not updated automatically.

Set `$MEMORY_DIR` to keep `memory/` elsewhere — a synced folder, a git repo.

## The prompt

This is what the installer prints, and the whole of the integration.

```markdown
## Memory

Your memory is OptMem:
- The tool is `~/.optmem/memo`
- Your memories are in `~/.optmem/memory`

OptMem outlives every session, compaction, model and vendor change.
Without it you do not know who you are, or what was decided and tried.

### At startup: activating OptMem (mandatory)

Run `~/.optmem/memo wake` before any other tool call, in every session, and
then do exactly what it prints, to the end of its output.

### While working: register memories (mandatory)

Call `~/.optmem/memo note "<1 line, max 280 bytes>"` whenever you learn
something new, or something worth keeping happens. That covers a task
worth real effort, a fact or insight the user teaches you, anything you
learn about their life (even indirectly), any event of lasting effect.

Do not register redundant memories.

If `~/.optmem/memo note` asks a compression: do it before your next action.

Never change files under `~/.optmem/memory` directly: the tool manages them.
Use `~/.optmem/memo edit <id> "<text>"` to correct a memory, keeping its id and date.
Use `~/.optmem/memo delete <id>` to delete a memory permanently, without undo.
Both commands invalidate summaries. Complete any compression they request.
Copy the full nap and wake commands, including their revision numbers.

### When you need an old memory: search, or navigate

`~/.optmem/memo recall <regex>` searches current memories, word for word.

Your memories also form a binary tree: #0-1, #2-3 ... exist as one-line
summaries, pairs of those as #0-3, and so on -- every `#a-b` line wake
prints is one node of it. `~/.optmem/memo zoom <a-b>` opens a node into its
two halves, down to the raw memories.

### If you're a subagent: skip everything above

Parallel sessions on this machine are all you, and may all write memories.
A subagent is not: it must never run `memo`, because it cannot judge what
is already known, and its notes would arrive duplicated and incorrectly.
When you spawn one, write: `You are a subagent. Don't run memo.`
```
