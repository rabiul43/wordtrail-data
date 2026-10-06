# wordtrail-data

Data and automation for the **Wordtrail** read-along audiobook app. The app reads these files straight from
`raw.githubusercontent.com/<you>/wordtrail-data/main/...`; GitHub Actions keep them fresh.

## Files the app reads
| File | Made by | Purpose |
|---|---|---|
| `catalog.json`, `details/` | `build_catalog.py` (weekly) | LibriVox catalog |
| `search.json`, `versions.json`, `gutenberg_stats.json` | `wt_smart_index.py` (daily) | search index, best recording per book |
| `levels.json`, `moods.json`, `similar.json`, `vocab/` | `build_levels.py`, `build_moods.py`, `build_vocab.py` (weekly) | reading level, mood, similar books, word lists |
| `home.json`, `curation.json` | `build_home.py`, `build_curation.py` (daily) | home page and collections |
| `cuts.json` | `wt_cuts.py` (daily) | intro/outro trims |
| `data/<id>/<track>.json`, `status.json` | `process.py` (on a `sync <id>` issue) | word timings from Whisper |
| `wt_packs/` | `wt23_build_packs.py` (daily) | complete books packed in one file |
| `align.json`, `health.json`, `quality.json` | `wt_align.py`, `wt_health.py`, `wt_quality.py` | checks and dashboard |

## Setup on a new account
1. Create a **public** repo named `wordtrail-data`, push this folder to `main`.
2. Settings → Actions → General → Workflow permissions: **Read and write**.
3. Optional secret `WT_PAT` (a token that can create issues) so the nightly alignment check can ask for re-syncs.
4. In the app: Settings → Advanced → Sync repository = `<you>/wordtrail-data`.
5. Run each workflow once from the Actions tab, starting with *Refresh catalog*.

## Keeping Actions usage safe
The heavy jobs are the Whisper sync (`sync.yml`, up to 350 min) and the weekly builds. `sync.yml` refuses new
syncs while 3 are already running. Do not run many heavy workflows at once, and avoid loops where a workflow
starts itself (only `catalog.yml` does, and it stops when the catalog is complete).
