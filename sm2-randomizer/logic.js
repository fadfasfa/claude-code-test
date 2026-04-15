(function (root) {
  "use strict";

  const APP_MODES = Object.freeze({
    SINGLE: "single",
    TEAM: "team"
  });

  const PLAYER_STATUS = Object.freeze({
    EMPTY: "empty",
    READY: "ready",
    FAILED: "failed"
  });

  const ERROR_CODES = Object.freeze({
    NO_AVAILABLE_CLASS: "NO_AVAILABLE_CLASS"
  });

  const CLASSES = Object.freeze([
    Object.freeze({
      id: "tactical",
      name: "Tactical",
      role: "Adaptive Marksman",
      tagline: "Balanced rifleman tuned for flexible line breaking.",
      primary: Object.freeze(["Bolt Rifle", "Plasma Incinerator", "Stalker Bolt Rifle"]),
      secondary: Object.freeze(["Bolt Pistol", "Plasma Pistol"]),
      melee: Object.freeze(["Combat Knife", "Chainsword"])
    }),
    Object.freeze({
      id: "assault",
      name: "Assault",
      role: "Jump Pack Shock",
      tagline: "High-mobility shock unit built to punish exposed lines.",
      primary: Object.freeze([]),
      secondary: Object.freeze(["Heavy Bolt Pistol", "Plasma Pistol"]),
      melee: Object.freeze(["Chainsword", "Power Fist", "Thunder Hammer"])
    }),
    Object.freeze({
      id: "bulwark",
      name: "Bulwark",
      role: "Shield Anchor",
      tagline: "Frontline wall that absorbs pressure and stabilizes the squad.",
      primary: Object.freeze([]),
      secondary: Object.freeze(["Bolt Pistol", "Plasma Pistol", "Neo-Volkite Pistol"]),
      melee: Object.freeze(["Power Sword", "Power Fist", "Chainsword"])
    }),
    Object.freeze({
      id: "heavy",
      name: "Heavy",
      role: "Suppressive Fortress",
      tagline: "Slow advance, maximal fire lane control, zero subtlety.",
      primary: Object.freeze(["Heavy Bolter", "Multi-Melta", "Heavy Plasma Incinerator"]),
      secondary: Object.freeze(["Bolt Pistol"]),
      melee: Object.freeze(["Combat Knife"])
    }),
    Object.freeze({
      id: "sniper",
      name: "Sniper",
      role: "Precision Hunter",
      tagline: "Long sightline eliminator with surgical ranged dominance.",
      primary: Object.freeze(["Las Fusil", "Bolt Sniper Rifle", "Stalker Bolt Carbine"]),
      secondary: Object.freeze(["Bolt Pistol", "Machine Pistol"]),
      melee: Object.freeze(["Combat Knife", "Combat Blade"])
    })
  ]);

  const TALENT_POOL = Object.freeze([
    "Auspex Relay",
    "Shock Grenade Servo",
    "Iron Halo Tuning",
    "Adrenal Injector",
    "Purity Seal Chain",
    "Reactor Overdrive",
    "Vengeance Protocol",
    "Targeting Catechism",
    "Armor Lock",
    "Glory Kill Circuit",
    "Martial Focus",
    "Sainted Ammunition"
  ]);

  const MODIFIERS = Object.freeze({
    positive: Object.freeze([
      Object.freeze({ id: "p01", text: "主武器伤害 +10%", type: "positive" }),
      Object.freeze({ id: "p02", text: "副武器装填速度 +20%", type: "positive" }),
      Object.freeze({ id: "p03", text: "近战暴击率 +15%", type: "positive" }),
      Object.freeze({ id: "p04", text: "生命值上限 +12%", type: "positive" }),
      Object.freeze({ id: "p05", text: "护甲恢复速度 +18%", type: "positive" }),
      Object.freeze({ id: "p06", text: "冲刺消耗 -25%", type: "positive" }),
      Object.freeze({ id: "p07", text: "技能冷却 -10%", type: "positive" }),
      Object.freeze({ id: "p08", text: "处决回血 +20%", type: "positive" }),
      Object.freeze({ id: "p09", text: "爆头伤害 +14%", type: "positive" }),
      Object.freeze({ id: "p10", text: "换弹时移动速度 +10%", type: "positive" }),
      Object.freeze({ id: "p11", text: "格挡宽容窗口 +18%", type: "positive" }),
      Object.freeze({ id: "p12", text: "破甲伤害 +11%", type: "positive" }),
      Object.freeze({ id: "p13", text: "近战终结技动画 -20%", type: "positive" }),
      Object.freeze({ id: "p14", text: "爆炸抗性 +16%", type: "positive" }),
      Object.freeze({ id: "p15", text: "主武器弹匣容量 +1 档", type: "positive" }),
      Object.freeze({ id: "p16", text: "处决后护甲返还 +1 格", type: "positive" }),
      Object.freeze({ id: "p17", text: "闪避后首发伤害 +25%", type: "positive" }),
      Object.freeze({ id: "p18", text: "榴弹半径 +12%", type: "positive" }),
      Object.freeze({ id: "p19", text: "精英敌人受伤加深 +9%", type: "positive" }),
      Object.freeze({ id: "p20", text: "复活队友速度 +22%", type: "positive" }),
      Object.freeze({ id: "p21", text: "怒气恢复效率 +17%", type: "positive" }),
      Object.freeze({ id: "p22", text: "完美招架后伤害减免 +12%", type: "positive" })
    ]),
    negative: Object.freeze([
      Object.freeze({ id: "n01", text: "主武器后坐力 +20%", type: "negative" }),
      Object.freeze({ id: "n02", text: "副武器伤害 -12%", type: "negative" }),
      Object.freeze({ id: "n03", text: "近战攻速 -15%", type: "negative" }),
      Object.freeze({ id: "n04", text: "生命恢复延迟 +30%", type: "negative" }),
      Object.freeze({ id: "n05", text: "护甲上限 -1 格", type: "negative" }),
      Object.freeze({ id: "n06", text: "冲刺速度 -10%", type: "negative" }),
      Object.freeze({ id: "n07", text: "受爆炸伤害 +18%", type: "negative" }),
      Object.freeze({ id: "n08", text: "技能冷却 +12%", type: "negative" }),
      Object.freeze({ id: "n09", text: "处决动画时间 +15%", type: "negative" }),
      Object.freeze({ id: "n10", text: "爆头判定窗口 -20%", type: "negative" }),
      Object.freeze({ id: "n11", text: "主武器切换时间 +18%", type: "negative" }),
      Object.freeze({ id: "n12", text: "副武器散布 +22%", type: "negative" }),
      Object.freeze({ id: "n13", text: "格挡体力消耗 +16%", type: "negative" }),
      Object.freeze({ id: "n14", text: "处决回血效果 -25%", type: "negative" }),
      Object.freeze({ id: "n15", text: "精英敌人仇恨值 +1 档", type: "negative" }),
      Object.freeze({ id: "n16", text: "闪避无敌帧 -12%", type: "negative" }),
      Object.freeze({ id: "n17", text: "技能施放前摇 +20%", type: "negative" }),
      Object.freeze({ id: "n18", text: "近战蓄力伤害 -18%", type: "negative" }),
      Object.freeze({ id: "n19", text: "范围武器自伤惩罚 +14%", type: "negative" }),
      Object.freeze({ id: "n20", text: "精准射击稳定时间 +0.4 秒", type: "negative" }),
      Object.freeze({ id: "n21", text: "复活队友速度 -25%", type: "negative" }),
      Object.freeze({ id: "n22", text: "护甲恢复起始时间 +1.2 秒", type: "negative" })
    ])
  });

  const CLASS_LOOKUP = CLASSES.reduce((lookup, entry) => {
    lookup[entry.id] = entry;
    return lookup;
  }, {});

  function cloneClass(entry) {
    if (!entry) {
      return null;
    }

    return {
      id: entry.id,
      name: entry.name,
      role: entry.role,
      tagline: entry.tagline,
      primary: [...entry.primary],
      secondary: [...entry.secondary],
      melee: [...entry.melee]
    };
  }

  function clonePlayer(player) {
    return {
      status: player.status,
      class: cloneClass(player.class),
      primary: [...player.primary],
      secondary: player.secondary,
      melee: player.melee,
      talents_enabled: player.talents_enabled,
      talents: [...player.talents],
      error: player.error ? { ...player.error } : null
    };
  }

  function createEmptyPlayer() {
    return {
      status: PLAYER_STATUS.EMPTY,
      class: null,
      primary: [],
      secondary: null,
      melee: null,
      talents_enabled: true,
      talents: [],
      error: null
    };
  }

  function createInitialState() {
    return {
      app_mode: APP_MODES.SINGLE,
      global_modifiers: {
        positive: [],
        negative: []
      },
      players: [createEmptyPlayer(), createEmptyPlayer(), createEmptyPlayer()],
      used_classes: [],
      active_player_index: 0
    };
  }

  let state = createInitialState();

  function ensureValidIndex(index) {
    if (index !== 0 && index !== 1 && index !== 2) {
      throw new Error("active player index must be 0 | 1 | 2");
    }
  }

  function randomIndex(list) {
    return Math.floor(Math.random() * list.length);
  }

  function randomPick(list) {
    if (!Array.isArray(list) || list.length === 0) {
      throw new Error("randomPick requires a non-empty list");
    }

    return list[randomIndex(list)];
  }

  function pickUnique(list, count) {
    const pool = [...list];
    const picks = [];
    const target = Math.min(count, pool.length);

    while (picks.length < target) {
      picks.push(pool.splice(randomIndex(pool), 1)[0]);
    }

    return picks;
  }

  function buildError(code, message) {
    return { code, message };
  }

  function deriveUsedClasses(players) {
    const ids = [];
    const seen = new Set();

    players.forEach((player) => {
      const classId = player.class && player.class.id;
      if (classId && !seen.has(classId)) {
        seen.add(classId);
        ids.push(classId);
      }
    });

    return ids;
  }

  function cloneState(currentState) {
    return {
      app_mode: currentState.app_mode,
      global_modifiers: {
        positive: currentState.global_modifiers.positive.map((modifier) => ({ ...modifier })),
        negative: currentState.global_modifiers.negative.map((modifier) => ({ ...modifier }))
      },
      players: currentState.players.map(clonePlayer),
      used_classes: [...currentState.used_classes],
      active_player_index: currentState.active_player_index
    };
  }

  function commitState(nextState) {
    nextState.used_classes = deriveUsedClasses(nextState.players);

    if (nextState.app_mode === APP_MODES.SINGLE) {
      nextState.active_player_index = 0;
    } else if (nextState.active_player_index < 0 || nextState.active_player_index > 2) {
      nextState.active_player_index = 0;
    }

    state = nextState;
    return getStateSnapshot();
  }

  function buildRolledPlayer(previousPlayer, occupiedClasses) {
    const availableClasses = CLASSES.filter((entry) => !occupiedClasses.has(entry.id));
    if (availableClasses.length === 0) {
      return {
        ok: false,
        error: buildError(ERROR_CODES.NO_AVAILABLE_CLASS, "No available class")
      };
    }

    const pickedClass = cloneClass(randomPick(availableClasses));

    return {
      ok: true,
      player: {
        status: PLAYER_STATUS.READY,
        class: pickedClass,
        primary: pickedClass.primary.length > 0 ? [randomPick(pickedClass.primary)] : [],
        secondary: randomPick(pickedClass.secondary),
        melee: randomPick(pickedClass.melee),
        talents_enabled: previousPlayer.talents_enabled,
        talents: pickUnique(TALENT_POOL, 3),
        error: null
      }
    };
  }

  function drawForIndex(index) {
    ensureValidIndex(index);

    const nextState = cloneState(state);
    const currentPlayer = nextState.players[index];
    const occupiedClasses = new Set(deriveUsedClasses(nextState.players));

    if (currentPlayer.class && currentPlayer.class.id) {
      occupiedClasses.delete(currentPlayer.class.id);
    }

    const rolled = buildRolledPlayer(currentPlayer, occupiedClasses);
    if (!rolled.ok) {
      nextState.players[index] = {
        ...clonePlayer(currentPlayer),
        status: currentPlayer.class ? currentPlayer.status : PLAYER_STATUS.FAILED,
        error: rolled.error
      };

      const snapshotOnFail = commitState(nextState);
      return {
        ok: false,
        error: rolled.error,
        state: snapshotOnFail
      };
    }

    nextState.players[index] = rolled.player;
    const snapshot = commitState(nextState);

    return {
      ok: true,
      error: null,
      state: snapshot
    };
  }

  function getStateSnapshot() {
    return cloneState(state);
  }

  function setAppMode(mode) {
    if (mode !== APP_MODES.SINGLE && mode !== APP_MODES.TEAM) {
      throw new Error("app_mode must be single | team");
    }

    const nextState = cloneState(state);
    const currentPlayer = clonePlayer(nextState.players[nextState.active_player_index]);

    if (mode === nextState.app_mode) {
      if (mode === APP_MODES.SINGLE) {
        nextState.active_player_index = 0;
      }
      return commitState(nextState);
    }

    nextState.players = [currentPlayer, createEmptyPlayer(), createEmptyPlayer()];
    nextState.app_mode = mode;
    nextState.active_player_index = 0;
    return commitState(nextState);
  }

  function setActivePlayer(index) {
    ensureValidIndex(index);

    const nextState = cloneState(state);
    nextState.active_player_index = nextState.app_mode === APP_MODES.SINGLE ? 0 : index;
    return commitState(nextState);
  }

  function setTalentsEnabledForActivePlayer(enabled) {
    const nextState = cloneState(state);
    const targetIndex = nextState.app_mode === APP_MODES.SINGLE ? 0 : nextState.active_player_index;
    nextState.players[targetIndex].talents_enabled = Boolean(enabled);
    return commitState(nextState);
  }

  function drawForActivePlayer() {
    const activeIndex = state.app_mode === APP_MODES.SINGLE ? 0 : state.active_player_index;
    return drawForIndex(activeIndex);
  }

  function rerollActivePlayer() {
    const activeIndex = state.app_mode === APP_MODES.SINGLE ? 0 : state.active_player_index;
    return drawForIndex(activeIndex);
  }

  function resetActivePlayer() {
    const nextState = cloneState(state);
    const activeIndex = nextState.app_mode === APP_MODES.SINGLE ? 0 : nextState.active_player_index;
    nextState.players[activeIndex] = createEmptyPlayer();
    return commitState(nextState);
  }

  function resetAll() {
    const nextState = createInitialState();
    nextState.app_mode = state.app_mode;
    nextState.active_player_index = state.app_mode === APP_MODES.SINGLE ? 0 : state.active_player_index;
    return commitState(nextState);
  }

  function drawGlobalModifier(type) {
    if (type !== "positive" && type !== "negative") {
      throw new Error("modifier type must be positive | negative");
    }

    const nextState = cloneState(state);
    const picked = { ...randomPick(MODIFIERS[type]) };
    nextState.global_modifiers[type].push(picked);
    const snapshot = commitState(nextState);

    return {
      picked,
      state: snapshot
    };
  }

  const AppLogic = Object.freeze({
    APP_MODES,
    PLAYER_STATUS,
    ERROR_CODES,
    getStateSnapshot,
    setAppMode,
    setActivePlayer,
    drawForActivePlayer,
    rerollActivePlayer,
    resetActivePlayer,
    resetAll,
    drawGlobalModifier,
    setTalentsEnabledForActivePlayer
  });

  root.AppLogic = AppLogic;

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function playerStatusLabel(player) {
    if (player.status === PLAYER_STATUS.READY) {
      return "Loadout Locked";
    }
    if (player.status === PLAYER_STATUS.FAILED) {
      return "Draw Failed";
    }
    return "Awaiting Draw";
  }

  function playerStatusClass(player) {
    if (player.status === PLAYER_STATUS.READY) {
      return "is-ready";
    }
    if (player.status === PLAYER_STATUS.FAILED) {
      return "is-failed";
    }
    return "";
  }

  function renderRosterLock(snapshot) {
    if (snapshot.used_classes.length === 0) {
      return '<span class="roster-lock__empty">No class lock engaged.</span>';
    }

    return snapshot.used_classes
      .map((classId) => {
        const entry = CLASS_LOOKUP[classId];
        const label = entry ? entry.name : classId;
        return `<span class="roster-lock__pill">${escapeHtml(label)}</span>`;
      })
      .join("");
  }

  function renderPlayerTabs(snapshot) {
    return snapshot.players
      .map((player, index) => {
        const className = player.class ? player.class.name : "Awaiting Draw";
        const activeClass = index === snapshot.active_player_index ? "is-active" : "";
        return `
          <button type="button" class="player-tab ${activeClass}" data-player-tab="${index}">
            <span class="player-tab__label">Player ${index + 1}</span>
            <span class="player-tab__meta">${escapeHtml(className)}</span>
            <span class="player-tab__status">${escapeHtml(playerStatusLabel(player))}</span>
          </button>
        `;
      })
      .join("");
  }

  function renderClassPanel(snapshot, activePlayer) {
    const className = activePlayer.class ? activePlayer.class.name : "No Assignment";
    const classRole = activePlayer.class ? activePlayer.class.role : "Awaiting Squad Draft";
    const classTagline = activePlayer.class
      ? activePlayer.class.tagline
      : "Roll the active marine to assign a class, weapon package, and talent slate.";
    const hasPrimaryWeapon = activePlayer.class && activePlayer.class.primary.length > 0 ? "Primary Package" : "Primary Package Disabled";
    const errorMarkup = activePlayer.error
      ? `
        <div class="error-banner">
          <strong>${escapeHtml(activePlayer.error.code)}</strong>
          <span>${escapeHtml(activePlayer.error.message)}</span>
        </div>
      `
      : "";

    return `
      <article class="class-card">
        <span class="class-card__status ${playerStatusClass(activePlayer)}">${escapeHtml(playerStatusLabel(activePlayer))}</span>
        <div>
          <h3 class="class-card__name">${escapeHtml(className)}</h3>
          <p class="class-card__role">${escapeHtml(classRole)}</p>
          <p class="class-card__tagline">${escapeHtml(classTagline)}</p>
        </div>
        <div class="intel-grid">
          <div class="intel-card">
            <span class="intel-card__label">Deployment Mode</span>
            <strong>${snapshot.app_mode === APP_MODES.TEAM ? "3-Marine Kill Team" : "Solo Marine"}</strong>
          </div>
          <div class="intel-card">
            <span class="intel-card__label">Talent Channel</span>
            <strong>${activePlayer.talents_enabled ? "Enabled" : "Disabled"}</strong>
          </div>
          <div class="intel-card">
            <span class="intel-card__label">Primary Slot</span>
            <strong>${escapeHtml(hasPrimaryWeapon)}</strong>
          </div>
          <div class="intel-card">
            <span class="intel-card__label">Roster Lock</span>
            <strong>${snapshot.used_classes.length} / 3</strong>
          </div>
        </div>
        <div>
          <p class="intel-card__label">Used Classes</p>
          <div class="roster-lock">${renderRosterLock(snapshot)}</div>
        </div>
        ${errorMarkup}
      </article>
    `;
  }

  function renderWeaponSlot(label, value, caption, isEmpty) {
    const slotClass = isEmpty ? "empty-slot" : "weapon-slot";
    return `
      <article class="${slotClass}">
        <span class="slot-card__label">${escapeHtml(label)}</span>
        <strong class="slot-card__value">${escapeHtml(value)}</strong>
        <span class="slot-card__caption">${escapeHtml(caption)}</span>
      </article>
    `;
  }

  function renderTalentSlot(player) {
    if (player.status === PLAYER_STATUS.EMPTY) {
      return renderWeaponSlot(
        "Talent Suite",
        "Awaiting Draw",
        "Deploy the active marine to reveal the talent slate.",
        true
      );
    }

    if (!player.talents_enabled) {
      return renderWeaponSlot(
        "Talent Suite",
        "Talent Disabled",
        "Top-bar switch is off for the active player. The middle panel remains reserved.",
        true
      );
    }

    const talents = player.talents.length
      ? `<ul class="talent-list">${player.talents
          .map((talent) => `<li>${escapeHtml(talent)}</li>`)
          .join("")}</ul>`
      : '<div class="message-card">No talent entries stored for this marine yet.</div>';

    return `
      <article class="slot-card">
        <span class="slot-card__label">Talent Suite</span>
        <strong class="slot-card__value">3 Active Traits</strong>
        ${talents}
      </article>
    `;
  }

  function renderLoadoutPanel(activePlayer) {
    const primaryEmpty = !activePlayer.class || activePlayer.primary.length === 0;
    const primaryValue = primaryEmpty ? "No Primary Weapon" : activePlayer.primary[0];
    const primaryCaption = primaryEmpty
      ? "This class is defined with primary: [] and must render the empty-slot state."
      : "Primary weapon slot rolled from the class pool.";
    const secondaryValue = activePlayer.secondary || "Awaiting Draw";
    const secondaryCaption = activePlayer.secondary
      ? "Secondary sidearm assigned from the active class."
      : "Roll a marine to assign a secondary weapon.";
    const meleeValue = activePlayer.melee || "Awaiting Draw";
    const meleeCaption = activePlayer.melee
      ? "Close-quarters option selected for the active class."
      : "Roll a marine to assign a melee weapon.";

    return `
      <div class="slot-grid">
        ${renderWeaponSlot("Primary", primaryValue, primaryCaption, primaryEmpty)}
        ${renderWeaponSlot("Secondary", secondaryValue, secondaryCaption, !activePlayer.secondary)}
        ${renderWeaponSlot("Melee", meleeValue, meleeCaption, !activePlayer.melee)}
        ${renderTalentSlot(activePlayer)}
      </div>
    `;
  }

  function renderModifierList(type, items) {
    if (items.length === 0) {
      return '<div class="message-card">No modifier rolled yet. Use the command buttons above to draw one entry at a time.</div>';
    }

    return `
      <div class="modifier-list">
        ${items
          .map(
            (item) => `
              <div class="modifier-chip modifier-chip--${type}">
                ${escapeHtml(item.text)}
              </div>
            `
          )
          .join("")}
      </div>
    `;
  }

  function renderModifierPanel(globalModifiers) {
    return `
      <div class="modifier-controls">
        <button type="button" class="modifier-button modifier-button--positive" data-modifier-draw="positive">
          抽取正面词条
        </button>
        <button type="button" class="modifier-button modifier-button--negative" data-modifier-draw="negative">
          抽取负面词条
        </button>
      </div>

      <section class="modifier-stack">
        <div class="modifier-stack__header">
          <span class="modifier-stack__title">Positive Modifiers</span>
          <span class="modifier-stack__count">${globalModifiers.positive.length}</span>
        </div>
        ${renderModifierList("positive", globalModifiers.positive)}
      </section>

      <section class="modifier-stack">
        <div class="modifier-stack__header">
          <span class="modifier-stack__title">Negative Modifiers</span>
          <span class="modifier-stack__count">${globalModifiers.negative.length}</span>
        </div>
        ${renderModifierList("negative", globalModifiers.negative)}
      </section>
    `;
  }

  function initBrowserApp() {
    if (typeof document === "undefined") {
      return;
    }

    const appRoot = document.querySelector("[data-sm2-app]");
    if (!appRoot) {
      return;
    }

    const refs = {
      teamModeToggle: document.getElementById("team-mode-toggle"),
      talentToggle: document.getElementById("talent-toggle"),
      modeReadout: document.getElementById("mode-readout"),
      activePlayerReadout: document.getElementById("active-player-readout"),
      playerTabs: document.getElementById("player-tabs"),
      classPanel: document.getElementById("class-panel"),
      loadoutPanel: document.getElementById("loadout-panel"),
      modifierPanel: document.getElementById("modifier-panel")
    };

    function renderApp() {
      const snapshot = AppLogic.getStateSnapshot();
      const activePlayer = snapshot.players[snapshot.active_player_index];

      refs.teamModeToggle.checked = snapshot.app_mode === APP_MODES.TEAM;
      refs.talentToggle.checked = activePlayer.talents_enabled;
      refs.modeReadout.textContent = snapshot.app_mode === APP_MODES.TEAM ? "Team Mode / 3 Marines" : "Single Mode / Solo";
      refs.activePlayerReadout.textContent = `Player ${snapshot.active_player_index + 1}`;
      refs.playerTabs.hidden = snapshot.app_mode !== APP_MODES.TEAM;
      refs.playerTabs.innerHTML = renderPlayerTabs(snapshot);
      refs.classPanel.innerHTML = renderClassPanel(snapshot, activePlayer);
      refs.loadoutPanel.innerHTML = renderLoadoutPanel(activePlayer);
      refs.modifierPanel.innerHTML = renderModifierPanel(snapshot.global_modifiers);
    }

    appRoot.addEventListener("click", (event) => {
      const tabButton = event.target.closest("[data-player-tab]");
      if (tabButton) {
        AppLogic.setActivePlayer(Number(tabButton.getAttribute("data-player-tab")));
        renderApp();
        return;
      }

      const actionButton = event.target.closest("[data-action]");
      if (actionButton) {
        const action = actionButton.getAttribute("data-action");
        if (action === "draw") {
          AppLogic.drawForActivePlayer();
        } else if (action === "reroll") {
          AppLogic.rerollActivePlayer();
        } else if (action === "reset-player") {
          AppLogic.resetActivePlayer();
        } else if (action === "reset-all") {
          AppLogic.resetAll();
        }
        renderApp();
        return;
      }

      const modifierButton = event.target.closest("[data-modifier-draw]");
      if (modifierButton) {
        AppLogic.drawGlobalModifier(modifierButton.getAttribute("data-modifier-draw"));
        renderApp();
      }
    });

    refs.teamModeToggle.addEventListener("change", (event) => {
      AppLogic.setAppMode(event.target.checked ? APP_MODES.TEAM : APP_MODES.SINGLE);
      renderApp();
    });

    refs.talentToggle.addEventListener("change", (event) => {
      AppLogic.setTalentsEnabledForActivePlayer(event.target.checked);
      renderApp();
    });

    renderApp();
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", initBrowserApp, { once: true });
    } else {
      initBrowserApp();
    }
  }
})(typeof window !== "undefined" ? window : globalThis);
