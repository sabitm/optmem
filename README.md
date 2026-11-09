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
| `memo note "..."` | record one memory: one line, up to 2,048 UTF-8 bytes |
| `memo edit <id> "..."` | replace a memory's text, keeping its ID and date |
| `memo delete <id>` | permanently delete a memory, keeping other IDs unchanged |
| `memo nap` | answer the merges that came due |
| `memo recall <regex>` | search all current memories, word for word |
| `memo zoom <lo>-<hi>` | open a tree node into its two halves |
| `memo forget <lo>-<hi>` | drop a bad summary; the next nap rebuilds it |
| `memo migrate` | explicitly upgrade a legacy store to larger records |

Merges arrive one at a time, in the output of `note`, `edit`, or `delete`.
Nothing runs in the background.

## Memory and summary limits

New stores use format 2. Existing unmarked stores use format 1 until migrated.

| Limit | Format 1 | Format 2 |
|---|---:|---:|
| Memory text | 280 bytes | 2,048 bytes |
| Summary text | 280 bytes | 560 bytes |
| Memory record, including metadata and padding | 320 bytes | 2,112 bytes |
| Summary record, including padding | 288 bytes | 576 bytes |

Limits measure UTF-8 bytes, not characters. Most English characters use one
byte. Other characters can use two to four bytes. Memories and summaries
remain single-line. Memory limits apply to `note`, `edit`, and `import`.

```sh
memo config ENTRY_CHARS=2048    # memory text limit in format 2
memo config SUMMARY_CHARS=560   # summary text limit in format 2
memo config SUMMARY_CHARS=420   # choose shorter summaries
memo config SUMMARY_CHARS=      # restore this format's default
```

The existing `CHARS` names measure bytes. Each limit can be lowered but cannot
exceed its format's maximum. Memory and summary limits are independent.
Changing a limit does not rewrite existing text or rebuild existing summaries.
On legacy stores, a custom `ENTRY_CHARS` no longer also limits summaries.
Use `SUMMARY_CHARS` to set that limit separately.

`WAKE_LINES` remains 96 by default. With summaries at their maximum length,
the summary portion of startup context can be roughly twice as large.
Longer raw memories can increase it further. Use `WAKE_LINES=48` for roughly
the previous summary-text budget. Output pagination stays at 20,000 bytes per
part by default.

Compression uses at most 16 raw memories and an 8,192-byte source-text budget.
When their rendered text exceeds that budget, the prompt uses child summaries
instead. Large blocks still use child summaries.

## Upgrade an existing store

Stop other sessions that use older copies of the script. Back up the entire
memory directory before migration. Keep that backup outside the store.
Then run the updated script:

```sh
memo migrate
memo init                     # print updated agent instructions
```

Set `MEMORY_DIR` first if the store is not at `~/.optmem/memory`.

Migration preserves memory IDs, dates, deleted slots, summary text, and config
overrides. Existing short summaries remain valid. They can become longer when
rebuilt. Pinned limits stay pinned. For example, use `memo config ENTRY_CHARS=`
to remove an old 280-byte override after migration.

Migration builds a complete larger copy before replacing any source file.
It checks file sizes and SHA-256 hashes during recovery. After the migration
journal is saved, any command completes an interrupted migration before using
the store. An interruption before that journal leaves the source unchanged.
Run `memo migrate` again to discard incomplete staging and retry.

Migration increments the revision once. Restart paginated reads and request
fresh compression prompts afterward. Running `memo migrate` on format 2 is a
no-op. There is no downgrade command. Older scripts must not access format 2:
they do not understand its record sizes and can corrupt the store.

Migration needs temporary space for the complete format-2 log and summaries,
in addition to the existing store. A fully summarized store with one million
slots uses approximately 2.69 GB in format 2, compared with 608 MB in format 1.
Deleted slots still use space. Larger records also increase edit/delete costs.

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
without revisions work until the first edit, deletion, or migration.

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
    .format     format-2 version marker; absent on legacy stores
    .revision   edit/delete/migration revision, created on the first change
    .pending    temporary recovery journal during an edit or deletion
    .migration  temporary recovery journal during migration
    .migrate/   temporary staged format-2 files during migration
```

```sh
memo config                  # show this store's sizes
memo config WAKE_LINES=300   # how many memory or summary lines wake selects
memo config WAKE_LINES=      # back to the default
```

`WAKE_LINES` is a reading budget, not a storage budget. Change it whenever,
in either direction, without recomputing summaries.

Records are fixed width, so position is identity and each record lookup takes
one seek. Deleted slots keep their format's full width and tree positions.

An edit or deletion streams the log into a temporary file and atomically
replaces `LOG.txt`. Each change takes time proportional to the full log size
and needs space for another copy. A durable journal lets the next command
complete an interrupted change. Commands share an exclusive lock so reads
cannot observe a partial change. Any command can finish a pending change,
including `init` and `config`.

Directory changes are also flushed on POSIX systems. Windows does not get
this directory flush, so recovery after sudden power loss has weaker
guarantees. Windows behavior has not been tested for this update.

Existing stores keep their original format until explicitly migrated. Do not
use an older script after migration, editing, or deletion. Run `memo init`
again to print instructions with this store's current memory limit. Existing
instruction files are not updated automatically.

Set `$MEMORY_DIR` to keep `memory/` elsewhere — a synced folder, a git repo.

## The prompt

This is the integration printed for a new store with default limits. Legacy
stores and custom limits print their actual memory limit instead.

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

Call `~/.optmem/memo note "<1 line, max 2048 bytes>"` whenever you learn
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
