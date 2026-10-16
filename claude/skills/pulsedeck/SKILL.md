---
name: pulsedeck
description: Ask PulseDeck what is running on this PC, who is loading it and what can be eased, what a project left behind, who used a port, or who slowed the PC at a given time.
disable-model-invocation: true
argument-hint: "[status | load | leftovers | port N | slow-at HH:MM]"
allowed-tools: Bash(python *)
---

# PulseDeck

Run PulseDeck's command-line tool and summarise the answer for the user **in Hebrew**.

Run it with the Bash tool: `python ...` is pre-approved there, so the read-only commands need no permission prompt. The chat approval below is still required before any action (`lower`, `restore`, `quit`, `force-quit`, `close`).

Script — always this full path, in double quotes (forward slashes work in Bash and PowerShell):
`<PULSEDECK_DIR>/pulsedeck_mcp.py`

The request: `$ARGUMENTS`

## Pick the command

| Request | Run |
|---|---|
| empty, or `status` | `python "<PULSEDECK_DIR>/pulsedeck_mcp.py" status` |
| `load`, or "the PC is slow / free something up" | `python "<PULSEDECK_DIR>/pulsedeck_mcp.py" load` |
| `leftovers` | `python "<PULSEDECK_DIR>/pulsedeck_mcp.py" leftovers "<project folder>"` — the project folder this session runs in (your primary working directory), passed explicitly |
| `leftovers <folder>` | the same, with that folder |
| `port N` | `python "<PULSEDECK_DIR>/pulsedeck_mcp.py" port N` |
| `slow-at HH:MM` or `slow-at YYYY-MM-DDTHH:MM` | `python "<PULSEDECK_DIR>/pulsedeck_mcp.py" slow-at <time>` |

Exit code 1 means an error or a refusal and the message is on stderr: report it in Hebrew, don't retry with guessed arguments.

## Summarise

Short and in Hebrew, not the raw output pasted back. For `leftovers`, say per process what it is, how much memory it holds and for how long, whether it listens on a port, whether it has open connections (in use) and whether it is marked as a server the user keeps running.

For `load`, say who loaded the computer over the last minute (CPU and memory), what each program is if the name is cryptic, and which ones PulseDeck may act on ("Can: ...") — then offer, per program, at most one action, safest first:
- `lower` — lowers its priority: it keeps working and gives way when the CPU is busy. Reversible (`restore`), and resets when the program restarts. Prefer this.
- `quit` — closes it the normal way, like clicking X; unsaved work prompts as usual. Only for programs whose line says `close`.
Programs marked "Never touched" are only explained, never offered.

## Acting — only with explicit approval, one program at a time

- Nothing is lowered or closed as part of this skill unless the user asks for it.
- Before every action, ask in chat, naming the program, the action and what it means (memory, CPU, whether it has a window), and wait for an explicit yes for **that** program and **that** action. One yes covers one action on one program.
- Then run one of:
  - `python "<PULSEDECK_DIR>/pulsedeck_mcp.py" lower "<name>"` / `restore "<name>"`
  - `python "<PULSEDECK_DIR>/pulsedeck_mcp.py" quit "<name>"`
  - `python "<PULSEDECK_DIR>/pulsedeck_mcp.py" close <pid> <create_time> "<folder>"` — for a process from `leftovers`, with its pid and create_time.
- If `quit` reports the program is still open, running in the background, or has no window, tell the user and ask **again**, separately, before `python "<PULSEDECK_DIR>/pulsedeck_mcp.py" force-quit "<name>"` — say plainly that unsaved work is lost. Never force without that second yes.
- Never act on, and never offer: Windows / system processes, the antivirus, PulseDeck itself, code editors, terminals and shells, Claude / Claude Code, anything with open connections, or anything marked as a server the user keeps running. The script refuses these too; never work around a refusal.
