# village-graph

Who talks to whom in the [AI Village](https://theaidigest.org/village) agent swarm. This tool turns the
[`aidigestorg/ai-village`](https://huggingface.co/datasets/aidigestorg/ai-village) chat log into an
interaction graph and answers questions from a CLI:

- which agents did X interact with?
- how often do A and B connect, in each direction, and when?
- which pairs are strongest overall, and which agents are the hubs?

It uses only the Python stdlib. The graph is stored in a single SQLite file (`village.db`).

## Why SQLite and not a graph database

The graph is small: 46 agent nodes plus one `Human` node. The full history has about 150k edges, and the
last 7 days about 4k. Every question above is a one-hop aggregate (`GROUP BY` over an edge table), which
SQLite answers instantly.

A graph DB with Cypher (Neo4j, Kuzu) pays off for deep multi-hop traversals over large graphs. Here it
would add a server or a dependency and buy nothing. If you later want Cypher or a visual explorer, the
`edges` table loads straight into one.

## Setup

**1. Get the dataset.** It is gated, so request access on the HF page first. Only five files are needed
(about 360 MB):

```bash
uvx --from huggingface_hub hf auth login
uvx --from huggingface_hub hf download aidigestorg/ai-village --repo-type dataset \
  agents.jsonl.gz chat_rooms.jsonl.gz village_goals.jsonl.gz events.jsonl.gz chat_messages.jsonl.gz
```

The tool finds the data in the HF cache (`$HF_HUB_CACHE`, default `~/.cache/huggingface/hub`) and always
uses the latest downloaded snapshot. To use a plain folder of `.jsonl.gz` files instead, set
`VILLAGE_DATA=/path/to/folder`.

**2. Install into a uv venv:**

```bash
cd village-graph
uv sync                               # creates .venv and installs the `village-graph` command
uv run python test_village_graph.py   # self-check of the mention extractor, prints "ok"
```

## Usage

```bash
uv run village-graph build            # last 7 days of the dataset (default), ~6 s
uv run village-graph build --days 0   # full history since 2025-04-02, ~22 s
uv run village-graph build --days 30  # any window, counted back from the newest message
```

The window is anchored on the newest message in the dataset, not on today's date. Query commands build
the default 7-day graph automatically if `village.db` is missing.

| Command | Answers |
|---|---|
| `pair A B [--by day\|month]` | How often A and B connect: A→B and B→A split into `@` and named, first and last contact, and a per-day (or per-month) timeline |
| `neighbors A` | Who A interacted with, ranked, with outgoing and incoming counts |
| `top-pairs` | The strongest pairs across the village |
| `hubs` | Agents ranked by number of distinct partners, then by volume |
| `agents` | Roster: model, messages sent, partners, mentions out and in, first and last message |
| `examples A B` | The actual messages behind A → B, newest first (default 10), so any count can be checked |
| `ignored` | One-sided pairs: A mentions B much more than B mentions A back |
| `replies [A] [--within 10]` | When @-mentioned, how often and how fast each agent replies. Given an agent, breaks it down by who asked. |
| `goals` | Village-wide goals, newest first, with the edge count in each goal's period (use the text with `--goal`) |

Filters that work on every query command:

| Filter | Meaning |
|---|---|
| `--since 2026-09-01` / `--until 2026-09-03` | UTC; `--until` is exclusive |
| `--room general` | Only edges from that chat room |
| `--kind addressed` / `--kind named` | Only one edge kind (default counts both) |
| `--goal "hardest game"` | Only the period of the village goal whose text contains this string (must match exactly one goal) |
| `--limit 20` | Maximum rows returned |
| `--samples 5` | Also list the newest messages behind the result's edges (on by default; `0` turns it off) |

Agent names can be abbreviated to any unique, case-insensitive substring, for example `"opus 4.8"`,
`gemini 2.5` or `human`. An ambiguous name lists its candidates.

```text
$ uv run village-graph pair "opus 4.8" "gemini 2.5"
# graph covers 2026-08-31 16:01 -> 2026-09-05 00:00
direction                          @    named  total  first             last
Claude Opus 4.8 -> Gemini 2.5 Pro  176  204    380    2026-08-31 16:01  2026-09-04 23:59
Gemini 2.5 Pro -> Claude Opus 4.8  166  28     194    2026-08-31 16:01  2026-09-05 00:00

day         Claude Opus 4.8 -> Gemini 2.5 Pro  Gemini 2.5 Pro -> Claude Opus 4.8
2026-08-31  60                                 38
2026-09-01  82                                 17
...
```

## Web UI

```bash
uv run village-graph web              # http://127.0.0.1:8765  (--port / --host to change)
```

Type any CLI command into the box, without the `village-graph` prefix, e.g. `top-pairs --limit 40`,
`hubs --since 2026-09-02`, `pair "Claude Opus 4.8" "Gemini 2.5 Pro"` or `build --days 30`. The result
is drawn as a graph with the same tables underneath.

- **Node size** is interaction volume. **Edge width** is the count for that direction; hover an edge to
  see the number. Orange nodes are the ones you asked about.
- **Click a node** to run `neighbors` for it. **Click an edge** to run `pair` for its two ends. The
  current `--since`, `--until`, `--room`, `--goal` and `--kind` filters carry over.
- The command lives in the URL hash, so the browser's back button works and any view can be shared as a
  link.
- `hubs` draws the edges among the hubs it lists. `-h` shows the CLI help.

The server uses only the stdlib and handles one request at a time, so a `build` from the UI blocks until
it finishes. It binds to localhost. On a remote machine, tunnel it with
`ssh -L 8765:127.0.0.1:8765 <host>` rather than using `--host 0.0.0.0`, since the server has no auth.
The graph library (Cytoscape.js) loads from cdnjs, so the browser needs internet access.

## How an interaction is defined

The dataset has **no reply-to or recipient field**, so edges come from the message text:

- **addressed**: A's message contains `@<B's exact full name>`, e.g. `@Claude Opus 4.8`.
- **named**: A's message contains B's full name without the `@`.
- **One edge per (message, target).** A message with several targets gives several edges. If a target is
  both `@`'d and named in one message, the edge is `addressed`. Self-mentions are dropped.
- **Humans are merged into a single `Human` node.**
  - Human → agent edges come from human chat messages.
  - Agent → Human edges come from an agent writing `@handle`, where the handle is a known human display
    name.
  - The `automated` nudger bot is excluded.

Known limits:

- **Short or ambiguous names are skipped.** "Opus", "Gemini", "Sonnet" and "Claude" cover several agents,
  and two different agents are both "Opus 4.5". This undercounts periods when agents used nicknames.
- **Matching guards against common traps.** `GPT-5` does not match `GPT-5.1`, `o3` does not match inside
  URLs, and non-breaking hyphens (`DeepSeek‑V3.2`) are normalised.
- **Use of `@` varies a lot over time**, from about 2% of messages in mid-2025 to over 40% in mid-2026.
  Compare periods on `total` rather than `@` alone.
- **Humans are only reachable through single-token display names.** `@Larissa Schiavo` is not matched.
- **`replies` counts any post, not just answers.** An @-mention counts as replied when the addressee posts
  anything in the same room within `--within` minutes; the post isn't necessarily an answer to the asker.
  Very chatty agents therefore score high. Use `examples` to read the actual exchange.

## Ad-hoc SQL

```text
nodes(id, name, model)                   -- 46 agents + ('human', 'Human', '')
edges(msg_id, src, dst, kind, room, ts)  -- kind: addressed | named; ts: UTC 'YYYY-MM-DD HH:MM:SS.ffffff'
messages(id, src, room, ts, content)     -- every chat message in the window (bot excluded)
goals(goal, start_time, end_time)        -- village-wide goals; end_time NULL = ongoing
```

```bash
uv run python -c "import sqlite3; print(sqlite3.connect('village.db').execute(
  \"select room, count(*) from edges group by room order by 2 desc\").fetchall())"
```

`edges.msg_id` joins to `messages.id` for the message text. The full-history database is about 185 MB.
After upgrading the tool, re-run `build`, since the schema can change between versions.
