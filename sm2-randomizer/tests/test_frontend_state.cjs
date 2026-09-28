const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

function loadApp() {
  let code = fs.readFileSync(path.join(__dirname, '../app/static/main.js'), 'utf8');
  code = code.slice(0, code.lastIndexOf('  bindEvents();')) +
    'globalThis.app={state,saveCurrentConfig,drawModifier,removeModifierById,render,resetCurrentPlayer,rerollTalents,rerollLoadout,resolveAssetPath};})();';
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      style: { removeProperty() {}, setProperty() {} }, dataset: {},
      classList: { toggle() {}, remove() {}, add() {} }, querySelector() { return null; }
    });
    return elements.get(id);
  };
  const context = { document: { getElementById: element, querySelector() { return null; }, querySelectorAll() { return []; } } };
  vm.createContext(context);
  vm.runInContext(code, context);
  return { ...context.app, element };
}

test('members may complete in any order and completion can be invalidated', () => {
  const a = loadApp();
  for (let i = 0; i < 3; i++) {
    const slug = ['tactical', 'vanguard', 'heavy'][i];
    const weapons = Object.fromEntries(['primary', 'secondary', 'melee'].map(slot => [slot, {slug: slot}]));
    a.state.team[i] = { classSlug: slug, ...weapons, hasDrawnLoadout: true, hasDrawnTalents: true, talents: [], confirmed: false };
    a.state.classLookup[slug] = { slug, loadout_pools: Object.fromEntries(Object.entries(weapons).map(([slot, item]) => [slot, [item]])) };
  }
  for (const index of [2, 0, 1]) {
    a.state.activeIndex = index;
    a.saveCurrentConfig();
    assert.equal(a.state.squadCompleted, index === 1);
    if (index !== 1) assert.equal(a.state.team[a.state.activeIndex].confirmed, false);
  }
  a.rerollTalents();
  assert.equal(a.state.squadCompleted, false);
  a.saveCurrentConfig();
  assert.equal(a.state.squadCompleted, true);
  a.resetCurrentPlayer();
  assert.equal(a.state.squadCompleted, false);
});

test('modifier controls track cap, removal and subsequent draws without full render', () => {
  const a = loadApp();
  a.state.meta = {
    positive_modifier_pool: [{ key: 'positive', name: 'P', detail: 'D' }],
    negative_modifier_pool: [{ key: 'negative', name: 'N', label: 'N', detail: 'D' }]
  };
  a.drawModifier('negative');
  for (let i = 0; i < 11; i++) a.drawModifier('positive');
  assert.equal(a.element('draw-positive-button').disabled, true);
  assert.equal(a.element('draw-negative-button').disabled, true);
  a.render();
  a.removeModifierById(a.state.modifierFeed[0].id);
  assert.equal(a.element('draw-positive-button').disabled, false);
  assert.equal(a.element('draw-negative-button').disabled, false);
  a.drawModifier('negative');
  assert.equal(a.state.modifierFeed.length, 12);
  assert.equal(a.element('draw-positive-button').disabled, true);
});

test('missing required weapons cannot be drawn or confirmed through stale flags', () => {
  for (const slug of ['tactical', 'assault', 'bulwark', 'unknown']) {
    for (const missing of ['primary', 'secondary', 'melee']) {
      const a = loadApp();
      const pools = Object.fromEntries(['primary', 'secondary', 'melee'].map(slot => [slot, [{slug: slot}]]));
      pools[missing] = [];
      a.state.classLookup[slug] = {slug, loadout_pools: pools};
      a.state.team[0].classSlug = slug;
      a.rerollLoadout();
      const optional = ['assault', 'bulwark'].includes(slug) && missing === 'primary';
      assert.equal(a.state.team[0].hasDrawnLoadout, optional);
      a.state.team[0].hasDrawnLoadout = true;
      a.state.team[0].hasDrawnTalents = true;
      a.saveCurrentConfig();
      assert.equal(a.state.team[0].confirmed, optional);
    }
  }
});

test('asset URLs accept only literal relative image paths', () => {
  const a = loadApp();
  assert.equal(a.resolveAssetPath('talents/战术兵/技能.png'), '../assets/talents/战术兵/技能.png');
  for (const path of ['../x.png', '/x.png', 'C:/x.png', '//host/x.png', 'a\\x.png',
    'HTTPS://host/x.png', 'a/./x.png', 'a//x.png', '%2e%2e/x.png', '%252e%252e/x.png',
    'x.png?y', 'x.png#y', 'x.svg', 'x.txt', {}, null]) {
    assert.equal(a.resolveAssetPath(path), '');
  }
});

test('confirmation rejects a weapon that is not in the current class pool', () => {
  const a = loadApp();
  const slug = 'tactical';
  a.state.classLookup[slug] = {slug, loadout_pools: Object.fromEntries(
    ['primary', 'secondary', 'melee'].map(slot => [slot, [{slug: slot}]]))};
  a.state.team[0].classSlug = slug;
  a.rerollLoadout();
  a.state.team[0].secondary = {slug: 'foreign-weapon'};
  a.state.team[0].hasDrawnTalents = true;
  a.render();
  assert.equal(a.element('save-config-button').disabled, true);
  a.saveCurrentConfig();
  assert.equal(a.state.team[0].confirmed, false);
});
