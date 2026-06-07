# DeepPaperNote-mineru

MinerU-backed fork of DeepPaperNote for Codex/agent-based deep paper reading notes.

This fork keeps the original evidence-grounded workflow, figure/table decision flow, lint gates, and Obsidian save behavior, while changing two important defaults:

- document parsing uses the MinerU v4 precise API with OCR by default
- data-mining and AI papers are written in a customized 16-section Chinese 精读版 format

## What This Skill Does

Given one paper title, DOI, URL, Zotero item, or local PDF, the skill:

1. resolves paper identity and metadata
2. acquires or reuses the best PDF
3. parses the paper through MinerU v4 precise API
4. builds canonical source artifacts and evidence manifests
5. plans figure/table placement
6. validates the note plan against source sections/pages
7. writes a Markdown deep-reading note
8. runs structure, style, math, figure, planning, and substantive-content lint
9. saves the final note into an Obsidian-style vault when configured

## Custom Defaults

### MinerU Parser

Set one of these environment variables before running the skill:

```bash
export MINERU_API_TOKEN="your-mineru-token"
```

or:

```bash
export DEEPPAPERNOTE_MINERU_API_TOKEN="your-mineru-token"
```

Do not hard-code API tokens in `SKILL.md`, scripts, notes, logs, or committed files.

Default MinerU options:

- API mode: v4 precise API
- model version: `vlm`
- OCR: enabled
- formula recognition: enabled
- table recognition: enabled
- language: `ch`

Useful overrides:

- `MINERU_MODEL_VERSION`
- `MINERU_IS_OCR`
- `MINERU_ENABLE_FORMULA`
- `MINERU_ENABLE_TABLE`
- `MINERU_LANGUAGE`
- `MINERU_PAGE_RANGES`
- `MINERU_TIMEOUT_SECONDS`
- `MINERU_POLL_INTERVAL_SECONDS`

Each also has a `DEEPPAPERNOTE_...` variant.

### Data-Mining / AI Note Format

For data-mining and AI papers, the default output follows `references/data-mining-ai-note-format.md`:

- `# 论文精读笔记`
- 16 numbered sections from `1. 论文基本信息` through `16. 总评`
- emphasis on technical motivation, method framework, formulas, algorithm flow, experimental design, innovation judgment, limitations, and follow-up topics

## Install

Clone this repository into your Codex skills directory:

```bash
git clone <this-repo-url> ~/.codex/skills/DeepPaperNote-mineru
```

Restart Codex so the skill metadata is reloaded.

The skill name is:

```text
deeppapernote-mineru
```

## Python Dependencies

For local script execution and tests:

```bash
python3 -m pip install -e '.[dev]'
```

`PyMuPDF` is still included because the workflow uses it for PDF page metadata, figure assets, and optional explicit fallback paths. MinerU is the default text parsing backend.

## Obsidian Output

Set your vault path if you want notes written directly into Obsidian:

```bash
export DEEPPAPERNOTE_OBSIDIAN_VAULT="/absolute/path/to/your/Obsidian Vault"
```

If no vault is configured, the skill will ask before using a workspace fallback.

## Development Checks

Run the test suite:

```bash
python3 -m pytest -q
```

Run a syntax check:

```bash
python3 -m py_compile scripts/*.py
```

## Safety

- MinerU tokens are read from environment variables only.
- The parser fails closed if MinerU fails, unless `DEEPPAPERNOTE_CUSTOM_ALLOW_PYMUPDF_FALLBACK=1` is explicitly set.
- Final notes must pass lint before being written to Obsidian.

## Upstream

This is a local customization fork of DeepPaperNote. Keep upstream changes separate from local parsing, note-format, and save-policy customizations.
