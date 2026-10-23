# TOOLS.md snippet: code-nav (Pilot B canary)

Paste under the agent's tool list to enroll them in the Pilot B canary.
Stage-0 measured 55-162x fewer tokens than whole-file reads on 300k+ token
files. Prefer these over reading big files to orient.

```markdown
## code-nav (symbolic navigation; Pilot B)

Run from anywhere inside the repo. Never from outside it. First call warms
the language server (~seconds); concurrent calls for one repo queue.

- `code-nav outline <file> [--depth 1]` -- symbols in a file. Start here
  for any file you have not read. `--depth 0` for top-level only.
- `code-nav symbol <name_path> [--body] [--in <file-or-dir>]` -- find a
  symbol (`MyClass/my_method`); `--body` prints its source. Pass `--in`
  whenever you know the file: much faster, fewer tokens.
- `code-nav refs <name_path> <file>` -- exact references to the symbol
  (language server, not grep). Check these before changing a signature.

Rules: outline before full read on files over ~500 lines; one `--body` beats
re-reading the file; `refs` beats grep for callers. Timeouts (120 s) mean
the server is still indexing -- retry once, then fall back to Read.
```
