# village-3d todo

Your four items, split into small issues. **Decide** marks a choice we still need from you.

**Status:**
- 2 (calendar) and 4 (player tags and summaries) are **done**, plus the building guide.
- 1 (deployment) is live at https://village.gensis-kb-tunnel.com.
- 3 (livelier agents) is next.

## What the data allows

| Fact | Number |
|---|---|
| Days with activity | 379, from 2025-04-02 to 2026-09-04 |
| Agents per day | 4 early on, about 30 now (46 over the whole run) |
| Village hours per day | 2–5 h through 2025, 9 h lately (read from each day's activity) |
| Built data | 379 day files (median 290 KB, 125 MB in total, 37 MB gzipped) + 4,165 agent-day files (about 220 MB) |
| Full build | about 4 min, 1.5 GB RAM |
| Summaries | 795 village-wide daily, 43 agent career, only 2 agent-per-day |
| Screenshots | up to 2026-08-21 only (not used yet, see 5.1) |

## 1. Deployment

- [x] **1.1 Who can see it: everyone.** The site is public with no login (decided 2026-09-30). The dataset's terms
      ask to cite AI Digest; the ⓘ guide credits them and Kenney.
- [x] **1.2 Hosting.** GitHub is the source of truth. This EC2 box pulls from it and Caddy serves static files on
      127.0.0.1:8080. A Cloudflare Tunnel carries traffic, so there are no public ports and no Elastic IP. No API server.
- [x] **1.3 Box ready.**
      - `sudo deploy/deploy.sh tunnel` has run: Caddy is enabled at boot, localhost only.
      - `deploy/deploy.sh publish [--build]` pulls from GitHub and copies only the page, scripts, models and data.
      - Cache headers are set: models for a year, data for an hour, page and scripts `no-cache`.
      - Verified locally.
- [x] **1.4 Cloudflare.** The tunnel `village-3d` runs on this box and routes
      `village.gensis-kb-tunnel.com` → `localhost:8080`. No Access application is needed (public site).
- [x] **1.5 Live:** https://village.gensis-kb-tunnel.com
- [ ] **1.6 Optional:** a GitHub Actions workflow that runs `deploy.sh publish` on the box on every push.
- [ ] **1.7 Frontend production pass.** Pin three.js with SRI or vendor it (it's a pinned jsDelivr version today).
- [ ] **1.8 CI.** On every PR, run `test_extract.py` and a headless smoke test: load a day, expect no console errors.
- [ ] **1.9 Uptime check** for the subdomain.
- [x] **1.10 API: not needed.** The data is a frozen export read one day at a time, so static files are enough.
      Revisit only for live data or search across days (FastAPI + DuckDB).

## 2. Calendar: any day of the village (done)

- [x] **2.1 Extractor for all days.** `extract.py` makes one pass over the tables and writes `data/index.json`,
      `data/days/<date>.json` and `data/days/<date>/<agent>.json`. It uses real Pacific time (zoneinfo), and
      `--since` gives quick dev runs.
- [x] **2.2 Village hours per day from actual activity.**
      - Stray edge hours (a single lone action, e.g. 2026-02-24 07:xx) are trimmed, along with agents seen only in them.
      - Quiet stretches of 30 minutes or more inside a day are skipped while playing.
- [x] **2.3 Clans from `model_string`.** o1/o3/o4-mini → OpenAI, fine-tuned leaders → Moonshot, Claude Code →
      Anthropic. Labels are unique for all 46 agents (GLM labels are `G5.2` and `G5.3F`).
- [x] **2.4 Older regimes.**
      - A slice with only chat puts the agent in the Town Hall.
      - `SEARCH_HISTORY` events before 2026-03-24 count as Library.
      - The Claude Code agent is placed by its own tool calls.
- [x] **2.5 Human messages stay out** (decided).
- [x] **2.6 Calendar UI.** A month grid marking the days with data, with the day number, goal and agent count in
      the tooltip. ◀ ▶ step between days, `?date=` links to one, and it opens on the latest day by default.
- [x] **2.7 Rebuild the scene per day.**
      - Characters, Hall of Records, arcs, counters, feed and hour ticks all follow the day.
      - The town is built once, with camps for all 8 clans.
      - Nothing is left over between days: label counts were checked on every switch.
- [x] **2.8 Day recap.** A tab next to the village chat with the village goal and the daily summary.
- [x] **2.9 Size check.** One day is over 1.5 MB: 2026-07-06 at 1.57 MB (509 KB gzipped).
- [ ] **2.10 Known gaps.**
      - 2026-06-13 (a Saturday special session) has no day number.
      - Some 2025 recaps print times one hour early; the error is in the source summaries.

## 3. Livelier agents (next)

- [ ] **3.1 Reproduce "stuck".** Today each agent holds one spot and one looping animation for a whole 5-minute
      slice: 5 seconds at 1 min/s, and indefinitely while paused. Also check pause/resume for a real bug.
- [ ] **3.2 Action stream data.** Store each agent's actions with real timestamps (about 150 KB per busy day) instead
      of one winning building per slice.
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

## 4. Player tags and summaries (done)

- [x] **4.1 Player card** with tabs, opened from the 3D tag, the roster, a chat line or the Hall of Records.
- [x] **4.2 Roster of player tags.** It lists only the agents present that day.
      - A clan filter; the clan chips double as the legend.
      - Agents who joined earlier but weren't there are greyed out ("away today" or "left <date>").
      - Agents who join later aren't listed, so nothing is spoiled.
- [x] **4.3 Career tab.** The career summary; if it was written after the selected day, it's locked behind "Show anyway?".
- [x] **4.4 "Thinking | Doing" side by side**, following the replay clock.
      - left: reasoning excerpts and stated intentions
      - right: moves, commands and messages
      - shows the newest 80 items per column
- [x] **4.5 Per-agent-day data**, loaded only when a card opens. It holds the latest memory up to that day (what the
      agent knew then) and up to 150 reasoning excerpts.
- [x] **4.6 "So far"** on the Today tab: days in the village and total actions and messages up to that day.
- [ ] **4.7 Decide: generate per-agent daily summaries with Claude?** Only 2 exist today. Covering every agent-day is
      roughly 4,165 summaries via the Batch API. Check the cost and the dataset terms first.
- [ ] **4.8 Small follow-up:** clicking a building sign could open the ⓘ guide at that building (today it flies there).

## 5. Later

- [x] **Building guide.** The ⓘ button explains each building, the camps, the Hall of Records, the arcs, the
      characters and how positions are decided.
- [ ] **5.1 Agent screenshots** from the dataset's per-day image archives (available up to 2026-08-21). Parked for now.

## Notes: why static files and not an API

- **The data doesn't change.** Each dataset export is frozen, and the page reads one day at a time. That's a set of
  files built once.
- **Sizes fit easily.** About 350 MB in total, served compressed; the per-agent files load only on demand.
- **Python is enough for the build.** The full pass takes about 4 minutes and runs once per export. If it ever gets
  slow, reach for DuckDB or multiprocessing before a rewrite. Rust would add a second toolchain, and users would
  never notice the difference.
- **When an API earns its place:** live data from the running village, search or aggregates across all days at
  request time, or per-user permissions. Then use Python (FastAPI plus DuckDB).
