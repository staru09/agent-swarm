"""Self-check for the pure helpers in extract.py: python3 test_extract.py -> ok"""
from collections import Counter
from extract import building, cc_building, clan_of, label, mentions_of, pt, scrub, slugify, thought, track, untag

mentions = mentions_of({'a': 'GPT-5', 'b': 'GPT-5.1', 'c': 'DeepSeek-V3.2', 'd': 'Claude Opus 4.8'})
assert mentions('@GPT-5.1 and GPT-5: ping DeepSeek‑V3.2, then gpt-5 again', 'd') == ['b', 'a', 'c']  # longest name wins
assert mentions('Claude Opus 4.8 here', 'd') == []                                   # self-mentions dropped
assert mentions('see https://x.io/GPT-5 and v2.GPT-5 and GPT-5x', 'd') == []         # not inside URLs or tokens

assert building({'command': 'ls'}) == 'W'
assert building({'action': 'left_click', 'coordinate': [1, 2]}) == 'T'
assert building({'action': 'send_message_back_to_chat'}) == 'H'
assert building({'action': 'search_history'}) == 'L'
assert building({'action': 'pause'}) == 'C'
assert building(None) is None and building({'text': None}) is None
assert [cc_building(t) for t in ['Bash', 'Read', 'WebFetch', 'mcp__village__edit_memory', 'mcp__village__computer_use']] == \
       ['W', 'W', 'T', 'L', None]

# every agent in the dataset: name -> (model_string, label); labels and slugs must be unique
AGENTS = {
    'Claude 3.5 Sonnet': ('claude-3-5-sonnet-20241022', '3.5S'), 'Claude 3.7 Sonnet': ('claude-3-7-sonnet-20250219', '3.7S'),
    'Claude Opus 4': ('claude-opus-4-20250514', 'O4'), 'Claude Opus 4.1': ('claude-opus-4-1-20250805', 'O4.1'),
    'Claude Opus 4.5': ('claude-opus-4-5-20251101', 'O4.5'), 'Claude Opus 4.6': ('claude-opus-4-6', 'O4.6'),
    'Claude Opus 4.7': ('claude-opus-4-7', 'O4.7'), 'Claude Opus 4.8': ('claude-opus-4-8', 'O4.8'),
    'Claude Opus 5': ('claude-opus-5', 'O5'), 'Claude Sonnet 4.5': ('claude-sonnet-4-5-20250929', 'S4.5'),
    'Claude Sonnet 4.6': ('claude-sonnet-4-6', 'S4.6'), 'Claude Sonnet 5': ('claude-sonnet-5', 'S5'),
    'Claude Haiku 4.5': ('claude-haiku-4-5-20251001', 'H4.5'), 'Claude Fable 5': ('claude-fable-5', 'F5'),
    'Claude Fable 5.1': ('claude-fable-5-1', 'F5.1'),
    'Opus 4.5 (Claude Code)': ('claude-code::claude-opus-4-5-20251101', 'O4.5CC'),
    'GPT-4o': ('gpt-4o-2024-08-06', '4o'), 'GPT-4.1': ('gpt-4.1-2025-04-14', '4.1'), 'GPT-5': ('gpt-5-2025-08-07', '5'),
    'GPT-5.1': ('gpt-5.1-2025-11-13', '5.1'), 'GPT-5.2': ('gpt-5.2-2025-12-11', '5.2'), 'GPT-5.4': ('gpt-5.4-2026-03-05', '5.4'),
    'GPT-5.5': ('gpt-5.5', '5.5'), 'GPT-5.6 Sol': ('gpt-5.6-sol', '5.6S'), 'GPT-5.6 Luna': ('gpt-5.6-luna', '5.6L'),
    'GPT-5.6 Terra': ('gpt-5.6-terra', '5.6T'), 'GPT-6 Astra': ('gpt-6-astra', '6A'),
    'o1': ('o1-2024-12-17', 'o1'), 'o3': ('o3-2025-04-16', 'o3'), 'o4-mini': ('o4-mini-2025-04-16', 'o4M'),
    'Gemini 2.5 Pro': ('gemini-2.5-pro', '2.5P'), 'Gemini 3 Pro': ('gemini-3-pro-preview', '3P'),
    'Gemini 3.1 Pro': ('gemini-3.1-pro-preview', '3.1P'), 'Gemini 3.5 Flash': ('gemini-3.5-flash', '3.5F'),
    'Gemini 3.8 Flash': ('gemini-3.8-flash', '3.8F'), 'DeepSeek-V3.2': ('deepseek-reasoner', 'V3.2'),
    'DeepSeek-V4-Pro': ('deepseek/deepseek-v4-pro', 'V4P'), 'GLM-5.2': ('z-ai/glm-5.2', 'G5.2'),
    'GLM-5.3 Flash': ('z-ai/glm-5.3-flash', 'G5.3F'), 'Kimi K2.6': ('kimi-k2.6', 'K2.6'), 'Kimi K3': ('kimi-k3', 'K3'),
    'Fine-Tuned Leader': ('tinker://363427a9:train:0/sampler_weights/kimi-leader-v7-aug-64', 'FTL'),
    '[Temporary] Fine-tuned Leader': ('tinker://363427a9:train:0/sampler_weights/kimi-leader-v7-aug-64', 'TFTL'),
    'Grok 4': ('grok-4-0709', '4'), 'Grok 4.5': ('grok-4.5', '4.5'), 'Muse Spark 1.3': ('meta/muse-spark-1.3', '1.3'),
}
assert len(AGENTS) == 46
assert {n: label(n) for n in AGENTS} == {n: lab for n, (_, lab) in AGENTS.items()}
assert len({label(n) for n in AGENTS}) == len({slugify(n) for n in AGENTS}) == 46
assert Counter(clan_of(m) for m, _ in AGENTS.values()) == Counter(
    Anthropic=16, OpenAI=14, Google=5, DeepSeek=2, Zhipu=2, Moonshot=4, xAI=2, Meta=1)
assert [clan_of(AGENTS[n][0]) for n in ['Opus 4.5 (Claude Code)', 'o4-mini', 'Fine-Tuned Leader', 'GLM-5.2']] == \
       ['Anthropic', 'OpenAI', 'Moonshot', 'Zhipu']
assert clan_of('mistral-large') is None
assert [slugify(n) for n in ['Claude Opus 4.8', 'GPT-5.6 Sol', '[Temporary] Fine-tuned Leader', 'Opus 4.5 (Claude Code)']] == \
       ['claude-opus-4-8', 'gpt-5-6-sol', 'temporary-fine-tuned-leader', 'opus-4-5-claude-code']

assert scrub('vnc http://10.108.0.42:6080/vnc.html ok', 99) == 'vnc [REDACTED] ok'
assert scrub('GPT-5.6 v3.2', 99) == 'GPT-5.6 v3.2'

assert pt('2026-09-02 16:05:00.000001') == ('2026-09-02', 9 * 3600 + 300)   # PDT, UTC-7
assert pt('2026-01-05 17:00:00') == ('2026-01-05', 9 * 3600)                # PST, UTC-8
assert pt('2026-09-03 06:30:00') == ('2026-09-02', 23 * 3600 + 1800)        # early UTC is the previous PT day

w, t = Counter(W=3, T=1), Counter(T=2)
assert track({2: w, 4: t}, {3, 4, 6}, 8) == '--WHTCHC'  # chat-only slices are Hall, idle is Camp, '-' before arrival
assert track({}, {1}, 3) == '-HC' and track({}, set(), 2) == '--'

assert untag('<narrative_summary>\nDay **1**.\n</narrative_summary>\n\n<top_moments>\n- a <quote>x</quote>\n</top_moments>') == \
       'Day **1**.\n\n- a\n\nx'
assert untag('adds a <script> tag') == 'adds a <script> tag'  # only the summary section tags go

assert thought({'content': [{'type': 'thinking', 'thinking': ' plan A '}, {'type': 'text', 'text': 'hi'}]}) == 'plan A'
assert thought([{'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'r1'}, {'text': 'r2'}]}]) == 'r1\nr2'
assert thought({'candidates': [{'content': {'parts': [{'text': 'g', 'thought': True}, {'text': 'answer'}]}}]}) == 'g'
assert thought({'role': 'assistant', 'reasoning_content': 'k', 'reasoning': 'k'}) == 'k'  # same text once
assert thought({'reasoning': {'effort': 'high'}, 'content': 'no thoughts'}) == ''
assert len(thought({'reasoning': 'x' * 1000})) < 1000
print('ok')
