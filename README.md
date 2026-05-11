# PR Review Agent

LangGraph agent that reviews a GitHub PR end-to-end: reads the diff, reasons about it with Claude, and either auto-approves or escalates to human reviewers with targeted, file/line-cited comments.

> Full README — install, architecture, design decisions, scale notes — lands in Phase 5. This file is a placeholder so the initial scaffold commit has a landing page.

## Usage

```bash
python main.py https://github.com/<org>/<repo>/pull/<n> --mode conservative
python main.py https://github.com/<org>/<repo>/pull/<n> --mode aggressive
```

## Status

Built in phased commits — see `git log` for the build progression.
