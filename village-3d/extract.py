#!/usr/bin/env python3
"""Build data.json for the 3D village from the AI Village dataset (stdlib only).

Covers the last 7 days of the dataset (anchored on the newest chat message) as 5-minute slices
of village time (village hours only, weekends skipped).
Data: $VILLAGE_DATA (dir with the files), else the HF cache ($HF_HUB_CACHE or ~/.cache/huggingface/hub).

  python3 extract.py        # writes data.json next to this file (~40 s: scans 2.2 GB of computer-use turns)
"""
import gzip, json, os, re, sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DAYS_BACK = 7
SLICE = 300  # seconds of village time per replay step
PT = timedelta(hours=-7)  # ponytail: fixed PDT offset, fine for a Sep window; use zoneinfo if a window crosses DST
HALL = {'send_message_back_to_chat', 'move_to_room', 'request_approval_for_unsolicited_outreach',
        'request_Google_sign_in', 'request_human_helper'}
CLANS = [('Google', 'gemini'), ('Anthropic', 'claude'), ('OpenAI', 'gpt'), ('Zhipu', 'glm'),  # colour-palette slot order
         ('Moonshot', 'kimi'), ('xAI', 'grok'), ('DeepSeek', 'deepseek'), ('Meta', 'muse')]
PREFIX = re.compile(r'^(Claude|GPT-|Gemini|DeepSeek-|GLM-|Kimi|Grok|Muse Spark)\s*')
ADDR = re.compile(r'(https?://)?\b\d{1,3}(\.\d{1,3}){3}\b(:\d+)?\S*')  # raw IPs / internal URLs
TS = re.compile(rb'"created_at":\s*"([^"]+)"')
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


def label(name):
    """Short chest print: 'Claude Opus 4.8' -> 'O4.8', 'GPT-5.6 Sol' -> '5.6S', 'DeepSeek-V4-Pro' -> 'V4P'."""
    parts = re.split(r'[\s-]+', PREFIX.sub('', name))
    return ''.join(p if re.search(r'\d', p) else p[0].upper() for p in parts if p)


def scrub(text, n):
    return ADDR.sub('[REDACTED]', text or '')[:n]


def clock(days, open_h, hours):
    """-> vtime(ts): UTC 'YYYY-MM-DD HH:MM:SS.f' -> seconds of village time since day 0 opened,
    or None outside village hours. Days are laid end to end, nights and weekends cut out."""
    span = hours * 3600

    def vtime(ts):
        t = datetime.fromisoformat(ts) + PT
        d = days.get(t.date().isoformat())
        s = (t - t.replace(hour=open_h, minute=0, second=0, microsecond=0)).total_seconds()
        return None if d is None or not 0 <= s < span else int(d * span + s)
    return vtime


def recent(snap, name, since):
    """rows(), but skips old lines before parsing them; created_at sits near the end of every row."""
    with gzip.open(snap / name, 'rb') as f:
        for line in f:
            m = TS.search(line, max(0, len(line) - 600))
            if m is None or m[1].decode() >= since:
                yield json.loads(line)


def main():
    snap = snapshot()
    everyone = {r['id']: r for r in rows(snap, 'agents.jsonl.gz')}
    names = {i: r['name'] for i, r in everyone.items()}
    win = next(rows(snap, 'villages.jsonl.gz'))['schedule']['windows'][0]
    open_h, close_h = int(win['start'][:2]), int(win['end'][:2])

    msgs = list(rows(snap, 'chat_messages.jsonl.gz'))
    since = (datetime.fromisoformat(max(m['created_at'] for m in msgs)) - timedelta(days=DAYS_BACK)).isoformat(' ')
    msgs = [m for m in msgs if m['created_at'] >= since and m['speaker_type'] == 'agent']
    dates = sorted({(datetime.fromisoformat(m['created_at']) + PT).date().isoformat() for m in msgs})
    vtime = clock({d: i for i, d in enumerate(dates)}, open_h, close_h - open_h)
    txt = (snap / 'village-transcript.json').read_text()
    daynum = {d: int(n) for n, d in re.findall(r'"day":\s*(\d+),\s*"date":\s*"([\d-]+)"', txt)}
    del txt

    slices = len(dates) * (close_h - open_h) * 3600 // SLICE
    counts = defaultdict(lambda: defaultdict(Counter))  # agent -> slice -> building -> n
    stats = defaultdict(Counter)
    bash = defaultdict(dict)  # agent -> slice -> first bash line
    dropped = Counter()

    def hit(agent, ts, b, n=1):
        v = vtime(ts)
        if v is None:
            dropped[b] += n
            return None
        counts[agent][v // SLICE][b] += n
        return v

    mentions = mentions_of(names)
    chat = []
    for m in msgs:
        src = m['agent_speaker_id']
        v = vtime(m['created_at'])
        if v is None:
            dropped['msg'] += 1
            continue
        to = mentions(m['content'] or '', src)
        chat.append((v, src, scrub(m['content'], 1500), to))
        stats[src]['messages'] += 1
        stats[src]['mentions_out'] += len(to)
        for d in to:
            stats[d]['mentions_in'] += 1

    intents = defaultdict(list)
    sess_agent = {}
    for s in rows(snap, 'computer_use_sessions.jsonl.gz'):
        sess_agent[s['id']] = s['agent_id']
        v = vtime(s['created_at']) if s['created_at'] >= since else None
        if v is not None:
            intents[s['agent_id']].append((v, scrub(s['short_displayed_session_goal'], 120), scrub(s['session_goal'], 500)))

    for e in recent(snap, 'events.jsonl.gz', since):
        d = e['data']
        if e['created_at'] >= since and d.get('actionType') == 'CONSOLIDATE':
            if hit(d['agentId'], e['created_at'], 'L') is not None:
                stats[d['agentId']]['memory'] += 1

    for t in recent(snap, 'computer_use_turns.jsonl.gz', since):
        agent, a = sess_agent.get(t['session_id']), t['agent_action']
        b = building(a)
        if t['created_at'] < since or agent is None or b is None:
            continue
        v = hit(agent, t['created_at'], b)
        if v is None:
            continue
        st = stats[agent]
        st['turns'] += 1
        st[b] += 1
        st['errors'] += bool(t['error'])
        st['searches'] += a.get('action') == 'search_history'
        if b == 'W' and v // SLICE not in bash[agent]:
            bash[agent][v // SLICE] = scrub(a['command'].strip().split('\n')[0], 160)

    active = [i for i in everyone if counts.get(i) or stats[i]['messages']]
    clan = {i: next(c for c, key in CLANS if key in names[i].lower()) for i in active}
    order = sorted(active, key=lambda i: ([c for c, _ in CLANS].index(clan[i]), everyone[i]['created_at']))
    idx = {a: n for n, a in enumerate(order)}

    goals = {}
    for g in sorted(rows(snap, 'agent_goals.jsonl.gz'), key=lambda g: g['start_time'] or ''):
        if g['end_time'] is None:
            goals[g['agent_id']] = g

    agents = []
    for i in order:
        c = counts[i]
        first = min(c) if c else min(v // SLICE for v, s, *_ in chat if s == i)
        track = ''.join('-' if s < first else (c[s].most_common(1)[0][0] if c.get(s) else 'C') for s in range(slices))
        partners = Counter()
        for v, s, _, to in chat:
            for d in to:
                if s == i and d in idx: partners[(d, 0)] += 1
                if d == i and s in idx: partners[(s, 1)] += 1
        tops = Counter()
        for (p, _), n in partners.items():
            tops[p] += n
        g = dict(goals.get(i, {}))
        if len(g.get('short_name') or '') > len(g.get('name') or ''):  # one row has role and goal swapped
            g['short_name'], g['name'] = g['name'], g['short_name']
        agents.append({
            'name': names[i], 'model': everyone[i]['model_string'], 'clan': clan[i], 'label': label(names[i]),
            'joined': everyone[i]['created_at'][:10],
            'role': g.get('short_name', ''), 'goal': scrub(g.get('name'), 300), 'note': scrub(g.get('description'), 300),
            'track': track, 'stats': dict(stats[i]),
            'partners': [[idx[p], partners[(p, 0)], partners[(p, 1)]] for p, _ in tops.most_common(8)],
            'intents': sorted(intents[i]), 'bash': bash[i],
        })

    out = {
        'days': [{'day': daynum.get(d), 'date': d} for d in dates],
        'open': open_h, 'hours': close_h - open_h, 'slice': SLICE, 'since_utc': since,
        'agents': agents,
        'messages': sorted([v, idx[s], text, [idx[d] for d in to if d in idx]] for v, s, text, to in chat if s in idx),
    }
    path = HERE / 'data.json'
    path.write_text(json.dumps(out, separators=(',', ':')))
    print(f'{path.name}: {len(agents)} agents, {len(out["messages"])} messages, {slices} slices, '
          f'{sum(a["stats"].get("turns", 0) for a in agents)} turns, {path.stat().st_size / 1e6:.1f} MB; '
          f'outside village hours (dropped): {dict(dropped)}')


if __name__ == '__main__':
    main()
