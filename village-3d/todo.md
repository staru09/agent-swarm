# village-3d todo

Your four items, each split into small issues. **Decide** marks a choice we need from you before that issue starts.

**Suggested order:** 2 → 3 → 4 → 1. The per-day data built in 2 is what the calendar, the livelier agents, the player
tags and the deployment all use. Settle 1.1 early, because it decides whether the site can be public at all.

## What the data allows

| Fact | Number |
|---|---|
| Days with activity | 379, from 2025-04-02 to 2026-09-04 |
| Agents per day | 4 early on, 32 now (46 over the whole run) |
| Computer-use turns per day | median about 4,000; busiest about 27,000 |
| Raw turn data per day | median 8 MB, busiest 112 MB |
| Processed day (current format) | about 0.75 MB on the busiest days |
| Reasoning text | about 7.6 MB on a busy day (57% of turns carry some) |
| Summaries | 795 village-wide daily, 43 agent career, only **2** agent-per-day |
| Screenshots | up to 2026-08-21 only |

## 1. Deployment

- [ ] **1.1 Decide who can see it.** The dataset is gated under research terms. A public site would republish chat,
      commands and reasoning text. Options: keep it private behind a login, or ask AI Digest for permission to publish.
      Blocks 1.3.
- [ ] **1.2 Hosting.** Static app plus static per-day data files on object storage behind a CDN, with no API server
      (see the notes at the end). Pick S3 + CloudFront (we are already on AWS) or Cloudflare Pages + R2.
- [ ] **1.3 Access control** to match 1.1: CloudFront signed cookies, Cloudflare Access, or nginx basic auth.
- [ ] **1.4 Build and publish pipeline.** One script that:
      - downloads the dataset (needs an HF token)
      - runs the extractor
      - uploads the day files with gzip/brotli and long cache headers under a versioned prefix
        (`data/<export-date>/…`), so a new export never mixes with an old one
- [ ] **1.5 Frontend production pass.** Pin three.js with SRI (or vendor it). Add error and empty states, load
      progress per day, and asset caching.
- [ ] **1.6 CI.** On every PR, run `test_extract.py` and a headless smoke test: load a day, expect no console errors,
      save a screenshot.
- [ ] **1.7 Domain, TLS, uptime check.**
- [ ] **1.8 Only if needed later: an API** for live village data or search across all days. Use FastAPI, which the repo
      already uses in `src/swarmguard/api.py`, over DuckDB or SQLite.

## 2. Calendar: any day of the village

- [ ] **2.1 Extractor for all days.** `extract.py --all` makes one pass over the tables and writes `data/index.json`
      plus `data/days/<date>.json`, one per day in today's `data.json` shape. Use real Pacific time (`zoneinfo`)
      instead of the fixed UTC-7.
- [ ] **2.2 Village hours per day from actual activity.** The schedule changed over time (about 2–3 h a day in 2025,
      then 4 h, then 8 h), and `villages.schedule` only holds today's.
- [ ] **2.3 Clans from `model_string`, not the name.**
      - `o1`, `o3` and `o4-mini` have no "gpt" in their names.
      - The fine-tuned Kimi leaders belong to Moonshot.
      - Opus 4.5 (Claude Code) belongs to Anthropic.
- [ ] **2.4 Older regimes.** Before 2026-03-24 computer use ran in separate sessions and much chat happened outside
      them, so take chat and session boundaries from `events` (`AGENT_TALK`, `START/STOP_USING_COMPUTER`) as well as
      turns. **Decide** how to show the Claude Code agent (Jan–Apr 2026); its data is in `claude_code_messages`.
- [ ] **2.5 Decide:** show human messages (`USER_TALK`) on days when public chat was open, e.g. as visitors at the gate?
- [ ] **2.6 Calendar UI.**
      - a month grid that marks the days with data, showing the day number and village goal on hover
      - previous/next day buttons
      - a `?date=2026-09-02` deep link
      - opens on the latest day by default
- [ ] **2.7 Rebuild the scene per day.** Characters, camps, the Hall of Records and the legend come from that day's
      agents (4 to 32). Unload the previous day cleanly.
- [ ] **2.8 Day recap.** A panel showing that day's village goal and its village daily summary (all 795 exist).
- [ ] **2.9 Size check.** Measure all 379 day files; keep each under about 1 MB gzipped.

## 3. Livelier agents

- [ ] **3.1 Reproduce "stuck".** Today each agent holds one spot and one looping animation for a whole 5-minute slice:
      5 seconds at 1 min/s, and indefinitely while paused. Also check pause/resume for a real bug.
- [ ] **3.2 Action stream data.** Store each agent's actions with real timestamps (about 150 KB per busy day) instead
      of one winning building per slice. Depends on 2.1.
- [ ] **3.3 Act out each action at its real time.**
      - walk to the Town Hall to say each message
      - work at the Workshop for each run of bash
      - climb the Watchtower for GUI runs
      - fetch a book at the Library on a memory update
      - sit at camp when paused

      Stick to the current place for a moment, so agents don't bounce between buildings.
- [ ] **3.4 Workstations.** Several spots per building and varied animations (the characters have 27), so a crowd
      isn't doing one identical move.
- [ ] **3.5 Idle life.** Glance around, wander a little, react when mentioned. Paused means clearly frozen, with a
      paused state on screen.
- [ ] **3.6 Speeds.** Add real time (1×) and 10 s/s; retune the default.
- [ ] **3.7 Optional:** a small floating screen above an agent showing its current command or page.

## 4. Player tags and summaries

- [ ] **4.1 Player card.** A Clash of Clans–style profile per agent: portrait, clan badge, model, role and goal, date
      joined, days in the village, stats. It opens from the 3D tag, the roster and the Hall of Records.
- [ ] **4.2 Roster.** A panel with a player tag for every agent in the selected day; filter by clan.
- [ ] **4.3 Career tab.** The agent's career summary. 43 exist; agents without one say so.
- [ ] **4.4 "Thinking | Doing" side by side** for the selected day, lined up by time and following the replay clock.
      - left: reasoning excerpts, memory notes and stated intentions
      - right: actions, commands and messages
- [ ] **4.5 Per-agent-day data.** A file per agent per day (`data/days/<date>/<agent>.json`), loaded only when opened.
      It holds reasoning excerpts and memory notes, trimmed to about 300 KB and scrubbed like the rest. Depends on 2.1.
- [ ] **4.6 Decide: generate per-agent daily summaries with Claude?** Only 2 exist today. Covering every agent-day is
      roughly 379 days × 15 agents ≈ 5–6k summaries via the Batch API. Check the cost and the dataset terms first.

## Notes: why static files and not an API

- **The data doesn't change.** Each dataset export is frozen, and the page reads one day at a time. That's a set of
  files, one per day, built once.
- **Sizes fit easily.** The whole history is about 379 files of 1 MB or less, plus the per-agent-day detail files
  loaded on demand. A CDN serves these with no server to run, scale or secure.
- **Python is enough for the build.** A full pass over the tables takes minutes and runs once per export. If it ever
  gets slow, reach for DuckDB or multiprocessing before a rewrite. Rust would add a second toolchain, and users would
  never notice the difference.
- **When an API earns its place:** live data from the running village, search or aggregates across all days at request
  time, or per-user permissions. Then use Python (FastAPI plus DuckDB). The request path would be I/O-bound, where Rust
  buys nothing.
