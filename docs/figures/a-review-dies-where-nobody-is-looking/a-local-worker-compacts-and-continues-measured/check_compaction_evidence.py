"""Check the evidence fragment's facts against the run's own logs.

The fragment states six compaction boundaries and nine readings by number.
Nothing in the repository reddens if one of those numbers drifts, so this
script re-derives every one of them from the run directory and fails when the
fragment and the logs disagree.

Run it with the run directory as the only argument, or with RECKON_RUN_DIR in
the environment. Exit 0 when every fact matches, 1 otherwise.
"""

import json
import os
import re
import sys

FRAGMENT = (os.path.dirname(__file__).rsplit('/figures/', 1)[0]
            + '/evidence/fragments/a-review-dies-where-nobody-is-looking/'
            'a-local-worker-compacts-and-continues-measured.html')
FRAGMENT = os.environ.get('EVIDENCE_FRAGMENT', FRAGMENT)

BOUNDARY_ROW = re.compile(
    r'<tr><td>(\d)</td><td>([\d ]+)</td><td>(\d+)</td><td>([\d ]+)</td>'
    r'<td>([\d ]+)</td><td>([\d ]+)</td><td>([\d.]+)</td></tr>')

READING_ROW = re.compile(
    r'<tr><td>part-(\d\d)</td><td><code>([0-9A-F]{16})</code></td>'
    r'<td>[\d:]+Z</td><td>([\d ]+)</td>')

def num(text):
    return int(text.replace(' ', ''))


def boundaries_in_stream(path):
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get('type') == 'system' and rec.get('subtype') == 'compact_boundary':
                metas = rec['compact_metadata']
                out.append(metas)
    return out


def stream_boundary_indices(path):
    idx = []
    with open(path) as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get('type') == 'system' and rec.get('subtype') == 'compact_boundary':
                idx.append(i)
    return idx


def fail(problems, message):
    problems.append(message)


def main(argv):
    run = argv[1] if len(argv) > 1 else os.environ.get('RECKON_RUN_DIR', '')
    if not run:
        print('usage: check_compaction_evidence.py <run-dir>')
        return 1
    html = open(FRAGMENT).read()
    problems = []

    rows = BOUNDARY_ROW.findall(html)
    metas = boundaries_in_stream(run + '/stream.jsonl')
    idxs = stream_boundary_indices(run + '/stream.jsonl')
    if len(rows) != len(metas):
        fail(problems, 'fragment lists %d boundaries, stream has %d'
             % (len(rows), len(metas)))
    for row, meta, idx in zip(rows, metas, idxs):
        n, rec, _turn, pre, post, dropped, dur = row
        if num(rec) != idx:
            fail(problems, 'boundary %s: fragment record index %s, stream %d'
                 % (n, num(rec), idx))
        for name, got, want in (('pre', num(pre), meta['pre_tokens']),
                                ('post', num(post), meta['post_tokens']),
                                ('dropped', num(dropped),
                                 meta['cumulative_dropped_tokens'])):
            if got != want:
                fail(problems, 'boundary %s: %s %d, stream %d'
                     % (n, name, got, want))
        if abs(float(dur) * 1000 - meta['duration_ms']) > 60:
            fail(problems, 'boundary %s: duration %ss, stream %d ms'
                 % (n, dur, meta['duration_ms']))

    expected = json.load(open(run + '/measure/corpus/expected-checksums.json'))
    readings = [l.split() for l in
                open(run + '/measure/probe/results/readings.txt') if l.strip()]
    logged = {p[0]: p[1] for p in readings if p[0].startswith('part-')}
    for nn, checksum, _pos in READING_ROW.findall(html):
        key = 'part-' + nn
        if expected.get(key) != checksum:
            fail(problems, '%s: fragment %s, corpus %s'
                 % (key, checksum, expected.get(key)))
        if logged.get(key) != checksum:
            fail(problems, '%s: fragment %s, readings %s'
                 % (key, checksum, logged.get(key)))

    window = 230144
    unused = [window - m['pre_tokens'] for m in metas]
    lo, hi = min(unused), max(unused)
    for value in (lo, hi):
        want = format(value, ',').replace(',', ' ')
        if want not in html:
            fail(problems, 'unused-window figure %s is not stated' % want)
    flat = ' '.join(html.split())
    if 'the last record in those streams being a system record' in flat:
        fail(problems, 'the fragment repeats the single-signature claim '
                       'the census contradicts')
    census = json.load(open(run + '/measure/compaction-census.json'))
    seen = {}
    for row in census['rows']:
        if row['backend'] != 'clive':
            continue
        for stream in row['streams']:
            if stream['announcements'] and not stream['boundaries']:
                seen[row['run_id']] = stream['last_type']
    if len(seen) != 2:
        fail(problems, 'census holds %d announced-no-boundary streams, not 2'
             % len(seen))
    for run_id, last_type in seen.items():
        if run_id[:44] not in flat:
            fail(problems, 'the fragment does not name %s' % run_id[:44])
        if last_type == 'result' and 'Prompt is too long' not in flat:
            fail(problems, 'the result-terminal stream is not described as such')
    for p in problems:
        print('FAIL ' + p)
    if problems:
        return 1
    print('OK %d boundaries, %d readings, unused-window margins %d..%d tokens'
          % (len(rows), len(logged), lo, hi))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))