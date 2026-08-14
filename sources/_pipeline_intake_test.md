# Ingest smoke test — safe to delete

This file exists only to prove that documents pushed to `sources/` reach the deployed
service. It is removed once stages 1-3 are verified. Anything it produces in Notion arrives
as `Pending Review` with `Include in Resume` unchecked, so it cannot reach the résumé.

## Project: Resume Pipeline (personal)

Built an automated résumé pipeline that reads approved entries from Notion, drafts the
Traditional Chinese version with Claude on Amazon Bedrock, and renders six artifacts
(HTML→PDF, LaTeX→PDF, .docx in both languages) on Google Cloud Run.

Added a human-in-the-loop approval gate: a scheduled run never publishes by itself. It
renders, diffs against the last approved snapshot, and posts a Slack notification with a
before/after review page; publishing happens only after an HMAC-signed approval.

Stored run state and artifacts through the GitHub Contents API instead of a database,
because the service has no persistent disk.

Technologies: Python, FastAPI, Playwright, Tectonic, Notion API, Slack API, Amazon Bedrock,
Google Cloud Run, GitHub Actions.
