# StashSweep — a private, reversible disk-cleaner that talks back

StashSweep is a local personal assistant that scans your folders, finds
reclaimable space (duplicates, junk dirs, old files), and cleans it up **safely**:
every "deletion" is actually a *move to quarantine* — fully reversible.

It talks to you three ways:

1. **CLI** — `pa scan`, `pa recommend`, `pa clean`
2. **System-tray app** — green icon next to your clock
3. **Popup chat** — press `Ctrl+Alt+P` anywhere; it says *Wassup*, you talk to it
   like a person ("files I haven't opened in 6 months", "my drive feels heavy"),
   powered by an LLM agent (Groq / Gemini / OpenAI / Ollama — your choice).

## The safety model

1. `scan` and `recommend` are always **read-only**
2. `clean` is a **dry-run by default** — pass `--apply` to act
3. `--apply` moves items to `.pa-quarantine/` (reversible) — it never truly deletes
4. The only destroying command is `pa quarantine purge`, which needs explicit confirmation
5. Paths in `rules.toml → deny_paths` are untouchable, no matter what
6. In chat: the LLM only *proposes* tools — the deterministic engine executes them,
   and the LLM can never bypass the stash/purge gates

## Running the popup assistant (Hotkey/trays)

```
python -m pa.tray
```

- Tray icon: `Open chat` / `Quit`
- Global hotkey: `Ctrl+Alt+P` opens the chat popup anywhere
- Voice greeting: says "Wassup" through Windows TTS (skip it if `pyttsx3` isn't installed)
- Make the process console-less: run it with `pythonw.exe -m pa.tray`

### Giving it a brain (optional but recommended)

Copy `llm.example.toml` to `llm.toml` and pick a provider — it's gitignored so your
key never gets pushed:

```toml
[llm]
provider = "groq"                     # groq | gemini | openai | ollama | none
model = "openai/gpt-oss-120b"
api_key = "gsk_..."                   # free from console.groq.com
```

- **groq / gemini** — free tiers, cloud-smart, instant setup
- **ollama** (`provider = "ollama"`, e.g. model `qwen2.5:3b`) — fully local & private
- **none** — deterministic keyword mode (offline, understands the basics)

Agent contract: the LLM replies with strict JSON `{say, tool, path, arg}`; tools map
to the same engine the CLI uses. It proposes, the engine disposes.


## Commands

```powershell
cd C:\Users\sahil\personal-assistant

pa scan <folder>          # top-level inventory + findings
pa scan <folder> --analyze
pa recommend <folder>     # "what I'd delete" verdicts + reasons
pa recommend <folder> --apply         # quarantine the SAFE picks
pa recommend <folder> --apply --yes   # skip confirmation (still reversible!)
pa clean <folder> --rule <name> --apply
pa clean --list-rules
pa quarantine list                    # what's sitting in the trash
pa quarantine restore <id>            # undo - move it back
pa quarantine purge                   # free the space for real
pa chat                               # natural language (offline keywords, optional Ollama)
```

## Running as an installed app

```
pip install -e .
pa scan                   # 'pa' now works from any directory
```

## Rules (`rules.toml`)

- `deny_paths.list` — never touch these dirs
- `[[size_rules]]` — large + old file thresholds
- `[[type_rules]]` — installers / ISO cleanup by age + extension
- `[[junk_dir_rules]]` — cache / build / temp folder patterns

## Tests

```powershell
python -m pytest tests -v
```

All 10 tests must pass before you ship a change. Every test runs in a throwaway
temp dir — no real user files are ever touched.

## Layout

```
pa/
├── cli.py            # command surface
├── config.py         # rules.toml loader + deny list
├── models.py         # FileEntry, Finding, Report
├── scanner.py        # read-only filesystem walk
├── analyzers/        # pure functions: Finding = rule + entries + bytes
├── recommend.py      # scoring -> "safe" vs "review"
├── executor.py       # THE only module allowed to write
├── quarantine.py     # reversible deletes + manifest.json
├── report.py         # Rich tables + JSON output
└── chat/             # natural-language front-end (keyword mode + optional Ollama)
```

