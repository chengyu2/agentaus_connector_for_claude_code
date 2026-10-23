"""Grade a 1913 bank.py fix: compare against the original module's behaviour. Usage: grade_1913.py <workdir> <original_bank.py> [--hard]"""
import contextlib, importlib.util, io, json, os, subprocess, sys
# Absolute: the original is staged under <wd>/.orig and reads ../strength_reports through a
# symlink; a relative link target resolves against .orig/, not the cwd. Measured: a relative
# wd scored 0.385 where the absolute one scored 0.538.
wd, orig = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
HARD = '--hard' in sys.argv[3:]
def load(path, name):
    """Import as another tool would: neutral argv, stdout captured, cwd always restored.
    Measured (d13 c1913h): the candidate called parser.parse_args() at module level, read the
    grader's own argv, and SystemExit (not an Exception) took the whole grade to 0."""
    spec = importlib.util.spec_from_file_location(name, path); m = importlib.util.module_from_spec(spec)
    cwd, argv = os.getcwd(), sys.argv
    sys.argv = [path]
    try:
        os.chdir(os.path.dirname(path))
        with contextlib.redirect_stdout(io.StringIO()):
            spec.loader.exec_module(m)
    finally:
        os.chdir(cwd); sys.argv = argv
    return m
# The original reads ../strength_reports relative to its own file, so stage it beside the candidate.
os.makedirs(os.path.join(wd, '.orig', 'tools'), exist_ok=True)
open(os.path.join(wd, '.orig', 'tools', 'bank.py'), 'w').write(open(orig).read())
if not os.path.lexists(os.path.join(wd, '.orig', 'strength_reports')):
    os.symlink(os.path.join(wd, 'strength_reports'), os.path.join(wd, '.orig', 'strength_reports'))
out = {}
try:
    o = load(os.path.join(wd, '.orig', 'tools', 'bank.py'), 'bank_orig')
    # Every check compares against the original; if it staged nothing, `all(... for n in o.BANK)`
    # passes vacuously. Fail loudly instead: a grader error, not a score.
    assert len(o.BANK) == 12, f'original failed to stage: {len(o.BANK)} profiles, want 12'
except BaseException as e:
    print(json.dumps({'score': None, 'grader_error': 'original: ' + repr(e)[:300]})); sys.exit()
try:
    c = load(os.path.join(wd, 'tools', 'bank.py'), 'bank_cand')
except BaseException as e:
    print(json.dumps({'score': 0.0, 'error': repr(e)[:300]})); sys.exit()
def same(f):
    try: return bool(f())
    except BaseException: return False
checks = {
 'prefers_fuller_report': lambda: {n: p['depth'] for n, p in c.BANK.items()} == {n: p['depth'] for n, p in o.BANK.items()} and set(c.FULL) == set(o.FULL),
 'au_spelling': lambda: all(c.BANK[n]['seq'] == o.BANK[n]['seq'] for n in o.BANK) and c.SPELLING.get('Maximiser') == 'Maximizer',
 'lead_domain_tiebreak': lambda: all(c.lead_domain(n) == o.lead_domain(n) for n in o.FULL),
 'short_names': lambda: all(c.short(n) == o.short(n) for n in o.BANK),
 'count_top_inclusive': lambda: all(c.count_top(t, k) == o.count_top(t, k) for t in o.THEMES for k in (1, 5, 10)),
 'no_regression': lambda: all(c.mean_rank(t) == o.mean_rank(t) for t in o.THEMES) and c.domain_slots() == o.domain_slots() and c.THEMES == o.THEMES and c.DOMAIN == o.DOMAIN,
}
if HARD:
    # c1913h's four further bugs, each its own check rather than one "no regression".
    checks.update({
        'names_cleaned': lambda: set(c.BANK) == set(o.BANK),
        'partial_profiles_kept': lambda: set(c.PART) == set(o.PART) and len(c.BANK) == len(o.BANK),
        'domain_membership': lambda: c.DOMAIN == o.DOMAIN,
        'mean_rank_is_a_mean': lambda: all(c.mean_rank(t) == o.mean_rank(t) for t in o.THEMES),
    })
for k, f in checks.items(): out[k] = same(f)
# Feature: --json prints one object per profile.
try:
    r = subprocess.run([sys.executable, 'tools/bank.py', '--json'], cwd=wd, capture_output=True, text=True, timeout=120)
    data = json.loads(r.stdout)
    want = {n: {'name': n, 'kind': p['kind'], 'depth': p['depth'], 'top5': o.top(n, 5),
                'lead_domain': o.lead_domain(n) if p['depth'] == 34 else None} for n, p in o.BANK.items()}
    got = {d['name']: d for d in data} if isinstance(data, list) else {}
    out['json_feature'] = set(got) == set(want) and all(all(got[n].get(k) == v for k, v in want[n].items()) for n in want)
except Exception as e:
    out['json_feature'] = False; out['json_error'] = repr(e)[:200]
try:  # the human summary must still work
    r = subprocess.run([sys.executable, 'tools/bank.py'], cwd=wd, capture_output=True, text=True, timeout=120)
    out['summary_still_runs'] = r.returncode == 0 and 'bank size: 12' in r.stdout and 'full 34: 11' in r.stdout
except Exception: out['summary_still_runs'] = False
keys = [k for k in out if not k.endswith('_error')]
out['score'] = round(sum(bool(out[k]) for k in keys) / len(keys), 3)
# Diagnostic, outside the score so the denominator (8, or 12 with --hard) and every historical
# score stay comparable: seven tools in tools/ import bank, so importing it under another
# tool's argv must neither exit nor print.
def _import_safe():
    spec = importlib.util.spec_from_file_location('bank_probe', os.path.join(wd, 'tools', 'bank.py'))
    m = importlib.util.module_from_spec(spec); cwd, argv = os.getcwd(), sys.argv
    sys.argv = ['viz.py', '--some-other-tool-flag']
    try:
        os.chdir(os.path.join(wd, 'tools'))
        with contextlib.redirect_stdout(io.StringIO()) as buf, contextlib.redirect_stderr(io.StringIO()):
            spec.loader.exec_module(m)
        return buf.getvalue() == ''
    except BaseException:
        return False
    finally:
        os.chdir(cwd); sys.argv = argv
out['diagnostics'] = {'import_safe': _import_safe()}
print(json.dumps(out))
