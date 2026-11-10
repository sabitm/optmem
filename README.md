# OptMem

Persistent memory for AI agents. One Python script, no dependencies, with
editable and deletable memories.

![how OptMem works](anim/optmem.gif)

## Install

The local examples use this repository's `memo` through `OPTMEM_SOURCE`, so
they do not download an older release. Remote installation requires this
version to be published at the installer's configured URL.

Install an independent copy for each harness. Each copy owns the `memory/`
directory beside its real executable.

```sh
# From this repository: Claude Code.
OPTMEM_SOURCE="$PWD/memo" sh install.sh "$HOME/.optmem/claude" \
  --skill-dir "$HOME/.claude/skills/optmem"

# From this repository: pi.
OPTMEM_SOURCE="$PWD/memo" sh install.sh "$HOME/.optmem/pi" \
  --skill-dir "$HOME/.pi/agent/skills/optmem"
```

`--skill-dir` names the complete skill folder, including `optmem`, not the
parent skills directory. The installer creates `SKILL.md` there. Use that
folder name for portable skill discovery in both harnesses.

Remote installation accepts the same arguments:

```sh
curl -fsSL https://raw.githubusercontent.com/sabitm/optmem/main/install.sh \
  | sh -s -- "$HOME/.optmem/pi" --skill-dir "$HOME/.pi/agent/skills/optmem"
```

Without arguments, the installer uses `~/.optmem` and does not install a skill.
Without `--skill-dir`, it never changes any skill file. Run the same command
again to update that installation and its generated skill. The installer
preserves existing memories, config overrides, and storage formats.

The installer refuses to overwrite custom or edited skills, skills owned by
another installation, and symlink destinations. Back up customized skill text
and choose an empty skill folder, or keep your custom skill outside installer
management. Generated skills contain an ownership record and a content hash
in the same atomic file. Do not edit that record. The installer updates only
recognized, unchanged generated content. Installation-local ownership hashes
also preserve safe skill updates after tool-only updates or interrupted skill
publication. Do not edit `.optmem-skills` inside the installation.

The installer does not edit `AGENTS.md`, `CLAUDE.md`, harness settings, or global
prompt files. Remove any old OptMem instructions that mandate startup reads
or automatic recording. Existing stores are never moved or copied into a new
installation. The default installation continues to use `~/.optmem/memory`.
New Claude/pi installation paths start with empty stores.

## Activate for a session

Generated skills include `disable-model-invocation: true`. Activate explicitly:

- Claude Code: `/optmem`
- pi: `/skill:optmem`

You can instead ask the agent to read the generated `SKILL.md`. Reload skills
or restart the harness after installation if needed. In pi, use `/reload`.
Activation applies to the current session only, not a new session or another
agent. No OptMem command should run automatically at startup.

After activation, the agent immediately runs `wake`, follows every page,
creates any requested summaries with `nap`, and resumes reading until done.
It may search with `recall`, navigate with `zoom`, and maintain required
summaries throughout the session without separate approval. Summaries use only
existing memories and must not introduce facts. Reading can update summary
files and finish pending recovery, so this is not strictly read-only access.

Adding, importing, editing, and deleting memories always require an explicit
user request or approval for the specific change. Activation does not permit
automatic recording of facts or task results. A request such as "remember that
I prefer SQLite" already authorizes that stated addition. It does not grant
permission for later additions. Changing configuration, discarding summaries
with `forget`, initializing stores, and migration also require explicit user
approval.

These are agent instructions, not operating-system access controls. Separate
installations prevent normal command routing from mixing stores. Processes
under the same account can still access each other's files. Technical
permission enforcement and special subagent permissions are not implemented.

## Installation paths and commands

```text
~/.optmem/
  claude/
    memo
    .optmem-install
    memory/
  pi/
    memo
    .optmem-install
    memory/
```

Use each installation's exact executable path, not an ambiguous `memo` on PATH:

```sh
~/.optmem/claude/memo wake
~/.optmem/pi/memo wake
```

Each store has its own IDs, summaries, config, locks, revisions, and recovery
state. Sessions using the same installation intentionally share its memory.
Symlinks to the executable resolve to the real installation and do not create
a new identity. Generated commands quote absolute paths, including spaces and
shell punctuation, and keep compression and pagination in that installation.

Installer-managed copies have a `.optmem-install` marker. They ignore ambient
`MEMORY_DIR` and always use the adjacent `memory/`. A corrupt marker stops the
command instead of selecting another store. Restore the marker from a backup
or remove the damaged marker deliberately before reinstalling. Do not change
or remove a healthy marker.

For an unmanaged script copied without the installer, `MEMORY_DIR` remains an
explicit compatibility override. Without it, memory defaults beside the real
script, not at a shared home-directory path. Printed commands pin the chosen
absolute store using `MEMORY_DIR=...`, so another shell environment cannot
redirect their continuation. Installed copies ignore that override.
The installer refuses to convert an unmanaged executable in place while its
`MEMORY_DIR` selects a different store. Keep that copy unmanaged, or choose a
new installation path. No external memory is adopted automatically.

`memo skill` prints a complete manual-only skill without creating or reading
a store. Use it if you prefer to manage the instruction file yourself. Avoid
redirecting it over an existing customized skill without first backing it up.

In the examples below, replace `memo` with the correct installation's path.

## Commands

| | |
|---|---|
| `memo init` | initialize this installation's store and print manual-use instructions |
| `memo skill` | print a complete manual-only skill without initializing memory |
| `memo wake` | load memory after activation, following all printed continuations |
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

Use the executable belonging to the store you want to migrate. Managed
installations always use their own memory directory. An unmanaged copy may
select another store with `MEMORY_DIR`.

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
  memo          this installation's Python tool
  .optmem-install  installer ownership marker; absent on unmanaged copies
  .optmem-skills   generated skill hashes, when a skill is installed
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
again to print this store's manual-use instructions. Existing instruction files
are not updated automatically. Re-run the installer with `--skill-dir` to
update its unchanged generated skill. Skills query current limits instead of
embedding limits that could become outdated.

## Manual-use instructions

`memo init` prints the instructions below with this installation's absolute
command and data paths. `memo skill` adds the skill frontmatter and an ownership
record. The default managed installation is used here as an example.

```markdown
## Memory

OptMem is available only after explicit user activation in this session.
- The tool is `/home/you/.optmem/memo`
- The memory directory is `/home/you/.optmem/memory`

### Activate for the current session

When the user invokes this skill or asks you to read these instructions,
activate OptMem for the current session. Do not activate it at session startup.
Do not carry activation into a new session or assume another agent is activated.

Run `/home/you/.optmem/memo wake` immediately. Follow every pagination command until the tool
prints `You are awake.` If reading requires summaries, complete the printed
`nap` commands and run `wake` again. Finish any compression requested after
reading. Copy every printed command in full, including its revision number.
If a revision is stale, request a fresh prompt and restart reading as directed.

### Read and maintain summaries while activated

Use `/home/you/.optmem/memo recall <regex>` to search existing memories.
Use `/home/you/.optmem/memo zoom <lo>-<hi>` to navigate their summaries and raw memories.
Read and search as needed throughout this session without separate approval.

Create missing summaries with `/home/you/.optmem/memo nap` when the tool requests them.
Summaries must use only existing memories, preserve facts, and invent nothing.
Summary maintenance is allowed after activation and needs no separate approval.
Reading can write summary files and complete pending recovery. It is not a
strictly read-only operation.

### Change memories only with user permission

Activation does not authorize adding, importing, editing, or deleting memories.
Run `note`, `import`, `edit`, or `delete` only for a user's explicit request or
approval covering that specific change. A clear request such as "remember this"
already authorizes the stated addition. Do not ask again for that same change.

Do not automatically save facts learned during work or record task results.
Do not interpret a request to read memory as permission to change its contents.
After an approved change, complete any summary maintenance the tool requests.
Use `/home/you/.optmem/memo config` to check this store's current byte limits before writing.

Do not change sizes, discard summaries with `forget`, initialize another store,
or migrate storage without a user's explicit request or approval.
Never edit memory files directly. Use only this installation's command path.
Do not read or write another installation's memories unless the user requests it.

These are agent instructions, not a technical permission or security boundary.
```
