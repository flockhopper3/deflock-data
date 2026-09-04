# EyesOnFlock Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Port the sharing-network pipeline from the research repo into a self-contained `eyesonflock/` hub and run it on GitHub Actions, producing verified artifacts (no Cloudflare upload yet).

**Architecture:** Behaviour-preserving port of seven stdlib-only Python scripts, re-rooted through a new `paths.py`, plus three additions: an absolute portal-count floor in step 00, a cache-only mode for the Google geocoder, and a fail-closed `06_verify_outputs.py` that also emits `meta.json`. One workflow runs tests → pipeline → artifacts.

**Tech Stack:** Python 3.10+ (stdlib only at runtime), pytest for tests, GitHub Actions (`setup-python`, `actions/cache`, `upload-artifact`).

**Spec:** `docs/superpowers/specs/2026-09-04-eyesonflock-pipeline-design.md`

## Global Constraints

- Python floor: 3.10 (PEP 604 unions). CI uses 3.12.
- Runtime dependencies: none beyond the standard library. `pytest` is dev-only.
- Output schema is frozen: the 18 property keys listed in the spec, file names `sharing-network-nodes.geojson` / `sharing-network-adjacency.json`.
- Generated data (`work/`, raw snapshots, outputs) is never committed. Gazetteers and the Google cache seed are.
- No `git push`, no Cloudflare action of any kind. Commits go on branch `eyesonflock-pipeline`.
- Source of truth being ported: `FLOCKHOPPER DATA RESEARCH/sharing-network/scripts/` and `…/tests/test_{fetch_eyesonflock,geocode,nodes_geojson,parse_orgs,state_bbox}.py` (115 tests, all passing at port time).

---

### Task 1: Scaffold the hub, add CLAUDE.md, port files verbatim

**Files:**
- Create: `CLAUDE.md`
- Create: `eyesonflock/.gitignore`, `eyesonflock/requirements-dev.txt`, `eyesonflock/.env.example`
- Copy: research `sharing-network/scripts/*.py` → `eyesonflock/scripts/`
- Copy: research `tests/test_{fetch_eyesonflock,geocode,nodes_geojson,parse_orgs,state_bbox}.py` → `eyesonflock/tests/`
- Copy: research `data/raw/2023_Gaz_*.txt` → `eyesonflock/gazetteer/`
- Copy: research `data/intermediate/google_geocode_cache.json` → `eyesonflock/google_geocode_cache.json`
- Copy: research `sharing-network/METHODOLOGY.md` → `eyesonflock/METHODOLOGY.md`

**Interfaces:**
- Produces: the five test modules resolve scripts via `Path(__file__).resolve().parent.parent / "scripts"`.

- [x] **Step 1: Write CLAUDE.md**

```markdown
# deflock-data — working rules

## Hard rule: no pushes, no Cloudflare, without permission
Never run `git push`, and never deploy to or upload to Cloudflare (Workers,
R2, Pages, `wrangler`, `aws s3 … --endpoint-url …r2…`) unless the owner has
explicitly said to in the current conversation. Local commits on a branch are
fine. Asking once and being told yes covers that one action, not the session.

## Layout
- `data/`  hourly camera ingestion (Overpass → R2)
- `tiles/` PMTiles builds (cameras, boundaries)
- `eyesonflock/` sharing-network pipeline (EyesOnFlock → GeoJSON + adjacency)
Each hub is independent; don't share code across them.
```

- [x] **Step 2: Copy files, fix the test `sys.path` line**

```bash
R="/Users/jackcauthen/Documents/Developer/FLOCK/FLOCKHOPPER DATA RESEARCH"
mkdir -p eyesonflock/{scripts,tests,gazetteer}
cp "$R"/sharing-network/scripts/*.py eyesonflock/scripts/
cp "$R"/tests/test_{fetch_eyesonflock,geocode,nodes_geojson,parse_orgs,state_bbox}.py eyesonflock/tests/
cp "$R"/data/raw/2023_Gaz_*.txt eyesonflock/gazetteer/
cp "$R"/data/intermediate/google_geocode_cache.json eyesonflock/
cp "$R"/sharing-network/METHODOLOGY.md eyesonflock/
sed -i '' 's#parent.parent / "sharing-network" / "scripts"#parent.parent / "scripts"#' eyesonflock/tests/test_*.py
printf 'work/\n.env\n' > eyesonflock/.gitignore
printf 'pytest>=8\n' > eyesonflock/requirements-dev.txt
```

- [x] **Step 3: Run the ported tests**

Run: `venv/bin/python -m pytest eyesonflock/tests -q`
Expected: `115 passed`

- [x] **Step 4: Commit**

```bash
git add CLAUDE.md eyesonflock
git commit -m "Port the EyesOnFlock sharing-network pipeline into eyesonflock/"
```

---

### Task 2: `paths.py` and re-root every script

**Files:**
- Create: `eyesonflock/scripts/paths.py`
- Test: `eyesonflock/tests/test_paths.py`
- Modify: every `eyesonflock/scripts/0*.py` — replace the `BASE_DIR`/`RAW_DIR`/`INT_DIR`/`OUTPUT_DIR`/gazetteer/`ENV_FILE`/`CACHE_FILE` constants with imports from `paths`; move import-time `mkdir` into `main()`.

**Interfaces:**
- Produces (`paths.py`):
  ```python
  EOF_ROOT: Path            # eyesonflock/
  GAZETTEER_VINTAGE: str    # "2023"
  GAZETTEER_DIR: Path       # EOF_ROOT / "gazetteer"
  PLACE_GAZ, COUNTY_GAZ, STATE_GAZ: Path
  GOOGLE_CACHE_FILE: Path   # EOF_ROOT / "google_geocode_cache.json"
  ENV_FILE: Path            # EOF_ROOT / ".env"
  WORK_DIR: Path            # $EYESONFLOCK_WORK_DIR or EOF_ROOT / "work"
  RAW_DIR, INT_DIR, OUTPUT_DIR: Path
  SNAPSHOT_FILE, PARSED_ORGS_FILE, GEOCODED_ORGS_FILE, AUDIT_FILE: Path
  NODES_FILE, ADJACENCY_FILE, META_FILE: Path
  ```

- [x] **Step 1: Write the failing test**

```python
# eyesonflock/tests/test_paths.py
import importlib, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

def _reload(monkeypatch, env):
    if env is None:
        monkeypatch.delenv("EYESONFLOCK_WORK_DIR", raising=False)
    else:
        monkeypatch.setenv("EYESONFLOCK_WORK_DIR", env)
    import paths
    return importlib.reload(paths)

def test_default_work_dir_is_inside_hub(monkeypatch):
    p = _reload(monkeypatch, None)
    assert p.WORK_DIR == p.EOF_ROOT / "work"
    assert p.RAW_DIR == p.WORK_DIR / "raw"

def test_env_override_relocates_generated_paths_only(monkeypatch, tmp_path):
    p = _reload(monkeypatch, str(tmp_path))
    assert p.OUTPUT_DIR == tmp_path / "output"
    assert p.NODES_FILE == tmp_path / "output" / "sharing-network-nodes.geojson"
    assert p.GAZETTEER_DIR == p.EOF_ROOT / "gazetteer"          # committed inputs don't move
    assert p.GOOGLE_CACHE_FILE == p.EOF_ROOT / "google_geocode_cache.json"

def test_gazetteer_files_exist():
    import paths
    for f in (paths.PLACE_GAZ, paths.COUNTY_GAZ, paths.STATE_GAZ):
        assert f.is_file(), f
```

- [x] **Step 2: Run to verify it fails** — `pytest eyesonflock/tests/test_paths.py -q` → `ModuleNotFoundError: paths`
- [x] **Step 3: Write `paths.py`** (the interface above, ~40 lines, `WORK_DIR = Path(os.environ.get("EYESONFLOCK_WORK_DIR") or EOF_ROOT / "work").resolve()`).
- [x] **Step 4: Re-root each script.** For each `0*.py`: delete its `BASE_DIR = Path(__file__)…` block and file constants; `from paths import …`; call `X.parent.mkdir(parents=True, exist_ok=True)` inside `main()` before writing. Keep module-level names that tests monkeypatch (`GEOCODED_ORGS`, `EYESONFLOCK`, `OUTPUT_FILE` in 03; `OUTPUT_FILE` in 00).
- [x] **Step 5: Run all tests** → `118 passed`.
- [x] **Step 6: Offline smoke run** against a copy of the April snapshot:

```bash
export EYESONFLOCK_WORK_DIR=/tmp/eof-smoke; mkdir -p $EYESONFLOCK_WORK_DIR/raw
cp "$R/data/raw/eyesonflock_full_data.json" $EYESONFLOCK_WORK_DIR/raw/
python eyesonflock/scripts/run_pipeline.py --skip-fetch   # --skip-fetch lands in Task 5; until then run 01..05 directly
```
Expected: outputs under `/tmp/eof-smoke/output/`, feature count 6461 (identical to the reference run — same snapshot, same code).

- [x] **Step 7: Commit** — `git commit -m "eyesonflock: route all paths through paths.py"`

---

### Task 3: Step 00 absolute portal floor

**Files:**
- Modify: `eyesonflock/scripts/00_fetch_eyesonflock.py` (`check_sanity`)
- Test: `eyesonflock/tests/test_fetch_eyesonflock.py` (add to `TestCheckSanity`)

- [x] **Step 1: Failing tests**

```python
def test_absolute_floor_without_prior(self):
    tiny = _snapshot([_portal(f"p{i}") for i in range(_fetch.MIN_PORTAL_COUNT_ABS - 1)])
    with pytest.raises(ValueError, match="absolute floor"):
        _fetch.check_sanity(None, tiny)

def test_absolute_floor_met_without_prior(self):
    ok = _snapshot([_portal(f"p{i}") for i in range(_fetch.MIN_PORTAL_COUNT_ABS)])
    _fetch.check_sanity(None, ok)
```
Existing `test_no_prior_passes` uses a 1-portal snapshot; change it to build `MIN_PORTAL_COUNT_ABS` portals.

- [x] **Step 2: Run** → FAIL (`AttributeError: MIN_PORTAL_COUNT_ABS`)
- [x] **Step 3: Implement** — `MIN_PORTAL_COUNT_ABS = 500`; in `check_sanity`, before the prior logic: `if new_n < MIN_PORTAL_COUNT_ABS: raise ValueError(f"new snapshot has only {new_n} portals, below the absolute floor of {MIN_PORTAL_COUNT_ABS}. Refusing to overwrite.")`
- [x] **Step 4: Run** → PASS. **Step 5: Commit** — `"eyesonflock: absolute portal-count floor in step 00"`

---

### Task 4: Google geocoder cache-only mode

**Files:**
- Modify: `eyesonflock/scripts/google_geocode.py` (`GoogleGeocoder.__init__`, `_call_api`)
- Modify: `eyesonflock/scripts/02a_google_geocode.py` (`main`)
- Test: `eyesonflock/tests/test_google_geocode.py` (new)

**Interfaces:**
- `GoogleGeocoder(api_key: str | None, cache_path: Path | None)`; with `api_key=None`, `geocode_org` returns cached hits and `None` on misses, never touching the network; `api_calls_made == 0`.

- [x] **Step 1: Failing tests**

```python
class TestCacheOnlyMode:
    def test_cache_hit_without_key(self, tmp_path):
        cache = tmp_path / "c.json"
        cache.write_text(json.dumps({"Allen Park, MI": {"lat": 42.25, "lng": -83.21}}))
        g = GoogleGeocoder(api_key=None, cache_path=cache)
        assert g.geocode_org({"type": "pd", "city": "Allen Park", "state": "MI"}) == (42.25, -83.21)
        assert g.api_calls_made == 0

    def test_cache_miss_without_key_does_not_call_network(self, tmp_path, monkeypatch):
        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("network call"))
        g = GoogleGeocoder(api_key=None, cache_path=tmp_path / "c.json")
        assert g.geocode_org({"type": "pd", "city": "Nowhere", "state": "TX"}) is None
        assert g.api_calls_made == 0

    def test_miss_without_key_is_not_cached_as_negative(self, tmp_path):
        g = GoogleGeocoder(api_key=None, cache_path=tmp_path / "c.json")
        g.geocode_org({"type": "pd", "city": "Nowhere", "state": "TX"})
        assert g.cache_size == 0   # a keyed run later must still be able to try
```

- [x] **Step 2: Run** → FAIL. **Step 3: Implement** — in `geocode_org`, on a miss: `if not self.api_key: return None` before `_call_api`; keep negative caching only for real API misses. **Step 4:** PASS.
- [x] **Step 5: Update 02a `main()`** — always construct the client; if `api_key` is None print `WARNING: GOOGLEMAPSAPI not set — cache-only mode; uncached candidates stay at state centroids.` and continue. Summary line adds `Mode: cache-only|live`.
- [x] **Step 6: Run all tests** → PASS. **Step 7: Commit** — `"eyesonflock: apply Google geocode cache even without an API key"`

---

### Task 5: `06_verify_outputs.py`, `meta.json`, orchestrator wiring

**Files:**
- Create: `eyesonflock/scripts/06_verify_outputs.py`
- Test: `eyesonflock/tests/test_verify_outputs.py`
- Modify: `eyesonflock/scripts/run_pipeline.py` (add step 06; `--skip-fetch`)

**Interfaces:**
- `verify(nodes: dict, adjacency: dict, snapshot: dict) -> list[str]` returns violation messages (empty = pass). Pure, testable.
- `build_meta(nodes, adjacency, snapshot, run_id: str | None) -> dict` returns the spec's meta object.
- `main() -> int` loads the three files from `paths`, prints violations, writes `META_FILE` on success, returns 0/1.
- Thresholds as module constants: `MIN_FEATURES = 5000`, `MIN_PORTALS = 700`, `MIN_DIRECTED_EDGES = 100_000`, `MAX_DEFAULT_SHARE = 0.02`, `EXPECTED_PROPERTY_KEYS = frozenset({...18 keys...})`.

- [x] **Step 1: Failing tests** — a `_good()` fixture builder that generates `MIN_FEATURES` synthetic nodes (`MIN_PORTALS` of them portals) and an adjacency with ≥ `MIN_DIRECTED_EDGES` edges where `connectionCount` is derived from it; then one test per invariant that breaks the fixture and asserts the matching substring appears in `verify(...)`:
  `"FeatureCollection"`, `"fewer than"`, `"coordinates"`, `"property keys"`, `"duplicate id"`, `"portalSlug"`, `"portals"`, `"unknown node"`, `"sorted"`, `"self-edge"`, `"directed edges"`, `"connectionCount"`, `"default"`. Plus `test_good_fixture_has_no_violations` and `test_main_writes_meta(tmp_path, monkeypatch)` asserting `meta["featureCount"] == MIN_FEATURES`.
- [x] **Step 2: Run** → FAIL (module missing). **Step 3: Implement** the module. **Step 4:** PASS.
- [x] **Step 5: Orchestrator** — `SCRIPTS` gains `"06_verify_outputs.py"`; `argparse` with `--skip-fetch` (drops `00_fetch_eyesonflock.py`); step 06 failure exits 1 like any other. Final print lists `NODES_FILE`, `ADJACENCY_FILE`, `META_FILE`.
- [x] **Step 6: Offline run** with `--skip-fetch` on the April snapshot → `PIPELINE COMPLETE`, `meta.json` present, `featureCount == 6461`.
- [x] **Step 7: Commit** — `"eyesonflock: fail-closed output verification + meta.json"`

---

### Task 6: Workflow, README, env example

**Files:**
- Create: `.github/workflows/eyesonflock-pipeline.yml` (exactly the steps in the spec's Workflow section)
- Create: `eyesonflock/README.md`, `eyesonflock/.env.example`
- Modify: `README.md` (repo layout table + hub row), `eyesonflock/METHODOLOGY.md` (paths, step 06, cache-only note)

- [x] **Step 1: Write the workflow.** Job summary step:
  ```bash
  { echo "## EyesOnFlock sharing network"; echo; echo '```json'; cat "$W/output/meta.json"; echo '```';
    echo; echo "<details><summary>Geocode audit</summary>"; echo; echo '```'; cat "$W/intermediate/geocode_audit.txt"; echo '```'; echo "</details>"; } >> "$GITHUB_STEP_SUMMARY"
  ```
- [x] **Step 2: Validate** — `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/eyesonflock-pipeline.yml'))"` (install `pyyaml` in the scratch venv) and eyeball against `fetch-data.yml` conventions (concurrency, permissions, timeout).
- [x] **Step 3: README** — sections: what it produces, run locally, the workflow, artifacts, secrets (optional `GOOGLEMAPSAPI`), gazetteer refresh, phase 2/3 plan.
- [x] **Step 4: Commit** — `"eyesonflock: GitHub Actions workflow + docs"`

---

### Task 7: End-to-end live run and reference comparison

- [x] **Step 1:** `EYESONFLOCK_WORK_DIR=/tmp/eof-live python eyesonflock/scripts/run_pipeline.py` (one live fetch; local `.env` may supply the Google key — copy from the research repo if the owner's key is there, otherwise cache-only).
- [x] **Step 2:** Compare `meta.json` with `scratchpad/reference-schema.json`: same 18 keys, `dangling == 0`, counts within ±10% of 6461 / 906 / 272290 or explained by the snapshot diff printed by step 00.
- [x] **Step 3:** Record the numbers in `eyesonflock/README.md` ("Reference run") and commit — `"eyesonflock: record first end-to-end run"`.
- [x] **Step 4:** Write a memory note (project) with location, phase status, and the no-push rule; final report to the owner with the design decisions to confirm and the exact command to push when ready.
