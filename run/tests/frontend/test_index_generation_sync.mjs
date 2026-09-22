// Execute the real index ESM with network/DOM imports replaced; no browser or server.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const requests = [];
const displayed = [];
const failures = [];
let ws;
const node = { classList: { add() {}, remove() {} }, innerHTML: '' };
const context = vm.createContext({
    console: { log() {}, error() {} },
    document: { readyState: 'complete', getElementById: () => node },
    localStorage: { getItem: () => '1' },
    requestAnimationFrame: fn => fn(), setTimeout: () => 1, clearTimeout() {},
    fetch: url => {
        if (url.endsWith('/api/champion_aliases')) return Promise.resolve({ ok: true, json: async () => [] });
        assert.ok(url.endsWith('/api/champions'));
        return new Promise(resolve => requests.push(resolve));
    },
});
const deps = {
    './shared/net.js': { apiBase: () => 'http://fixture', wsUrl: () => 'ws://fixture',
        createReconnectingWS: options => { ws = options; return {}; } },
    './shared/ddragon.js': { refreshDdragonVersion() {} },
    './index/render.js': { renderTiers() {}, renderLoadFailure: (_node, unavailable) => failures.push(unavailable),
        updateHighlights() {}, formatPercent: String },
    './index/search.js': { createSearchController: () => ({ setup() {}, reapply() {},
        setChampions: rows => displayed.push(rows) }), resolveAliasRecords: async () => [] },
    './index/preload.js': { bindChampionPreloads() {} },
    './index/champion_pinyin.js': { CHAMPION_PINYIN: {} },
};
const source = fs.readFileSync(new URL('../../src/hextech/interfaces/web/backend/static/js/index.js', import.meta.url), 'utf8');
const entry = new vm.SourceTextModule(source, { context });
await entry.link(specifier => {
    const exports = deps[specifier];
    assert.ok(exports, specifier);
    return new vm.SyntheticModule(Object.keys(exports), function () {
        for (const [name, value] of Object.entries(exports)) this.setExport(name, value);
    }, { context });
});
await entry.evaluate();
const flush = () => new Promise(resolve => setImmediate(resolve));
const reply = (resolve, rows) => resolve({ ok: true, json: async () => rows });
assert.equal(requests.length, 1, 'bootstrap must load without waiting for WebSocket');
// Data arrived before subscription; onOpen must reconcile even with an old request in flight.
ws.onOpen();
ws.onOpen();
assert.equal(requests.length, 1, 'concurrent sync requests must coalesce');
requests[0]({ ok: false, status: 503, json: async () => ({ error: 'snapshot_unavailable' }) });
await flush();
assert.deepEqual(failures, [true]);
assert.equal(requests.length, 2, 'successful subscription must retry the pre-subscription snapshot');
reply(requests[1], ['A']);
await flush();
assert.deepEqual(displayed, [['A']]);
ws.onOpen();
assert.equal(requests.length, 3, 'reconnect must refresh data missed while disconnected');
ws.onMessage({ type: 'data_updated' });
ws.onMessage({ type: 'data_updated' });
assert.equal(requests.length, 3, 'notifications may queue only one follow-up');
reply(requests[2], ['B']);
await flush();
assert.equal(requests.length, 4);
reply(requests[3], ['C']);
await flush();
assert.deepEqual(displayed, [['A'], ['B'], ['C']]);
assert.equal(requests.length, 4, 'no unbounded retry loop');
console.log('index generation synchronization passed');
