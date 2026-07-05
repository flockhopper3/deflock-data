# analysis/

Analysis & research on the camera dataset — notebooks, one-off studies, and writeups (coverage growth over time, operator/brand breakdowns, geographic density, etc.).

**Status: empty for now.** Conventions when work lands here:

- One subfolder per study, named `YYYY-MM-topic/`, containing its code and a README with the question, method, and findings
- Keep raw data out of git — pull from the public tile/data URLs (see the [root README](../README.md)) or the R2 buckets, and write intermediate artifacts to the study folder (gitignored patterns cover `.geojson`/`.pmtiles`)
- Anything reusable across studies graduates to a shared `analysis/lib/`
