#!/usr/bin/env python3
"""Build the 3D village's data from the AI Village dataset (stdlib only), one file per PT day.

  data/index.json               agents, days (date, day number, goal, who was there)
  data/days/<date>.json         that day in 5-minute slices of its village hours (first to last activity, PT)
  data/days/<date>/<slug>.json  an agent's memory as of that day and reasoning excerpts, loaded when its card opens

Data: $VILLAGE_DATA (dir with the files), else the HF cache ($HF_HUB_CACHE or ~/.cache/huggingface/hub).

  python3 extract.py                     # every day (~3 min: one pass over ~5 GB of gzipped tables)
  python3 extract.py --since 2026-09-01  # just these days, for dev loops (memory and sofar only see rows since then)
"""
import argparse, gzip, json, os, re, sys
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
SLICE = 300  # seconds of village time per replay step
HOUR = 3600 // SLICE
STRAY = 2  # an edge hour with a single active agent-slice is a stray (2026-02-24 07:xx); busier edges are real work
PTZ = ZoneInfo('America/Los_Angeles')
PERMA = '2026-03-24'  # perma-computer-use: before it, history searches were events, after it, turns
THINK, THINKS, MEMORY, LONG = 400, 150, 20000, 20000  # excerpt chars, excerpts per agent-day, memory chars, recap chars
HALL = {'send_message_back_to_chat', 'move_to_room', 'request_approval_for_unsolicited_outreach',
        'request_Google_sign_in', 'request_human_helper'}
CLANS = {'Google': ('gemini',), 'Anthropic': ('claude',), 'OpenAI': ('gpt', 'o1', 'o3', 'o4'),  # colour-palette slot order;
         'Zhipu': ('z-ai/glm',), 'Moonshot': ('kimi', 'tinker://'), 'xAI': ('grok',),     # claude-code::claude… is Anthropic,
         'DeepSeek': ('deepseek',), 'Meta': ('meta/',)}                                     # tinker:// = fine-tuned Kimi leaders
# Claude Code tool uses -> building. Its mcp__village__ computer_use/bash/pixel calls are already computer_use_turns,
# chat_message is already chat, get_events is chat polling: those are skipped so nothing counts twice.
CC = {'WebFetch': 'T', 'WebSearch': 'T', 'mcp__village__edit_memory': 'L', 'mcp__village__search_history': 'L'}
PREFIX = re.compile(r'^(Claude|GPT-|Gemini|DeepSeek-|Kimi|Grok|Muse Spark)\s*')
ADDR = re.compile(r'(https?://)?\b\d{1,3}(\.\d{1,3}){3}\b(:\d+)?\S*')  # raw IPs / internal URLs
TS = re.compile(rb'"created_at":\s*"([^"]+)"')
TAGS = re.compile(r'(?:\s*</?(?:narrative_summary|list_of_chronological_events|top_moments?|takeaways?|blurb|quote)>)+\s*')
CUT = 25  # keep this many chars past a later trim, so scrub() still sees a whole IP at the edge
HYPHENS = str.maketrans({'‐': '-', '‑': '-', '–': '-'})


def snapshot():
    if os.environ.get('VILLAGE_DATA'):
        return Path(os.environ['VILLAGE_DATA'])
    root = Path(os.environ.get('HF_HUB_CACHE') or Path.home() / '.cache/huggingface/hub') / 'datasets--aidigestorg--ai-village'
    try:
        return root / 'snapshots' / (root / 'refs' / 'main').read_text().strip()
    except FileNotFoundError:
        sys.exit(f'AI Village dataset not found under {root}; download it (see README) or set VILLAGE_DATA.')


def rows(snap, name):
    with gzip.open(snap / name, 'rb') as f:
        for line in f:
            yield json.loads(line)


def mentions_of(agents):
    """agents: {id: name} -> mentions(text, src) -> ids of other agents named in text, with or without '@'."""
    # ponytail: exact full names only; short forms ("Opus", "Gemini") are ambiguous across versions and skipped.
    lookup = {n.translate(HYPHENS).lower(): i for i, n in agents.items()}
    alt = '|'.join(re.escape(n) for n in sorted(lookup, key=len, reverse=True))  # longest name wins
    rx = re.compile(rf'(?<![\w./-])({alt})(?![\w-]|\.\d)', re.I)  # GPT-5 must not eat GPT-5.1, nor match in URLs

    def mentions(text, src):
        found = (lookup[m[1].lower()] for m in rx.finditer(text.translate(HYPHENS)))
        return list(dict.fromkeys(d for d in found if d != src))
    return mentions


def building(action):
    """computer_use_turns.agent_action -> building letter: Workshop (bash), Tower (GUI/browser),
    Hall (chat and requests to humans), Library (history search), Camp (pause). None = no action."""
    if not action:
        return None
    if 'command' in action:
        return 'W'
    a = action.get('action')
    if not a:
        return None
    return {'pause': 'C', 'search_history': 'L'}.get(a) or ('H' if a in HALL else 'T')


def cc_building(tool):
    """Claude Code tool_use name -> building letter; None = counted elsewhere (see CC)."""
    return CC.get(tool) or (None if tool.startswith('mcp__') else 'W')


def clan_of(model):
    return next((c for c, p in CLANS.items() if model.startswith(p)), None)


def label(name):
    """Short chest print: 'Claude Opus 4.8' -> 'O4.8', 'GPT-5.6 Sol' -> '5.6S', 'Fine-Tuned Leader' -> 'FTL'."""
    parts = re.split(r'[^\w.]+', PREFIX.sub('', name))
    return ''.join(p if re.search(r'\d', p) else p[0].upper() for p in parts if p)


def slugify(name):
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')


def scrub(text, n):
    return ADDR.sub('[REDACTED]', text or '')[:n]


def untag(text):
    """Summary sections (<narrative_summary>, <top_moments>, …) -> paragraphs; **bold** stays."""
    return TAGS.sub('\n\n', text or '').strip()


def pt(ts):
    """UTC 'YYYY-MM-DD HH:MM:SS.f' -> (PT date, seconds since PT midnight)."""
    t = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc).astimezone(PTZ)
    return t.date().isoformat(), t.hour * 3600 + t.minute * 60 + t.second


def thoughts(o):
    """Reasoning text anywhere in a provider-shaped model response: Anthropic thinking blocks, OpenAI reasoning
    summaries, Gemini thought parts, reasoning(_content) strings (DeepSeek, Kimi, OpenRouter)."""
    if isinstance(o, list):
        for v in o:
            yield from thoughts(v)
    elif isinstance(o, dict):
        if o.get('type') == 'thinking':
            yield o.get('thinking')
        if o.get('type') == 'reasoning':
            yield from (s.get('text') for s in o.get('summary') or [] if isinstance(s, dict))
        if o.get('thought') is True:
            yield o.get('text')
        yield o.get('reasoning_content')
        yield o.get('reasoning')
        for v in o.values():
            if isinstance(v, (dict, list)):
                yield from thoughts(v)


def thought(msg):
    text = '\n'.join(dict.fromkeys(t.strip() for t in thoughts(msg) if isinstance(t, str) and t.strip()))
    return text[:THINK + CUT]


def track(counts, said, n):
    """counts {slice: Counter(building)}, said {slices with chat} -> one letter per slice: busiest building,
    'H' for chat-only slices, 'C' for idle, '-' before the agent's first activity."""
    first = min([*counts, *said], default=n)
    return ''.join('-' if s < first else counts[s].most_common(1)[0][0] if counts.get(s) else 'H' if s in said else 'C'
                   for s in range(n))


def recent(snap, name, since):
    """rows(), but skips old lines before parsing them; created_at sits near the end of most rows."""
    with gzip.open(snap / name, 'rb') as f:
        for line in f:
            m = TS.search(line, max(0, len(line) - 600))
            if m is None or m[1].decode() >= since:
                yield json.loads(line)


def save(path, obj):
    path.write_text(json.dumps(obj, separators=(',', ':'), ensure_ascii=False), encoding='utf-8')
    return path.stat().st_size


def main():
    ap = argparse.ArgumentParser(description='Build data/ for the 3D village.')
    ap.add_argument('--since', default='', help='YYYY-MM-DD: only build PT days from this date on')
    since = ap.parse_args().since
    snap = snapshot()
    everyone = {r['id']: r for r in rows(snap, 'agents.jsonl.gz')}
    names = {i: r['name'] for i, r in everyone.items()}
    clan = {i: clan_of(r['model_string']) for i, r in everyone.items()}
    slug = {i: slugify(n) for i, n in names.items()}
    assert all(clan.values()), [everyone[i]['model_string'] for i, c in clan.items() if not c]
    assert len(set(slug.values())) == len(slug) and len({label(n) for n in names.values()}) == len(names)
    txt = (snap / 'village-transcript.json').read_bytes()  # bytes: as str it would take 3 GB (wide chars)
    daynum = {d.decode(): int(n) for n, d in re.findall(rb'"day":\s*(\d+),\s*"date":\s*"([\d-]+)"', txt)}
    del txt

    # everything keyed (PT date, agent); slices count from PT midnight until the day's window is known
    counts = defaultdict(lambda: defaultdict(Counter))  # -> slice -> building -> n
    said = defaultdict(set)    # -> slices with a chat message
    stats = defaultdict(Counter)
    bash = defaultdict(dict)   # -> slice -> (second, first bash line), earliest wins
    think = defaultdict(dict)  # -> reasoning excerpt -> second
    intents = defaultdict(list)
    chat = defaultdict(list)   # date -> (second, agent, text, mentioned)

    def first_bash(k, s, cmd):
        line = (s, scrub(cmd.strip().split('\n')[0], 160))
        bash[k][s // SLICE] = min(bash[k].get(s // SLICE, line), line)

    mentions = mentions_of(names)
    for m in rows(snap, 'chat_messages.jsonl.gz'):  # humans (USER_TALK) stay out
        if m['speaker_type'] != 'agent' or m['created_at'] < since:
            continue
        (d, s), src = pt(m['created_at']), m['agent_speaker_id']
        to = mentions(m['content'] or '', src)
        chat[d].append((s, src, scrub(m['content'], 1500), to))
        said[d, src].add(s // SLICE)
        stats[d, src]['messages'] += 1
        stats[d, src]['mentions_out'] += len(to)
        for x in to:
            stats[d, x]['mentions_in'] += 1

    sess_agent = {}
    for r in rows(snap, 'computer_use_sessions.jsonl.gz'):
        sess_agent[r['id']] = r['agent_id']
        if r['created_at'] >= since:
            d, s = pt(r['created_at'])
            intents[d, r['agent_id']].append((s, scrub(r['short_displayed_session_goal'], 120), scrub(r['session_goal'], 500)))

    for e in recent(snap, 'events.jsonl.gz', since):
        x, kind = e['data'], e['data'].get('actionType')
        if kind == 'CONSOLIDATE' or kind == 'SEARCH_HISTORY' and e['created_at'] < PERMA:
            d, s = pt(e['created_at'])
            counts[d, x['agentId']][s // SLICE]['L'] += 1
            stats[d, x['agentId']]['memory' if kind == 'CONSOLIDATE' else 'searches'] += 1

    for t in recent(snap, 'computer_use_turns.jsonl.gz', since):
        agent, a = sess_agent.get(t['session_id']), t['agent_action']
        if agent is None:
            continue
        d, s = pt(t['created_at'])
        k, b = (d, agent), building(a)
        if th := thought(t['agent_messages']):
            think[k].setdefault(th, s)
        if b is None:
            continue
        counts[k][s // SLICE][b] += 1
        st = stats[k]
        st['turns'] += 1
        st[b] += 1
        st['errors'] += bool(t['error'])
        st['searches'] += a.get('action') == 'search_history'
        if b == 'W' and a['command']:
            first_bash(k, s, a['command'])

    for r in recent(snap, 'claude_code_messages.jsonl.gz', since):  # the Claude Code agent's own tool calls
        if r['message_type'] != 'assistant':
            continue
        d, s = pt(r['created_at'])
        k = (d, r['agent_id'])
        if th := thought(r['content']):
            think[k].setdefault(th, s)
        for u in (r['content'].get('message') or {}).get('content') or []:
            if u.get('type') == 'tool_use' and (b := cc_building(u['name'])):
                counts[k][s // SLICE][b] += 1
                stats[k]['turns'] += 1
                stats[k][b] += 1
                if u['name'] == 'Bash' and (u.get('input') or {}).get('command'):
                    first_bash(k, s, u['input']['command'])

    by_day = defaultdict(list)
    for d, a in set(counts) | set(said):
        if d >= since:
            by_day[d].append(a)
    days = sorted(by_day)
    active = defaultdict(list)  # agent -> its active dates
    for d in days:
        for a in by_day[d]:
            active[a].append(d)

    memo = {}  # (first active date on/after it was written, agent) -> (created_at, text): latest wins
    for r in recent(snap, 'agent_memories.jsonl.gz', since):
        a, ds = r['agent_id'], active.get(r['agent_id'])
        if not ds:
            continue
        i = bisect_left(ds, pt(r['created_at'])[0])
        if i < len(ds) and r['created_at'] > memo.get((ds[i], a), ('',))[0]:
            memo[ds[i], a] = (r['created_at'], r['content'][:MEMORY + CUT])

    vgoals = sorted(rows(snap, 'village_goals.jsonl.gz'), key=lambda g: g['start_time'])
    agoals = sorted(rows(snap, 'agent_goals.jsonl.gz'), key=lambda g: g['start_time'] or '')
    for g in agoals:
        g['from'], g['to'] = pt(g['start_time'])[0] if g['start_time'] else '', pt(g['end_time'])[0] if g['end_time'] else '~'
        if len(g.get('short_name') or '') > len(g.get('name') or ''):  # one row has role and goal swapped
            g['short_name'], g['name'] = g['name'], g['short_name']
    summaries = sorted(rows(snap, 'summaries.jsonl.gz'), key=lambda r: r['created_at'])  # latest wins below
    recaps = {r['summary_date']: r['content'] for r in summaries if r['type'] == 'daily'}
    careers = {r['summary_target']: r for r in summaries if r['type'] == 'agent'}

    out = HERE / 'data'
    (out / 'days').mkdir(parents=True, exist_ok=True)
    sofar, memory, index_days, sizes, extra, dropped, gaps = defaultdict(Counter), {}, [], [], [], Counter(), []
    for d in days:
        order = sorted(by_day[d], key=lambda i: (list(CLANS).index(clan[i]), everyone[i]['created_at']))
        # window = first..last busy hour; edge hours with one lone action (2026-02-24) don't count.
        # Quiet gaps inside the day stay (two sessions on 2025-06-18); the player skips them.
        per_hour = Counter(s // HOUR for a in order for s in (*counts[d, a], *said[d, a]))
        busy = [h for h, n in per_hour.items() if n >= STRAY] or list(per_hour)
        open_h = min(busy)
        hours = max(busy) + 1 - open_h
        inside = lambda s: open_h * HOUR <= s < (open_h + hours) * HOUR
        order = [a for a in order if any(map(inside, (*counts[d, a], *said[d, a])))]  # active only in a trimmed stray: not here
        idx = {a: n for n, a in enumerate(order)}
        o, base, span = open_h * HOUR, open_h * 3600, hours * 3600
        at_open = datetime.fromisoformat(f'{d} {open_h:02}:00').replace(tzinfo=PTZ).astimezone(timezone.utc).isoformat(' ')[:19]
        vg = [g for g in vgoals if g['start_time'] <= at_open]
        if not vg or (vg[-1]['end_time'] or '~') <= at_open:
            gaps.append(d)
        (dd := out / 'days' / d).mkdir(exist_ok=True)
        agents = []
        for a in order:
            k, name = (d, a), names[a]
            partners = Counter()
            for _, src, _, to in chat[d]:
                for x in to:
                    if src == a and x in idx: partners[(x, 0)] += 1
                    if x == a and src in idx: partners[(src, 1)] += 1
            tops = Counter()
            for (p, _), n in partners.items():
                tops[p] += n
            g = next((g for g in reversed(agoals) if g['agent_id'] == a and g['from'] <= d < g['to']), {})
            ins = sorted((s - base, short, goal) for s, short, goal in intents[k])
            dropped['intents'] += sum(not 0 <= v < span for v, *_ in ins)
            sofar[a].update(days=1, turns=stats[k]['turns'], messages=stats[k]['messages'])
            agents.append({
                'slug': slug[a], 'name': name, 'model': everyone[a]['model_string'], 'clan': clan[a], 'label': label(name),
                'joined': active[a][0],
                'role': g.get('short_name') or '', 'goal': scrub(g.get('name'), 300), 'note': scrub(g.get('description'), 300),
                'track': track({s - o: c for s, c in counts[k].items()}, {s - o for s in said[k]}, hours * HOUR),
                'stats': dict(stats[k]),
                'partners': [[idx[p], partners[(p, 0)], partners[(p, 1)]] for p, _ in tops.most_common(8)],
                'intents': [i for i in ins if 0 <= i[0] < span],
                'bash': {s - o: line for s, (_, line) in sorted(bash[k].items())},
                'sofar': dict(sofar[a]),
            })
            memory[a] = memo.get(k) or memory.get(a)
            th = sorted((s, t) for t, s in think[k].items() if base <= s < base + span)
            th = [th[i * len(th) // THINKS] for i in range(THINKS)] if len(th) > THINKS else th  # evenly over the day
            mem = None
            if memory[a]:
                md, ms = pt(memory[a][0])
                mem = {'written': f'{md} {ms // 3600:02}:{ms % 3600 // 60:02}', 'text': scrub(memory[a][1], MEMORY)}
            extra.append(save(dd / f'{slug[a]}.json', {'memory': mem, 'thinking': [[s - base, scrub(t, THINK)] for s, t in th]}))
        messages = sorted([s - base, idx[src], text, [idx[x] for x in to if x in idx]]
                          for s, src, text, to in chat[d] if base <= s < base + span)
        dropped['messages'] += len(chat[d]) - len(messages)
        goal = scrub(vg[-1]['goal'], 1000) if vg else ''
        sizes.append(save(out / 'days' / f'{d}.json', {
            'date': d, 'day': daynum.get(d), 'open': open_h, 'hours': hours, 'slice': SLICE,
            'days': [{'day': daynum.get(d), 'date': d}], 'goal': goal,
            'recap': scrub(untag(recaps[d]), LONG) if d in recaps else None,
            'agents': agents, 'messages': messages,
        }))
        index_days.append({'date': d, 'day': daynum.get(d), 'goal': goal, 'agents': [slug[a] for a in order],
                           'messages': len(messages), 'turns': sum(stats[d, a]['turns'] for a in order)})

    career = lambda r: r and {'written': pt(r['created_at'])[0], 'text': scrub(untag(r['content']), LONG)}
    save(out / 'index.json', {
        'export': json.loads((snap / 'manifest.json').read_text())['exportedAt'][:10],
        'clans': list(CLANS),
        'agents': {slug[a]: {'name': names[a], 'model': everyone[a]['model_string'], 'clan': clan[a], 'label': label(names[a]),
                             'joined': ds[0], 'last': ds[-1], 'career': career(careers.get(names[a]))}
                   for a, ds in sorted(active.items(), key=lambda x: x[1][0])},
        'days': index_days,
    })
    kb = lambda xs: f'{min(xs) / 1e3:.0f}/{median(xs) / 1e3:.0f}/{max(xs) / 1e3:.0f} KB (min/median/max), {sum(xs) / 1e6:.0f} MB'
    print(f'{out.name}/: {len(days)} days {days[0]}..{days[-1]}, {len(active)} agents; day files {kb(sizes)}; '
          f'agent-day files ({len(extra)}) {kb(extra)}; dropped outside the day window: {dict(dropped)}; '
          f'days outside any village goal: {gaps}')


if __name__ == '__main__':
    main()
