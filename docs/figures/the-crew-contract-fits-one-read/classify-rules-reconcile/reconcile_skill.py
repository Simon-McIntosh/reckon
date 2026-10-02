"""Reconcile the skill fragment: append rows for uncovered candidates, list rejects."""
import json, html

W = '/home/ITER/mcintos/Code/.reckon-worktrees/reckon-c8f839407e49/s25-coord/classify-rules-reconcile'
FRAG = W + '/docs/evidence/fragments/the-crew-contract-fits-one-read/classify-rules-in-the-skill.html'
T = '/tmp/reckon-crew-scratch/r-20261002T164535169273-classify-rules-reconcile/recon'
uncov = json.load(open(T + '/skill-uncovered.json'))['uncov']

REJECT = {
    0: 'vocabulary list, not a rule sentence',
    9: "explains why the rule above holds; the rule itself is the sentence before it",
    24: 'diagnostic observation about workers, not an instruction to a reader',
    26: 'introduces a table and states no rule',
    27: 'table row, not a rule sentence',
    39: 'table row, not a rule sentence',
    40: 'states an invariant of the handler code, not a rule a coordinator or worker follows',
    69: "API signature documentation for an edit_plan operation",
    70: 'table row, not a rule sentence',
    76: 'enumeration of refusal categories, not a rule sentence',
    80: 'fragment of a code block left by the sentence splitter, not prose',
    82: 'fragment of a keyword array, not a sentence',
    83: 'table row, not a rule sentence',
}

rows_html = []
num = 247
for i, (line, text) in enumerate(uncov):
    if i in REJECT:
        continue
    num += 1
    esc = html.escape_ if False else html.escape
    body = esc(text, quote=False).replace('`', '`')
    rows_html.append(
        f'      <tr>\n'
        f'        <td>{num}</td>\n'
        f'        <td>{body}<br><span class="loc">skills/reckon-build/SKILL.md:{line}</span></td>\n'
        f'        <td>—</td>\n'
        f'        <td class="t">—</td>\n'
        f'        <td>yes</td>\n'
        f'        <td>keep</td>\n'
        f'        <td>prose rule re-read at the current head; no code refusal verified at this node</td>\n'
        f'      </tr>')

rejects_html = []
for i, (line, text) in enumerate(uncov):
    if i not in REJECT:
        continue
    snippet = text if len(text) <= 160 else text[:157] + '…'
    rejects_html.append(
        f'      <li><code>skills/reckon-build/SKILL.md:{line}</code> — '
        f'“{html.escape(snippet, quote=False)}” <em>rejected: {REJECT[i]}.</em></li>')

s = open(FRAG).read()
assert '</tbody>' in s and '</section>' in s
if 'Scanner candidates rejected' in s: REJECTS_PRESENT=True
s = s.replace('    </tbody>', '\n'.join(rows_html) + '\n    </tbody>', 1)
block = (
    '\n  <h3 id="rejected">Scanner candidates rejected as non-rules</h3>\n'
    '  <p>Every candidate the counter reports is either a row above or one of the '
    'sentences below, rejected for the reason stated; rows plus rejected equal the '
    'candidate count.</p>\n  <ul>\n' + '\n'.join(rejects_html) + '\n  </ul>\n')
if not REJECTS_PRESENT:
    s = s.replace('</section>', block + '</section>', 1)
open(FRAG, 'w').write(s)
print('rows appended:', num - 247, 'rejects listed:', len(rejects_html), 'total rows now:', num)