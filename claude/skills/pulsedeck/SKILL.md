---
name: pulsedeck
description: Ask PulseDeck what is running on this PC, what a project left behind, who used a port, or who slowed the PC at a given time.
disable-model-invocation: true
argument-hint: "[status | leftovers | port N | slow-at HH:MM]"
---

# PulseDeck

Run PulseDeck's command-line tool once and summarise the answer for the user **in Hebrew**.

Script — always this full path, in double quotes (forward slashes work in Bash and PowerShell):
`<PULSEDECK_DIR>/pulsedeck_mcp.py`

The request: `$ARGUMENTS`

## Pick the command

| Request | Run |
|---|---|
| empty, or `status` | `python "<PULSEDECK_DIR>/pulsedeck_mcp.py" status` |
| `leftovers` | `python "<PULSEDECK_DIR>/pulsedeck_mcp.py" leftovers "<project folder>"` — the project folder this session runs in (your primary working directory), passed explicitly |
| `leftovers <folder>` | the same, with that folder |
| `port N` | `python "<PULSEDECK_DIR>/pulsedeck_mcp.py" port N` |
| `slow-at HH:MM` or `slow-at YYYY-MM-DDTHH:MM` | `python "<PULSEDECK_DIR>/pulsedeck_mcp.py" slow-at <time>` |

Exit code 1 means an error and the message is on stderr: report it in Hebrew, don't retry with guessed arguments.

## Summarise

Short and in Hebrew, not the raw output pasted back. For `leftovers`, say per process what it is, how much memory it holds and for how long, whether it listens on a port, whether it has open connections (in use) and whether it is marked as a server the user keeps running.

## Closing — only with explicit approval

- Nothing is closed as part of this skill unless the user asks for it.
- Before every close, ask in chat, naming the process (name, pid, folder, port, memory), and wait for an explicit yes for **that** process. One yes covers one process.
- Only then run `python "<PULSEDECK_DIR>/pulsedeck_mcp.py" close <pid> <create_time> "<folder>"`, with the pid and create_time from the `leftovers` output.
- Never close, and never offer to close: system processes, PulseDeck itself, code editors, terminals and shells, Claude / Claude Code, anything with open connections, or anything marked as a server the user keeps running. The script refuses these too; never work around a refusal.
