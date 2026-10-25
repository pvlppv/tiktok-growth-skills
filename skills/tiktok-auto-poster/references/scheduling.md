# Scheduling

The daily shape:

    night      your renderer writes carousel folders (and a .hold in each, rule 1)
    morning    hold --new, plan --save, review --live      -> albums in Telegram
    any time   a person replies OK                          -> approve
    midday     postiz --live, telegram --live, farm --live  -> posts
    evening    check                                        -> failures, if any

## cron

`crontab -e`, times are the machine's local time. `cd` into the folder that holds `.env` and
`accounts.txt`; cron does not source your shell profile.

```cron
# m  h  dom mon dow  command
 0  8  *   *   *    cd ~/posting && python3 skills/tiktok-auto-poster/scripts/poster.py hold --dir renders/a --dir renders/b --new && python3 skills/tiktok-auto-poster/scripts/poster.py plan --dir renders/a --dir renders/b --save && python3 skills/tiktok-auto-poster/scripts/poster.py review --dir renders/a --dir renders/b --live >> logs/review.log 2>&1
30 11  *   *   *    cd ~/posting && python3 skills/tiktok-auto-poster/scripts/poster.py postiz --dir renders/a --dir renders/b --live >> logs/post.log 2>&1
35 11  *   *   *    cd ~/posting && python3 skills/tiktok-auto-poster/scripts/poster.py telegram --dir renders/a --dir renders/b --live >> logs/post.log 2>&1
```

Approval stays a human step: run `approve` yourself after you replied OK. Posting jobs that
fire before you approved simply skip held carousels and print `held, skipped`; run them again
after approving (every command is safe to re-run).

## Claude Code scheduled task

Create a scheduled task (for example daily at 08:00) with a prompt like:

```markdown
Run the morning send for tiktok-auto-poster, from ~/posting:

    python3 skills/tiktok-auto-poster/scripts/poster.py hold   --dir renders/a --dir renders/b --new
    python3 skills/tiktok-auto-poster/scripts/poster.py plan   --dir renders/a --dir renders/b --save
    python3 skills/tiktok-auto-poster/scripts/poster.py review --dir renders/a --dir renders/b --live

Then stop. Do not approve anything yourself. Report: how many were planned, how many went
to review, and any line that says short / held / FAILED, word for word.
```

and a second task for midday:

```markdown
From ~/posting run postiz --live, then telegram --live, then check (same --dir flags).
Report every POSTIZ FAILED and ERROR line word for word. Never retry a failed booking by
hand and never remove a .hold.
```

Keep these prompts in your repo and generate the scheduled-task file from them with a small
build step. Hand-kept copies drift, and then edits to the source never reach what actually runs.
