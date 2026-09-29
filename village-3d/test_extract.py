"""Self-check for the pure helpers in extract.py: python3 test_extract.py -> ok"""
from extract import building, clock, label, scrub

assert building({'command': 'ls'}) == 'W'
assert building({'action': 'left_click', 'coordinate': [1, 2]}) == 'T'
assert building({'action': 'send_message_back_to_chat'}) == 'H'
assert building({'action': 'search_history'}) == 'L'
assert building({'action': 'pause'}) == 'C'
assert building(None) is None and building({'text': None}) is None

assert [label(n) for n in ['Claude Opus 4.8', 'GPT-5.6 Sol', 'DeepSeek-V4-Pro', 'Kimi K2.6', 'Gemini 2.5 Pro',
                           'GPT-5', 'Muse Spark 1.3', 'GLM-5.3 Flash']] == \
       ['O4.8', '5.6S', 'V4P', 'K2.6', '2.5P', '5', '1.3', '5.3F']

assert scrub('vnc http://10.108.0.42:6080/vnc.html ok', 99) == 'vnc [REDACTED] ok'
assert scrub('GPT-5.6 v3.2', 99) == 'GPT-5.6 v3.2'

vt = clock({'2026-08-31': 0, '2026-09-01': 1}, 9, 8)
assert vt('2026-08-31 16:00:00.000001') == 0                # 09:00 PT opens day 0
assert vt('2026-09-01 16:05:00') == 8 * 3600 + 300          # day 1 starts right after day 0 closes
assert vt('2026-09-01 00:00:30') is None                    # 17:00 PT, after close
assert vt('2026-09-02 17:00:00') is None                    # a date outside the window
print('ok')
