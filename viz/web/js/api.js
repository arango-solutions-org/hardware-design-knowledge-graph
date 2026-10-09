// api.js — thin fetch wrapper for the ChronoGraph backend
//
// Every path is RELATIVE and resolved against the page's own URL, never the
// origin root: on the Arango platform the app is mounted under
// /_service/uds/_db/<db>/<instance>/ (trailing slash required), so an API
// path with a leading slash would miss the prefix and 404. Locally the page is
// at the origin root, so 'api/health' resolves exactly as it always did.
// (tests/test_viz_platform.py and viz/deploy/byoc_deploy.py both guard this.)
const API = {
  async _get(path, params) {
    const url = new URL(path, document.baseURI);
    if (params) Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null) url.searchParams.set(k, v);
    });
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${path} → ${r.status} ${await r.text().catch(() => '')}`);
    return r.json();
  },
  health()          { return this._get('api/health'); },
  repos()           { return this._get('api/repos'); },
  timeline()        { return this._get('api/timeline'); },
  slice(ts, repos, projection) {
    // /api/slice takes an integer unix timestamp. The play loop advances the
    // cursor by fractional seconds for a smooth playhead; sending that float
    // got a 422 on every frame after the first, freezing the graph during
    // playback (drag was unaffected — Timeline.tsOf rounds).
    return this._get('api/slice', { ts: Math.floor(ts), repos: repos.join(','), projection });
  },
  provenance(id)    { return this._get('api/provenance', { id }); },
  source(kind, ref, terms) {
    return this._get('api/source', { kind, ref, terms: (terms || []).join('|') });
  },
  search(q, repos)  { return this._get('api/search', { q, repos: (repos || []).join(',') }); },
};
