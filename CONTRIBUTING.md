# How a skill in this repo is built

Every skill lives in `skills/<name>/` and stands alone. Nothing is shared between folders.

```
skills/<name>/
  SKILL.md          frontmatter (name, description) + the guide the agent follows
  scripts/          the code the guide calls; every script answers --help
  references/       optional longer notes the guide links to
  requirements.txt  pip deps (stdlib needs no entry)
  .env.example      only if the skill needs keys
```

## SKILL.md

- Frontmatter: `name` (same as the folder) and `description`: one paragraph saying what it
  does and the phrases that should trigger it.
- Open with one line: "Give me X — I return Y."
- Then: quick start (copy-paste commands), the steps, **the rules that make it work** (the
  lessons that cost real runs, with the measured number when there is one), cost, limits.
- Under ~250 lines. Long material goes in `references/`.

## Code

- Python 3.10+. No hard-coded paths, handles or IDs: flags, a config file, or env vars.
- Keys only from env / `.env`. Never print them.
- Anything that spends money, posts or sends a message is off by default and needs an
  explicit flag (`--live`, `--send`, `--max-usd`).
- Small and plain beats clever. Port what earns its place; drop the rest.

## Never in this repo

App names, our account handles, chat IDs, tokens, Notion IDs, copywriting prompts, photos,
scraped data, persona details, revenue figures.
