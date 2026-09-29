#!/usr/bin/env python3
"""Who-talks-to-whom graph for the AI Village dataset (stdlib only).

Edges come from chat text (there is no reply/recipient field):
  addressed = A wrote "@<B's full name>", named = A wrote B's full name without "@".
All humans are merged into one "Human" node; the "automated" nudger bot is dropped.

  village-graph build [--days 7]      # 0 = full history
  village-graph pair "Claude Opus 4.8" "DeepSeek-V3.2" [--by month]
  village-graph neighbors "GPT-5.2"
  village-graph top-pairs --since 2026-09-02
  village-graph hubs --goal "hardest game"
  village-graph agents | goals | ignored | replies [A] [--within 10] | examples A B
  village-graph web [--port 8765]     # same commands, drawn as an interactive graph in the browser
Filters on every query: --since/--until (UTC, until exclusive) --room --kind --goal --limit
Each result also lists the newest messages behind its edges (--samples N, 0 = off).
Data: $VILLAGE_DATA (dir with the .jsonl.gz files), else the HF cache ($HF_HUB_CACHE or ~/.cache/huggingface/hub).
"""
import argparse, contextlib, gzip, io, json, os, re, shlex, sqlite3, statistics, sys
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
DB = HERE / 'village.db'
HYPHENS = str.maketrans({'‐': '-', '‑': '-', '–': '-'})
HANDLE = re.compile(r'(?<![\w.])@([\w.-]+)')
NOT_HUMAN = {'automated', 'all', 'team', 'everyone', 'agents'}


def extractor(agents, humans):
    """agents: {id: name}; humans: lowercase handles. Returns mentions(text, src) -> {dst: kind}."""
    # ponytail: exact full names only; short forms ("Opus", "Gemini") are ambiguous across versions and skipped.
    lookup = {n.translate(HYPHENS).lower(): i for i, n in agents.items()}
    alt = '|'.join(re.escape(n) for n in sorted(lookup, key=len, reverse=True))  # longest name wins
    rx = re.compile(rf'(@)?(?<![\w./-])({alt})(?![\w-]|\.\d)', re.I)  # GPT-5 must not eat GPT-5.1

    def mentions(text, src):
        text, out = text.translate(HYPHENS), {}
        for m in rx.finditer(text):
            dst = lookup[m[2].lower()]
            if dst != src and out.get(dst) != 'addressed':
                out[dst] = 'addressed' if m[1] else 'named'
        if src != 'human':
            # ponytail: only single-token display names are reachable via @handle.
            if any(h.rstrip('.-').lower() in humans for h in HANDLE.findall(rx.sub(' ', text))):
                out['human'] = 'addressed'
        return out
    return mentions


def rows(snap, name):
    with gzip.open(snap / name, 'rb') as f:
        for line in f:
            yield json.loads(line)


def snapshot():
    if os.environ.get('VILLAGE_DATA'):
        return Path(os.environ['VILLAGE_DATA'])
    cache = Path(os.environ.get('HF_HUB_CACHE') or Path.home() / '.cache/huggingface/hub')
    root = cache / 'datasets--aidigestorg--ai-village'
    try:
        return root / 'snapshots' / (root / 'refs' / 'main').read_text().strip()
    except FileNotFoundError:
        sys.exit(f'AI Village dataset not found under {root}; download it (see README) or set VILLAGE_DATA.')


SCHEMA = '''
    CREATE TABLE nodes(id TEXT PRIMARY KEY, name TEXT UNIQUE, model TEXT);
    CREATE TABLE edges(msg_id TEXT, src TEXT, dst TEXT, kind TEXT, room TEXT, ts TEXT, PRIMARY KEY(msg_id, dst));
    CREATE TABLE messages(id TEXT PRIMARY KEY, src TEXT, room TEXT, ts TEXT, content TEXT);
    CREATE INDEX messages_by_speaker ON messages(src, room, ts);  -- reply lookups in `replies`
    CREATE TABLE goals(goal TEXT, start_time TEXT, end_time TEXT);'''


def build(days):
    snap = snapshot()
    roster = list(rows(snap, 'agents.jsonl.gz'))
    agents = {r['id']: r['name'] for r in roster}
    rooms = {r['id']: r['name'] for r in rows(snap, 'chat_rooms.jsonl.gz')}
    goals = [(r['goal'], r['start_time'], r['end_time']) for r in rows(snap, 'village_goals.jsonl.gz')]

    speaker = {}  # chat message id -> human display name
    with gzip.open(snap / 'events.jsonl.gz', 'rb') as f:
        for line in f:
            if b'"USER_TALK"' in line:
                d = json.loads(line)['data']
                if d.get('actionType') == 'USER_TALK':
                    speaker[d['messageId']] = d.get('speakerName') or ''
    agent_names = {n.lower() for n in agents.values()}
    humans = {n.lower() for n in speaker.values() if len(n) >= 3} - NOT_HUMAN - agent_names
    mentions = extractor(agents, humans)

    msgs = list(rows(snap, 'chat_messages.jsonl.gz'))
    cutoff = ''
    if days:
        latest = datetime.fromisoformat(max(m['created_at'] for m in msgs))
        cutoff = (latest - timedelta(days=days)).isoformat(' ')
    msgs = [m for m in msgs if m['created_at'] >= cutoff]

    edges, messages = [], []
    for m in msgs:
        if m['speaker_type'] == 'agent':
            src = m['agent_speaker_id']
        elif speaker.get(m['id']) == 'automated':
            continue
        else:
            src = 'human'
        room = rooms.get(m['room_id'])
        messages.append((m['id'], src, room, m['created_at'], m['content'] or ''))
        for dst, kind in mentions(m['content'] or '', src).items():
            edges.append((m['id'], src, dst, kind, room, m['created_at']))

    tmp = DB.with_suffix('.tmp')
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)
    con.executemany('INSERT INTO nodes VALUES (?,?,?)',
                    [*((r['id'], r['name'], r['model_string']) for r in roster), ('human', 'Human', '')])
    con.executemany('INSERT INTO edges VALUES (?,?,?,?,?,?)', edges)
    con.executemany('INSERT INTO messages VALUES (?,?,?,?,?)', messages)
    con.executemany('INSERT INTO goals VALUES (?,?,?)', goals)
    con.commit()
    con.close()
    tmp.replace(DB)  # swap only after a complete build

    ts = [m['created_at'] for m in msgs] or ['-']
    print(f'window: {min(ts)[:19]} -> {max(ts)[:19]}  '
          f'({len(msgs)} messages, {"last %d days" % days if days else "full history"})')
    kinds = {}
    for e in edges:
        kinds[e[3]] = kinds.get(e[3], 0) + 1
    print(f'edges: {len(edges)}  ' + '  '.join(f'{k}={v}' for k, v in sorted(kinds.items())))


# message text for tables: first 300 chars on one line
SNIPPET = ("replace(substr(m.content,1,300), char(10), ' ') "
           "|| CASE WHEN length(m.content) > 300 THEN '…' ELSE '' END")


def where(con, a, t=''):
    """SQL filter from the shared flags; t is a column prefix such as 'e.' for joins."""
    sql, p = ['1=1'], []
    if a.since: sql.append(f'{t}ts >= ?'); p.append(a.since)
    if a.until: sql.append(f'{t}ts < ?'); p.append(a.until)
    if a.room: sql.append(f'{t}room = ?'); p.append(a.room)
    if a.kind: sql.append(f'{t}kind = ?'); p.append(a.kind)
    if a.goal:
        g = con.execute('SELECT goal, start_time, end_time FROM goals WHERE goal LIKE ? ORDER BY start_time',
                        (f'%{a.goal}%',)).fetchall()
        if len(g) != 1:
            sys.exit(f'--goal {a.goal!r} matched {len(g)} goals:\n' +
                     '\n'.join(f'  {s[:10]}  {t[:80]!r}' for t, s, _ in g))
        sql.append(f'{t}ts >= ? AND (? IS NULL OR {t}ts < ?)'); p += [g[0][1], g[0][2], g[0][2]]
    return ' AND '.join(sql), p


def degrees(con, w, p):
    """{node id: [partners, out, in, total]} under the filter."""
    return {n: r for n, *r in con.execute(
        f"SELECT n, count(DISTINCT o), sum(out_), sum(1-out_), count(*) FROM ("
        f"  SELECT src n, dst o, 1 out_ FROM edges WHERE {w} UNION ALL"
        f"  SELECT dst, src, 0 FROM edges WHERE {w}) GROUP BY n", (*p, *p))}


def among(con, ids, w, p, names):
    """Directed edge weights between the given nodes, for drawing."""
    qs = ','.join('?' * len(ids))
    return [(names[s], names[d], c) for s, d, c in con.execute(
        f"SELECT src, dst, count(*) FROM edges WHERE src IN ({qs}) AND dst IN ({qs}) AND {w} GROUP BY src, dst",
        (*ids, *ids, *p))]


def node(con, q):
    all_ = con.execute('SELECT id, name FROM nodes').fetchall()
    hits = [r for r in all_ if r[1].lower() == q.lower()] or [r for r in all_ if q.lower() in r[1].lower()]
    if len(hits) != 1:
        sys.exit(f'{q!r} matches {len(hits)} nodes: ' + ', '.join(r[1] for r in hits))
    return hits[0]


def table(headers, rows):
    rows = [['' if v is None else str(v) for v in r] for r in rows]
    widths = [max(map(len, col)) for col in zip(headers, *rows)]
    for r in [headers, *rows]:
        print('  '.join(v.ljust(w) for v, w in zip(r, widths)).rstrip())


def parser():
    ap = argparse.ArgumentParser(prog='village-graph', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('build', help='rebuild village.db from the dataset').add_argument('--days', type=int, default=7, help='last N days of data; 0 = all')
    f = argparse.ArgumentParser(add_help=False)
    f.add_argument('--since'); f.add_argument('--until'); f.add_argument('--room'); f.add_argument('--goal')
    f.add_argument('--kind', choices=['addressed', 'named'])
    f.add_argument('--limit', type=int, default=20)
    f.add_argument('--samples', type=int, default=5, help='sample messages shown under each result (0 = off)')
    p = sub.add_parser('pair', parents=[f], help='how often A and B connect, both directions, over time')
    p.add_argument('a'); p.add_argument('b')
    p.add_argument('--by', choices=['day', 'month'], default='day')
    sub.add_parser('neighbors', parents=[f], help='who A interacts with').add_argument('a')
    sub.add_parser('top-pairs', parents=[f], help='strongest pairs')
    sub.add_parser('hubs', parents=[f], help='agents with the most distinct partners')
    sub.add_parser('agents', parents=[f], help='roster: messages sent, partners, first/last seen').set_defaults(limit=100)
    e = sub.add_parser('examples', parents=[f], help='the messages behind A -> B, newest first')
    e.add_argument('a'); e.add_argument('b'); e.set_defaults(limit=10)
    sub.add_parser('ignored', parents=[f], help='one-sided pairs: A mentions B, B rarely mentions A back')
    r = sub.add_parser('replies', parents=[f], help='when @-mentioned, how often and how fast each agent replies')
    r.add_argument('a', nargs='?', help='break one agent down by who asked')
    r.add_argument('--within', type=int, default=10, help='minutes to count as a reply (default 10)')
    sub.add_parser('goals', parents=[f], help='village goals, newest first').set_defaults(limit=100)
    web = sub.add_parser('web', help='interactive graph UI in the browser')
    web.add_argument('--host', default='127.0.0.1'); web.add_argument('--port', type=int, default=8765)
    return ap


def query(a):
    """Run a parsed query command -> {covers, tables: [(headers, rows)], graph: {nodes, edges, focus}}."""
    if not DB.exists():
        build(7)
    con = sqlite3.connect(DB)
    names = dict(con.execute('SELECT id, name FROM nodes'))
    covers = '%s -> %s' % tuple((t or '-')[:16] for t in con.execute('SELECT min(ts), max(ts) FROM edges').fetchone())
    w, p = where(con, a)
    tables, edges, focus, extra = [], [], [], []

    if a.cmd == 'pair':
        (x, xn), (y, yn) = node(con, a.a), node(con, a.b)
        rows = [[f'{s} -> {d}', *con.execute(f"SELECT sum(kind='addressed'), sum(kind='named'), count(*), "
                                             f"substr(min(ts),1,16), substr(max(ts),1,16) FROM edges "
                                             f"WHERE src=? AND dst=? AND {w}", (si, di, *p)).fetchone()]
                for (si, s), (di, d) in (((x, xn), (y, yn)), ((y, yn), (x, xn)))]
        tables.append((['direction', '@', 'named', 'total', 'first', 'last'], rows))
        tables.append(([a.by, f'{xn} -> {yn}', f'{yn} -> {xn}'], con.execute(
            f"SELECT substr(ts,1,{10 if a.by == 'day' else 7}) b, sum(src=?), sum(src=?) FROM edges "
            f"WHERE ((src=? AND dst=?) OR (src=? AND dst=?)) AND {w} GROUP BY b ORDER BY b",
            (x, y, x, y, y, x, *p)).fetchall()))
        edges, focus = [(xn, yn, rows[0][3]), (yn, xn, rows[1][3])], [xn, yn]

    elif a.cmd == 'neighbors':
        x, xn = node(con, a.a)
        rows = [[names[o], *r] for o, *r in con.execute(
            f"SELECT CASE WHEN src=? THEN dst ELSE src END o, sum(src=?), sum(dst=?), "
            f"sum(kind='addressed'), sum(kind='named'), count(*) t FROM edges "
            f"WHERE (src=? OR dst=?) AND {w} GROUP BY o ORDER BY t DESC LIMIT ?",
            (x, x, x, x, x, *p, a.limit))]
        tables.append((['partner', 'out', 'in', '@', 'named', 'total'], rows))
        edges, focus = [e for r in rows for e in ((xn, r[0], r[1]), (r[0], xn, r[2]))], [xn]

    elif a.cmd == 'top-pairs':
        rows = [[names[i], names[j], *r] for i, j, *r in con.execute(
            f"SELECT min(src,dst) i, max(src,dst) j, sum(src<dst), sum(src>dst), "
            f"sum(kind='addressed'), sum(kind='named'), count(*) t FROM edges "
            f"WHERE {w} GROUP BY i, j ORDER BY t DESC LIMIT ?", (*p, a.limit))]
        tables.append((['a', 'b', 'a->b', 'b->a', '@', 'named', 'total'], rows))
        edges = [e for r in rows for e in ((r[0], r[1], r[2]), (r[1], r[0], r[3]))]

    elif a.cmd == 'hubs':
        d = degrees(con, w, p)
        ids = sorted(d, key=lambda n: (-d[n][0], -d[n][3]))[:a.limit]
        tables.append((['node', 'partners', 'out', 'in', 'total'], [[names[n], *d[n]] for n in ids]))
        edges, extra = among(con, ids, w, p, names), [names[i] for i in ids]

    elif a.cmd == 'agents':
        wm, pm = where(con, argparse.Namespace(**{**vars(a), 'kind': None}))  # messages have no kind
        sent = {s: r for s, *r in con.execute(
            f"SELECT src, count(*), substr(min(ts),1,16), substr(max(ts),1,16) FROM messages WHERE {wm} GROUP BY src",
            pm)}
        d, models = degrees(con, w, p), dict(con.execute('SELECT id, model FROM nodes'))
        ids = sorted(sent.keys() | d.keys(), key=lambda i: -sent.get(i, [0])[0])[:a.limit]
        rows = []
        for i in ids:
            n, first, last = sent.get(i, (0, None, None))
            rows.append([names[i], models[i], n, *d.get(i, (0, 0, 0, 0))[:3], first, last])
        tables.append((['agent', 'model', 'msgs', 'partners', 'out', 'in', 'first', 'last'], rows))
        edges, extra = among(con, ids, w, p, names), [names[i] for i in ids]

    elif a.cmd == 'examples':
        (x, xn), (y, yn) = node(con, a.a), node(con, a.b)
        we, pe = where(con, a, 'e.')
        sql = f"FROM edges e JOIN messages m ON m.id = e.msg_id WHERE e.src=? AND e.dst=? AND {we}"
        tables.append((['time', 'kind', 'room', f'{xn} -> {yn}'], con.execute(
            f"SELECT substr(e.ts,1,16), e.kind, e.room, {SNIPPET} {sql} ORDER BY e.ts DESC LIMIT ?",
            (x, y, *pe, a.limit)).fetchall()))
        edges, focus = [(xn, yn, con.execute(f'SELECT count(*) {sql}', (x, y, *pe)).fetchone()[0])], [xn, yn]

    elif a.cmd == 'ignored':
        rows = [[names[s], names[d], n, back, f'{100 * back // n}%'] for s, d, n, back in con.execute(
            f"WITH d AS (SELECT src, dst, count(*) n FROM edges WHERE {w} GROUP BY src, dst) "
            f"SELECT a.src, a.dst, a.n, coalesce(b.n, 0) FROM d a LEFT JOIN d b ON b.src = a.dst AND b.dst = a.src "
            f"ORDER BY a.n - coalesce(b.n, 0) DESC LIMIT ?", (*p, a.limit))]
        tables.append((['from', 'to', 'sent', 'returned', 'returned %'], rows))
        edges = [e for r in rows for e in ((r[0], r[1], r[2]), (r[1], r[0], r[3]))]

    elif a.cmd == 'replies':
        at = argparse.Namespace(**{**vars(a), 'kind': 'addressed'})  # only @mentions expect an answer
        we, pe = where(con, at, 'e.')
        target = node(con, a.a) if a.a else None
        # ponytail: "replied" = the addressee posted anything in the same room within --within minutes,
        # not necessarily an answer to the asker. Check with `examples` when it matters.
        waits = {}  # agent (or asker, with a target) -> [seconds to reply, or None]
        for s, d, secs in con.execute(
                f"SELECT e.src, e.dst, (julianday((SELECT min(m.ts) FROM messages m WHERE m.src = e.dst "
                f"AND m.room = e.room AND m.ts > e.ts AND m.ts < datetime(e.ts, ?))) - julianday(e.ts)) * 86400 "
                f"FROM edges e WHERE {we}" + (' AND e.dst = ?' if target else ''),
                (f'+{a.within} minutes', *pe, *(target[:1] if target else ()))):
            waits.setdefault(s if target else d, []).append(secs)
        ids = sorted(waits, key=lambda k: -len(waits[k]))[:a.limit]
        rows = []
        for k in ids:
            got = [x for x in waits[k] if x is not None]
            rows.append([names[k], len(waits[k]), len(got), f'{100 * len(got) // len(waits[k])}%',
                         round(statistics.median(got)) if got else None])
        tables.append((['asker' if target else 'agent', '@ asked', 'replied', 'rate', 'median secs'], rows))
        if target:
            edges, focus = [(r[0], target[1], r[1]) for r in rows], [target[1]]
        else:
            edges, extra = among(con, ids, *where(con, at), names), [names[i] for i in ids]

    elif a.cmd == 'goals':
        tables.append((['start', 'end', 'goal', 'edges'], con.execute(
            "SELECT substr(start_time,1,10), substr(end_time,1,10), replace(substr(goal,1,90), char(10), ' '), "
            "(SELECT count(*) FROM edges WHERE ts >= g.start_time AND (g.end_time IS NULL OR ts < g.end_time)) "
            "FROM goals g ORDER BY start_time DESC LIMIT ?", (a.limit,)).fetchall()))

    edges = [e for e in edges if e[2]]
    if a.samples and edges and a.cmd != 'examples':  # newest messages behind the edges drawn
        ids = {n: i for i, n in names.items()}
        we, pe = where(con, a, 'e.')
        tables.append((['time', 'from', 'to', 'sample messages, newest first'], con.execute(
            f"SELECT substr(e.ts,1,16), s.name, group_concat(d.name, ', '), {SNIPPET} FROM edges e "
            f"JOIN messages m ON m.id = e.msg_id JOIN nodes s ON s.id = e.src JOIN nodes d ON d.id = e.dst "
            f"WHERE (e.src, e.dst) IN (VALUES {','.join(['(?,?)'] * len(edges))}) AND {we} "
            f"GROUP BY e.msg_id ORDER BY e.ts DESC LIMIT ?",
            (*(ids[n] for e in edges for n in e[:2]), *pe, a.samples)).fetchall()))
    nodes = list(dict.fromkeys([*focus, *extra, *(n for e in edges for n in e[:2])]))
    return {'covers': covers, 'tables': tables, 'graph': {'nodes': nodes, 'edges': edges, 'focus': focus}}


def api(cmd):
    """Run one CLI command string for the web UI. Usage/lookup errors come back as {'error': ...}."""
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            a = parser().parse_args(shlex.split(cmd))
            if a.cmd == 'web':
                raise SystemExit('the web UI is already running')
            if a.cmd == 'build':
                build(a.days)
                return {'text': out.getvalue()}
            r = query(a)
    except SystemExit as e:
        if e.code == 0:  # -h / --help
            return {'text': out.getvalue()}
        return {'error': e.code if isinstance(e.code, str) else out.getvalue()}
    except Exception as e:
        return {'error': f'{type(e).__name__}: {e}'}
    r['flags'] = ''.join(f' --{k} {shlex.quote(v)}' for k in ('since', 'until', 'room', 'goal', 'kind')
                         if (v := getattr(a, k)))
    return r


def serve(host, port):
    page = (HERE / 'village_web.html').read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            if url.path == '/':
                body, ctype = page, 'text/html; charset=utf-8'
            elif url.path == '/api':
                body = json.dumps(api(parse_qs(url.query).get('cmd', [''])[0])).encode()
                ctype = 'application/json'
            else:
                return self.send_error(404)
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    print(f'village-graph web UI: http://{host}:{port}')
    # ponytail: single-threaded (stdout capture is process-global); a `build` from the UI blocks until done.
    HTTPServer((host, port), Handler).serve_forever()


def main():
    a = parser().parse_args()
    if a.cmd == 'build':
        return build(a.days)
    if a.cmd == 'web':
        return serve(a.host, a.port)
    r = query(a)
    print('# graph covers', r['covers'])
    for i, (headers, rows) in enumerate(r['tables']):
        if i:
            print()
        table(headers, rows)


if __name__ == '__main__':
    main()
