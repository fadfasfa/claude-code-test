const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

function loadApp() {
  let code = fs.readFileSync(path.join(__dirname, '../app/static/main.js'), 'utf8');
  code = code.slice(0, code.lastIndexOf('  bindEvents();')) +
    'globalThis.app={state,saveCurrentConfig,drawModifier,removeModifierById,render,resetCurrentPlayer,rerollTalents};})();';
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
    a.state.team[i] = { classSlug: `class-${i}`, hasDrawnLoadout: true, hasDrawnTalents: true, talents: [], confirmed: false };
    a.state.classLookup[`class-${i}`] = { slug: `class-${i}` };
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
