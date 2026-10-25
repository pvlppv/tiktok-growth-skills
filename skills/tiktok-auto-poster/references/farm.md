# Farm route — what it expects and why every tap needs a check

## What farm software

`scripts/farm.py` talks to an iOS phone-farm server with a local HTTP API. The farm drives
real iPhones over USB through WebDriverAgent (WDA): pushes the
slides into the camera roll, switches the TikTok account, types title and description, saves to
Drafts (or posts). Anything that serves these two routes works:

    GET  {FARM_URL}/api/devices
         -> [{name, udid, pluginData: {FARM_PLUGIN: {accounts: [handles]}}}]
    POST {FARM_URL}/api/devices/{udid}/posts   (multipart)
         destination = draft | publish, account, title, caption,
         timing = {"kind":"once","runAt":ISO}, musicUrl (optional), media (one per slide)

`farm.py devices` lists what the farm knows. The device name in your accounts file must equal
the farm's device name, and the handle must be in that device's account list, or the row is
skipped with `FARM SKIP`.

Timing: `--start HH:MM` for the first post on each device, `--gap` minutes between posts,
`--jitter` ± minutes seeded per account and day (a fixed cadence across many phones is its own
fingerprint; seeded so the dry run prints the live times). `--music sounds.json` = a JSON list
of TikTok sound URLs; tracking query is stripped and the same sound is not used twice in a row
for one account within a run. Default destination is **draft**; `--publish` posts publicly.

## Blind taps (read before `--live`)

A farm that taps fixed coordinates with no check of which screen is showing can log "draft
saved — succeeded" while it **publishes a Story**. One silent miss leaves the flow a screen
behind; from there a title tap can type the hook **as a text overlay on the photo**, and the
"Drafts" point on the post screen is **"Your Story" on the editor**.

Before a farm run may reach a publishing tap, the farm must:

- fingerprint the screen (OCR) before Drafts/Post and abort if the post screen is not showing;
- verify the selected-media count before Next instead of assuming the taps landed;
- reject any computed tap outside the screen bounds.

If your farm build does not do these, do not run `farm --live` on an account you care about.
First run: one phone, one carousel, drafts, a throwaway account: `farm --live --limit 1`.

## Other things to know

- WDA's added routes have no auth and listen on the phone's network interface: anyone on the
  same Wi-Fi can drive the phone. Keep phones on a network you trust; keep the farm API on
  127.0.0.1.
- Each screen size needs its own tap-coordinate profile.
- Recent Xcode runs XCTest only on iOS 17+ devices (CoreDevice). Older phones need an older
  Xcode, so a mixed fleet may need two farm instances.
