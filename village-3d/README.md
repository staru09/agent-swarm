# village-3d

Any day of the [AI Village](https://theaidigest.org/village) as a walkable toy town. Pick a date from 2 Apr 2025 to
4 Sep 2026 (379 days in [`aidigestorg/ai-village`](https://huggingface.co/datasets/aidigestorg/ai-village)). The agents
who were there that day walk between buildings as the day replays in 5-minute steps. Each has a player card with what
it thought, what it did, what it remembered up to that day, and a career summary.

It is one static page (three.js from a CDN, no build step) plus data files built by a stdlib-only Python script.

## Run it

**1. Get the dataset.** It is gated: request access on the HF page, then download the files the extractor reads
(about 5.3 GB, most of it computer-use turns and agent memories):

```bash
uvx --from huggingface_hub hf auth login
uvx --from huggingface_hub hf download aidigestorg/ai-village --repo-type dataset \
  manifest.json agents.jsonl.gz agent_goals.jsonl.gz agent_memories.jsonl.gz chat_messages.jsonl.gz \
  claude_code_messages.jsonl.gz computer_use_sessions.jsonl.gz computer_use_turns.jsonl.gz events.jsonl.gz \
  summaries.jsonl.gz village_goals.jsonl.gz village-transcript.json
```

The extractor finds the latest downloaded snapshot in the HF cache (`$HF_HUB_CACHE`, default `~/.cache/huggingface/hub`).
To use a plain folder of the files instead, set `VILLAGE_DATA=/path/to/folder`.

**2. Build `data/` and serve the folder:**

```bash
cd village-3d
python3 test_extract.py               # self-check of the pure helpers, prints "ok"
python3 extract.py                    # about 4 min, 1.5 GB RAM: every day into data/ (about 350 MB)
python3 extract.py --since 2026-09-01 # about 1 min: only recent days, for quick iterations
python3 -m http.server 8000           # then open http://localhost:8000 (or ?date=2026-09-02)
```

| Output | What |
|---|---|
| `data/index.json` | the calendar: every day with its day number, village goal, agents and counts; every agent with its clan, first and last day, and career summary |
| `data/days/<date>.json` | one day: its hours, each agent's 5-minute track, stats, goal, stated intentions and commands, the chat, the village goal and the daily recap |
| `data/days/<date>/<agent>.json` | loaded only when a player card opens: the agent's latest memory up to that day, and up to 150 reasoning excerpts from that day |

`data/` holds chat text, commands, memories and reasoning from the gated dataset, so it is git-ignored. Don't commit it.

## What you are looking at

| In the town | Meaning |
|---|---|
| ⚒️ **Workshop** (windmill) | bash / terminal actions. The sails spin faster the more agents are in there |
| 🔭 **Watchtower** | browser and GUI actions: clicks, typing, scrolling, screenshots |
| 💬 **Town Hall** | sending chat messages and requests to humans (outreach approval, human helper, Google sign-in) |
| 📚 **Library** | memory consolidations and history searches |
| 🔥 **Clan camps** | pausing, or no recorded action in that slice. One tent per provider, in the provider's colour |
| 🧱 **Hall of Records** | one LEGO column per agent. Pick the measure under *Plaza*: actions, messages, times mentioned, bash share, error rate, idle time, memory updates |
| **Arcs** | chat mentions. Colour = speaker, lightening toward the mentioned agent. *Last hour* shows pairs with 2 or more mentions, *Whole day* shows the lasting ties. Select an agent to see all of its ties |
| **Characters** | Kenney blocky figures. Shirt = clan colour, chest print = model (`O4.8` = Claude Opus 4.8, `5.6S` = GPT-5.6 Sol, `G5.2` = GLM-5.2) |

The **ⓘ** button next to the counters opens this guide in the app.

- **Calendar.** Click the date in the title card for a month grid; days with data are marked. ◀ ▶ step between days,
  and `?date=YYYY-MM-DD` links to one.
- **Players.** Only the agents who were there that day are in the town. The roster lists them as player tags, with
  a clan filter. Agents who joined earlier but weren't there that day are listed greyed out ("away today" or
  "left <date>"). Agents who join later are not listed, so nothing is spoiled.
- **Player card.** Click an agent, its tag, its column or a chat line to open its card:
  - **Today:** where it is now, its latest command and intent, where its day went, the day's numbers, who it talks
    with, and "so far" (days in the village, total actions and messages up to this day)
  - **Thinking | Doing:** its reasoning and stated intentions next to its moves, commands and messages, following the
    replay clock
  - **Memory:** its own consolidated memory, the latest one written up to this day. This is what it knew then
  - **Career:** the career summary. If it was written after the selected day, it stays locked behind a "Show
    anyway?" button, because it covers later events
- **Day recap.** Next to the village chat: the village goal and the daily summary for that day.

**Controls:**
- Map view: drag to pan, right-drag to rotate, scroll to zoom.
- **Walk**: WASD to move, mouse to look, Shift to run, click an agent to open its card, Esc to leave. Needs a mouse.
- Space: play/pause. ←/→: jump 15 minutes.

## How the replay is built

- **Village hours per day.** A day is a Pacific-time calendar date, with real PDT/PST. Its window runs from the
  first to the last busy hour. That is about 2–5 h a day through 2025 and 9 h lately, because the schedule changed
  over the months.
  - An edge hour holding a single lone action is a stray and is cut, along with any agent seen only in it.
  - Quiet stretches of 30 minutes or more inside a day are skipped while playing, e.g. between two sessions.
- **Where an agent stands.** Each 5-minute slice goes to the building with the most of that agent's actions:
  - `computer_use_turns` actions
  - `CONSOLIDATE` and older `SEARCH_HISTORY` events
  - for the Claude Code agent (Jan–Apr 2026), its own tool calls

  A slice with only chat puts the agent in the Town Hall. That covers the time before 2026-03-24, when agents chatted
  outside computer sessions. A slice with nothing puts it at its camp. Before its first action of the day it is not
  in the village yet.
- **The count is by actions, not time.** One chat message is one action, and a slice of bash is often ten or
  more. So the Town Hall is rarely where an agent spends the *most* of a slice, even though agents chat all day.
  The chat feed and the speech bubbles show the talking.
- **Clans** come from the model string, not the name. So o1, o3 and o4-mini are OpenAI, the fine-tuned Kimi leaders
  are Moonshot, and the Claude Code agent is Anthropic.
- **Mentions.** A mention is another agent's exact full name in the message text, with or without `@`. The longest
  name wins, so `GPT-5` does not match inside `GPT-5.1`, and names inside URLs don't count. Short forms like
  "Opus" are skipped because they are ambiguous across versions. Human messages are left out.
- **Context up to the selected day.**
  - The memory is the agent's latest one written on or before that day.
  - "So far" counts only up to that day.
  - Individual goals exist from 2026-07-06 on.
  - Career summaries were written between Nov 2025 and Sep 2026, so the Career tab locks the ones written after the
    selected day.
- **Privacy.** Raw IPs and internal URLs are scrubbed from every text field; one agent goal listed VNC addresses.
  Screenshots aren't used yet.

**Clan colours** are a validated colour-blind-checked 8-slot palette. Colour never works alone: every figure and
column also carries its model label.

## Deploy (this EC2 box behind Cloudflare)

GitHub (`village-3d` branch) is the source of truth. The EC2 box pulls from it and serves the page and data with Caddy
from `/var/www/village-3d`. A **Cloudflare Tunnel** carries traffic: `cloudflared` on the box connects out to
Cloudflare, so the box needs no public ports, no Elastic IP and no A record. Cloudflare does HTTPS, and **Cloudflare
Access** can put a login in front, but the site is **public by decision**. The dataset's terms ask to cite AI
Digest; the ⓘ guide credits them.

**Once, in the Cloudflare dashboard:**
1. The domain is an active zone (*Websites*).
2. *Zero Trust → Networks → Tunnels → Create a tunnel* (Cloudflared, Debian 64-bit). Run the install command it shows
   yourself in an SSH session on the box; it contains a secret token. Wait for the connector to show *Healthy*.
3. On the tunnel, add a *Public Hostname*: subdomain (e.g. `village`) plus the domain, service **HTTP**,
   `localhost:8080`.
4. Optional, only if you ever want a login: *Zero Trust → Access controls → Applications → Self-hosted and
   private*, the same public hostname, a policy allowing your team's emails.
5. Optional: a *Cache Rule* for that hostname, paths `/assets/*` and `/data/*`, set to *Eligible for cache* and
   *Respect origin TTL*.

**Once, on the box:**
```bash
sudo deploy/deploy.sh tunnel       # Caddy on 127.0.0.1:8080 only, public through the tunnel; starts at boot
```

**Every release:**
```bash
deploy/deploy.sh publish           # git pull from GitHub, then copy the site to /var/www
deploy/deploy.sh publish --build   # same, but rebuild data/ from the dataset first
```
Only `index.html`, the scripts, `assets/` and `data/` are published. The extractor, docs and this kit stay private.
Logs: `journalctl -u caddy`, `journalctl -u cloudflared`, `/var/log/caddy/village-access.log`.

**Without Cloudflare:**
- Run `sudo deploy/deploy.sh setup village.example.com`. Caddy then gets the certificate itself and asks for a
  password (set `VILLAGE_USER` / `VILLAGE_PASSWORD`, or a random one is printed).
- This needs an Elastic IP, inbound TCP 80/443 in the security group, and an A record.

## Files

| File | What |
|---|---|
| `extract.py` / `test_extract.py` | dataset → `data/`, and its self-check |
| `index.html` | page, HUD, calendar, roster, player card and guide markup and styles |
| `main.js` | per-day loading, characters, replay, mention arcs, Hall of Records, roster, player card, calendar, controls |
| `town.js` | the town, assembled from kit pieces, and the building descriptions |
| `assets/` | Kenney CC0 models, only the pieces used (see `assets/LICENSE.md`) |
| `deploy/` | Caddy config template and `deploy.sh` (setup once, publish every release) |
| `todo.md` | what's done and what's next |
