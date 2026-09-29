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
  village-graph web [--port 8765]     # same commands, drawn as an interactive graph in the browser
Filters on every query: --since/--until (UTC, until exclusive) --room --kind --goal --limit
Data: $VILLAGE_DATA (dir with the .jsonl.gz files), else the HF cache ($HF_HUB_CACHE or ~/.cache/huggingface/hub).
"""
import argparse, contextlib, gzip, io, json, os, re, shlex, sqlite3, sys
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


def build(days):
    snap = snapshot()
    agents = {r['id']: r['name'] for r in rows(snap, 'agents.jsonl.gz')}
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

    edges = []
    for m in msgs:
        if m['speaker_type'] == 'agent':
            src = m['agent_speaker_id']
        elif speaker.get(m['id']) == 'automated':
            continue
        else:
            src = 'human'
        for dst, kind in mentions(m['content'] or '', src).items():
            edges.append((m['id'], src, dst, kind, rooms.get(m['room_id']), m['created_at']))

    tmp = DB.with_suffix('.tmp')
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript('''
        CREATE TABLE nodes(id TEXT PRIMARY KEY, name TEXT UNIQUE);
        CREATE TABLE edges(msg_id TEXT, src TEXT, dst TEXT, kind TEXT, room TEXT, ts TEXT, PRIMARY KEY(msg_id, dst));
        CREATE TABLE goals(goal TEXT, start_time TEXT, end_time TEXT);''')
    con.executemany('INSERT INTO nodes VALUES (?,?)', [*agents.items(), ('human', 'Human')])
    con.executemany('INSERT INTO edges VALUES (?,?,?,?,?,?)', edges)
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


def where(con, a):
    sql, p = ['1=1'], []
    if a.since: sql.append('ts >= ?'); p.append(a.since)
    if a.until: sql.append('ts < ?'); p.append(a.until)
    if a.room: sql.append('room = ?'); p.append(a.room)
    if a.kind: sql.append('kind = ?'); p.append(a.kind)
    if a.goal:
        g = con.execute('SELECT goal, start_time, end_time FROM goals WHERE goal LIKE ? ORDER BY start_time',
                        (f'%{a.goal}%',)).fetchall()
        if len(g) != 1:
            sys.exit(f'--goal {a.goal!r} matched {len(g)} goals:\n' +
                     '\n'.join(f'  {s[:10]}  {t[:80]!r}' for t, s, _ in g))
        sql.append('ts >= ? AND (? IS NULL OR ts < ?)'); p += [g[0][1], g[0][2], g[0][2]]
    return ' AND '.join(sql), p


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
    sub.add_parser('build').add_argument('--days', type=int, default=7, help='last N days of data; 0 = all')
    f = argparse.ArgumentParser(add_help=False)
    f.add_argument('--since'); f.add_argument('--until'); f.add_argument('--room'); f.add_argument('--goal')
    f.add_argument('--kind', choices=['addressed', 'named'])
    f.add_argument('--limit', type=int, default=20)
    p = sub.add_parser('pair', parents=[f]); p.add_argument('a'); p.add_argument('b')
    p.add_argument('--by', choices=['day', 'month'], default='day')
    sub.add_parser('neighbors', parents=[f]).add_argument('a')
    sub.add_parser('top-pairs', parents=[f])
    sub.add_parser('hubs', parents=[f])
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
        raw = con.execute(
            f"SELECT n, count(DISTINCT o), sum(out_), sum(1-out_), count(*) t FROM ("
            f"  SELECT src n, dst o, 1 out_ FROM edges WHERE {w} UNION ALL"
            f"  SELECT dst, src, 0 FROM edges WHERE {w}) "
            f"GROUP BY n ORDER BY count(DISTINCT o) DESC, t DESC LIMIT ?", (*p, *p, a.limit)).fetchall()
        tables.append((['node', 'partners', 'out', 'in', 'total'], [[names[n], *r] for n, *r in raw]))
        ids, qs = [r[0] for r in raw], ','.join('?' * len(raw))
        edges = [(names[s], names[d], c) for s, d, c in con.execute(  # edges among the hubs shown
            f"SELECT src, dst, count(*) FROM edges WHERE src IN ({qs}) AND dst IN ({qs}) AND {w} GROUP BY src, dst",
            (*ids, *ids, *p))]
        extra = [names[i] for i in ids]

    edges = [e for e in edges if e[2]]
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
