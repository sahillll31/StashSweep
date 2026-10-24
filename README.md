# PA — Personal Assistant (disk cleanup)

A local assistant that scans folders, finds reclaimable space, and cleans it up
**safely**: every deletion is actually a *move to quarantine*, fully reversible.

## The safety model

1. `scan` and `recommend` are always **read-only**
2. `clean` is a **dry-run by default** — pass `--apply` to act
3. `--apply` moves items to `.pa-quarantine/` (reversible) — it never truly deletes
4. The only destroying command is `pa quarantine purge`, which needs explicit confirmation
5. Paths in `rules.toml → deny_paths` are untouchable, no matter what

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

