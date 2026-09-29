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
print('ok')
