from village_graph import extractor

agents = {'a': 'GPT-5', 'b': 'GPT-5.1', 'c': 'Fine-Tuned Leader', 'd': '[Temporary] Fine-tuned Leader',
          'e': 'o3', 'f': 'DeepSeek-V3.2', 'g': 'Claude Opus 4.8'}
m = extractor(agents, humans={'zak', 'claude'})

assert m('I agree with GPT-5.1 here', 'x') == {'b': 'named'}
assert m('GPT-5, thanks.', 'x') == {'a': 'named'}
assert m('GPT-5.6 Sol said so', 'x') == {}                                  # unknown 5.x doesn't fall back to GPT-5
assert m('[Temporary] Fine-tuned Leader wins', 'x') == {'d': 'named'}
assert m('Fine-Tuned Leader wins', 'x') == {'c': 'named'}
assert m('@DeepSeek‑V3.2 ok', 'x') == {'f': 'addressed'}               # non-breaking hyphen
assert m('see https://x.com/o3/page', 'x') == {}
assert m('o3 is right', 'x') == {'e': 'named'}
assert m('Claude Opus 4.8 said, so @Claude Opus 4.8 do it', 'x') == {'g': 'addressed'}
assert m('thanks @zak.', 'x') == {'human': 'addressed'}
assert m('@Claude Opus 4.8 hi', 'x') == {'g': 'addressed'}                  # "@Claude" is not human "claude"
assert m('@GPT-5 hi @zak', 'human') == {'a': 'addressed'}                   # no Human -> Human
assert m('I am GPT-5', 'a') == {}                                           # self-mention dropped

# Query SQL on a tiny hand-made graph: Alpha @Beta twice (Beta answers the first after 60 s, the second
# only after 30 min), Alpha @Gamma once (Gamma only speaks in another room), Beta names Alpha once.
import sqlite3, tempfile
from pathlib import Path
import village_graph as v

v.DB = Path(tempfile.mkdtemp()) / 'test.db'
con = sqlite3.connect(v.DB)
con.executescript(v.SCHEMA)
con.executemany('INSERT INTO nodes VALUES (?,?,?)', [('a', 'Alpha', 'm-a'), ('b', 'Beta', 'm-b'), ('c', 'Gamma', 'm-c')])
msgs = [('1', 'a', 'general', '2026-09-01 10:00:00.000000', '@Beta hi'),
        ('2', 'b', 'general', '2026-09-01 10:01:00.000000', 'Alpha: yes'),
        ('3', 'a', 'general', '2026-09-01 11:00:00.000000', '@Beta again'),
        ('4', 'b', 'general', '2026-09-01 11:30:00.000000', 'late'),
        ('5', 'a', 'general', '2026-09-01 12:00:00.000000', '@Gamma hi'),
        ('6', 'c', 'rest', '2026-09-01 12:00:30.000000', 'elsewhere')]
con.executemany('INSERT INTO messages VALUES (?,?,?,?,?)', msgs)
con.executemany('INSERT INTO edges VALUES (?,?,?,?,?,?)', [(i, s, d, k, 'general', msgs[int(i) - 1][3]) for i, s, d, k in
                [('1', 'a', 'b', 'addressed'), ('2', 'b', 'a', 'named'), ('3', 'a', 'b', 'addressed'), ('5', 'a', 'c', 'addressed')]])
con.commit()
q = lambda *args: v.query(v.parser().parse_args(args))['tables'][0][1]

assert q('replies') == [['Beta', 2, 1, '50%', 60], ['Gamma', 1, 0, '0%', None]]
assert q('replies', 'beta') == [['Alpha', 2, 1, '50%', 60]]
assert q('replies', '--within', '31') == [['Beta', 2, 2, '100%', 930], ['Gamma', 1, 0, '0%', None]]
ignored = q('ignored')
assert sorted(ignored[:2]) == [['Alpha', 'Beta', 2, 1, '50%'], ['Alpha', 'Gamma', 1, 0, '0%']]
assert ignored[2] == ['Beta', 'Alpha', 1, 2, '200%']
assert [r[3] for r in q('examples', 'alpha', 'beta')] == ['@Beta again', '@Beta hi']
assert q('agents')[0] == ['Alpha', 'm-a', 3, 2, 3, 1, '2026-09-01 10:00', '2026-09-01 12:00']
pair = v.query(v.parser().parse_args(['pair', 'alpha', 'beta']))['tables']
assert [r[3] for r in pair[-1][1]] == ['@Beta again', 'Alpha: yes', '@Beta hi']       # samples: both directions, newest first
assert len(v.query(v.parser().parse_args(['pair', 'alpha', 'beta', '--samples', '0']))['tables']) == 2
print('ok')
