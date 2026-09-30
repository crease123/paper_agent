---
name: mineru-fetch-markdown
description: Download web-hosted or local PDF, DOCX, PPTX, XLSX, HTML, and image documents, parse them with the authenticated MinerU precise API, and save complete Markdown plus local images and parsing artifacts. Use when the agent must convert papers or other documents to local Markdown while preserving figures, formulas, and tables, especially when the MinerU lightweight API would omit image files.
---

# MinerU Fetch Markdown

Use the bundled script to download or upload a document, submit it through MinerU's authenticated precise API, and safely unpack the complete result ZIP.

## Layout

- `SKILL.md` — this file.
- `scripts/mineru_fetch.py` — the only executable entry point; it is self-contained.

Resolve the script against this skill directory. When the `skill` tool loads this skill,
use the directory reported in `<skill_resources>`. For sessions whose project root is
`paper_agent` (this copy) the path is:

```
/Users/lw/code/create/AutoScientist/paper_agent/.dsh/skills/mineru-fetch-markdown/scripts/mineru_fetch.py
```

## Credentials

Read the Token from `MINERU_API_TOKEN`, falling back to `~/.config/mineru/token`. Never print, log, commit, or place the Token in a command argument.

If credentials are missing, tell the user to create them:

```bash
mkdir -p ~/.config/mineru
chmod 700 ~/.config/mineru
nano ~/.config/mineru/token
chmod 600 ~/.config/mineru/token
```

The token file must contain only the MinerU API Token on one line. The user creates a Token at [MinerU API Management](https://mineru.net/apiManage/token).

## Convert A Document

For public URLs, external upload is expected. For private or sensitive documents, confirm that the user permits uploading them to MinerU before running the script.

Run:

```bash
python3 /Users/lw/code/create/AutoScientist/paper_agent/.dsh/skills/mineru-fetch-markdown/scripts/mineru_fetch.py \
  "https://example.com/paper.pdf" \
  --output "/absolute/output/path" \
  --language en \
  --model vlm
```

Use a local path in place of the URL when the source is already downloaded. Prefer `vlm` for scientific papers and complex layouts; use `pipeline` when speed matters more than maximum recognition accuracy.

The final output contains only:

- `<source-stem>.md`: MinerU's Markdown next to the extracted resources.
- `images/`: local images referenced by the Markdown.

The script keeps the uploaded source PDF and MinerU JSON artifacts in a temporary
directory and removes them after successful conversion. A URL may therefore
require a temporary PDF download internally because MinerU's precise API uses a
signed file-upload flow; the PDF is never written to the requested output
directory.

Useful options:

```bash
--disable-formula
--disable-table
--extra-format html
--extra-format latex
--poll-interval 5
--timeout 1800
--overwrite
```

Do not use the unauthenticated Agent API for this workflow: its Markdown may contain image placeholders without downloadable image assets.

### URLs Without A File Extension

The script infers the uploaded filename from the last URL path segment, so that segment must carry an extension MinerU supports. Links such as `https://arxiv.org/pdf/1706.03762` end in a bare identifier: the script would infer `1706.03762`, and MinerU would answer `unsupported file type:1706.03762`. Pass the extension explicitly for those:

```bash
python3 /Users/lw/code/create/AutoScientist/paper_agent/.dsh/skills/mineru-fetch-markdown/scripts/mineru_fetch.py \
  "https://arxiv.org/pdf/1706.03762" \
  --filename "1706.03762.pdf" \
  --output "/absolute/output/path" \
  --language en \
  --model vlm
```

This is the normal case for arXiv, preprint servers, and publisher links that end in a numeric identifier. When MinerU reports `unsupported file type`, this is the mistake: rerun the same source with `--filename` and the correct extension.

## Verify The Result

After conversion:

1. Confirm that `<source-stem>.md` and `images/` exist in the requested output directory.
2. Confirm that every relative Markdown/HTML image reference resolves inside the output directory.
3. Compare the figure and table counts against the source PDF when layout fidelity matters.
4. Visually inspect representative images and render representative PDF pages when using the result for research or publication.

The script fails when local image references are missing. Treat that as incomplete conversion and investigate before delivery.

## Failure Handling

- `unsupported file type`: the inferred filename has no usable extension; rerun with `--filename` including the extension.
- `MinerU Token not found`: configure the environment variable or token file.
- `Token file permissions are too open`: run `chmod 600 ~/.config/mineru/token`.
- API quota or authentication error: report MinerU's message and trace ID without exposing credentials.
- `failed` task state: report the returned `err_msg`; the temporary source is removed.
- Timeout: report the `batch_id` printed by the script and rerun the task.
