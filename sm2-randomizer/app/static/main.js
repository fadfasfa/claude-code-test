(function () {
  const DATA_FILES = Object.freeze({
    classes: "../data/classes.json",
    talents: "../data/talents.json",
    meta: "../data/meta.json"
  });

  const MAX_MODIFIER_COUNT = 12;
  const MODIFIER_CAP_NOTICE = `已达到 ${MAX_MODIFIER_COUNT} 条策略词条上限`;
  const TALENT_VISUAL_COLUMNS = 8;
  const TALENT_VISUAL_ROWS = 3;

  const state = {
    classes: [],
    classLookup: {},
    talents: {},
    meta: {},
    team: Array.from({ length: 3 }, () => emptyPlayer()),
    activeIndex: 0,
    modifiers: { positive: [], negative: [] },
    modifierFeed: [],
    warning: "",
    hasShownModifierCapNotice: false,
    squadCompleted: false,
    tooltipTarget: null,
    modifierTooltipTarget: null
  };

  let modifierIdSeed = 0;

  function emptyPlayer() {
    return {
      classSlug: "",
      primary: null,
      secondary: null,
      melee: null,
      talents: [],
      confirmed: false,
      hasDrawnLoadout: false,
      hasDrawnTalents: false
    };
  }

  function randomPick(list) {
    return list[Math.floor(Math.random() * list.length)];
  }

  function uniqueBySlug(list) {
    const seen = new Set();
    return list.filter((item) => {
      const key = String((item && item.slug) || "");
      if (!key || seen.has(key)) {
        return false;
      }
      seen.add(key);
      return true;
    });
  }

  function shuffleList(list) {
    const next = [...list];
    for (let index = next.length - 1; index > 0; index -= 1) {
      const swapIndex = Math.floor(Math.random() * (index + 1));
      [next[index], next[swapIndex]] = [next[swapIndex], next[index]];
    }
    return next;
  }

  function currentPlayer() {
    return state.team[state.activeIndex];
  }

  function currentClass() {
    return state.classLookup[currentPlayer().classSlug] || null;
  }

  function confirmedCount() {
    return state.team.filter((player) => player.confirmed).length;
  }

  function invalidateSquadCompletion() {
    state.squadCompleted = false;
  }

  function clearPlayerConfig(player, classSlug) {
    return {
      ...emptyPlayer(),
      classSlug
    };
  }

  function resolveClassImage(entry) {
    return entry && entry.image_path ? `../assets/${entry.image_path}` : "";
  }

  function resolveWeaponImage(entry) {
    const assetPath = entry && entry.image_path;
    return assetPath ? `../assets/${assetPath}` : "";
  }

  function pickLoadout(list) {
    return Array.isArray(list) && list.length ? randomPick(list) : null;
  }

  function drawLoadoutForClass(entry) {
    const pools = (entry && entry.loadout_pools) || {};
    return {
      primary: pickLoadout(pools.primary),
      secondary: pickLoadout(pools.secondary),
      melee: pickLoadout(pools.melee)
    };
  }

  function drawTalents(classSlug) {
    const talentGroup = state.talents[classSlug] || { nodes: [] };
    const byVisualColumn = new Map();

    (talentGroup.nodes || []).forEach((item) => {
      const visualColumn = Number(item.row || 0);
      if (!byVisualColumn.has(visualColumn)) {
        byVisualColumn.set(visualColumn, []);
      }
      byVisualColumn.get(visualColumn).push(item);
    });

    const picks = [];
    for (let visualColumn = 1; visualColumn <= TALENT_VISUAL_COLUMNS; visualColumn += 1) {
      const items = byVisualColumn.get(visualColumn) || [];
      if (items.length) {
        picks.push(randomPick(items));
      }
    }
    return picks;
  }

  function buildPlayerStatus(player) {
    if (!player.classSlug) {
      return "等待机魂锁定职业。";
    }
    if (!player.hasDrawnLoadout && !player.hasDrawnTalents) {
      return "职业已锁定，待抽取军械与天赋。";
    }
    if (!player.hasDrawnLoadout) {
      return "职业已锁定，军械库待分配。";
    }
    if (!player.hasDrawnTalents) {
      return "军械库已完成分配，待抽取天赋。";
    }
    if (player.confirmed) {
      return "当前成员配置已确认。";
    }
    return "当前成员配置已完整，可保存并推进到下一名成员。";
  }

  function drawClassForIndex(index) {
    const used = new Set(state.team.map((player, playerIndex) => (playerIndex === index ? "" : player.classSlug)));
    const candidates = state.classes.filter((entry) => !used.has(entry.slug));
    invalidateSquadCompletion();
    hideTalentTooltip();
    if (!candidates.length) {
      state.team[index] = emptyPlayer();
      return;
    }

    const entry = randomPick(candidates);
    state.team[index] = clearPlayerConfig(state.team[index], entry.slug);
  }

  function drawSingle() {
    drawClassForIndex(state.activeIndex);
    render();
  }

  function resetCurrentPlayer() {
    state.team[state.activeIndex] = emptyPlayer();
    invalidateSquadCompletion();
    hideTalentTooltip();
    render();
  }

  function rerollLoadout() {
    const entry = currentClass();
    if (!entry) return;

    const next = drawLoadoutForClass(entry);
    state.team[state.activeIndex] = {
      ...state.team[state.activeIndex],
      primary: next.primary,
      secondary: next.secondary,
      melee: next.melee,
      confirmed: false,
      hasDrawnLoadout: true
    };
    invalidateSquadCompletion();
    render();
  }

  function rerollTalents() {
    const entry = currentClass();
    if (!entry) return;

    state.team[state.activeIndex] = {
      ...state.team[state.activeIndex],
      talents: drawTalents(entry.slug),
      confirmed: false,
      hasDrawnTalents: true
    };
    invalidateSquadCompletion();
    hideTalentTooltip();
    render();
  }

  function resetAll() {
    state.team = Array.from({ length: 3 }, () => emptyPlayer());
    state.activeIndex = 0;
    state.modifiers = { positive: [], negative: [] };
    state.modifierFeed = [];
    state.warning = "";
    state.hasShownModifierCapNotice = false;
    state.squadCompleted = false;
    hideTalentTooltip();
    render();
  }

  function saveCurrentConfig() {
    const player = currentPlayer();
    const readyToSave = player.classSlug && player.hasDrawnLoadout && player.hasDrawnTalents;
    if (!readyToSave || state.squadCompleted) {
      return;
    }

    player.confirmed = true;
    if (state.activeIndex < state.team.length - 1) {
      state.activeIndex += 1;
    } else {
      state.squadCompleted = true;
    }
    render();
  }

  function modifierLabel(value) {
    if (typeof value === "string") {
      return value;
    }
    if (value && typeof value === "object") {
      return String(value.name || value.label || value.detail || "");
    }
    return "";
  }

  function titleOfModifier(value) {
    return modifierLabel(value).split("：")[0];
  }

  function summarizeModifierBody(value) {
    const body = String(value || "").trim();
    if (!body) {
      return "暂无简述";
    }
    return body;
  }

  function getPositiveModifierPool() {
    const positive = Array.isArray(state.meta.positive_modifier_pool) ? state.meta.positive_modifier_pool : [];
    return positive.filter((item) => item && item.key && item.name && item.detail);
  }

  function getNegativeModifierRules() {
    const payload = state.meta && typeof state.meta.negative_modifier_rules === "object"
      ? state.meta.negative_modifier_rules
      : {};
    return {
      exactConflicts: Array.isArray(payload.exact_conflicts) ? payload.exact_conflicts : [],
      quotaLimits: payload && typeof payload.quota_limits === "object" && !Array.isArray(payload.quota_limits)
        ? payload.quota_limits
        : {},
      titleAliases: payload && typeof payload.title_aliases === "object" && !Array.isArray(payload.title_aliases)
        ? payload.title_aliases
        : {}
    };
  }

  function getNegativeModifierPool() {
    const pool = Array.isArray(state.meta.negative_modifier_pool) ? state.meta.negative_modifier_pool : [];
    return pool
      .filter((item) => item && typeof item === "object" && item.key && item.label)
      .map((item) => ({
        ...item,
        type: "negative",
        name: String((item.name || titleOfModifier(item.label)) || "").trim() || "未命名词条",
        detail: String((item.detail || item.label) || "").trim() || "暂无详细信息"
      }));
  }

  function resolveNegativeModifierKey(item) {
    if (item && typeof item === "object" && item.key) {
      return String(item.key).trim();
    }
    const label = modifierLabel(item);
    if (!label) {
      return "";
    }
    const aliases = getNegativeModifierRules().titleAliases;
    const title = titleOfModifier(label);
    return String(aliases[label] || aliases[title] || "").trim();
  }

  function getNegativeModifierKeys(items) {
    return items.map(resolveNegativeModifierKey).filter(Boolean);
  }

  function countNegativeCoreTags(items) {
    const counts = {};
    for (const item of items) {
      if (!item || typeof item !== "object" || !Array.isArray(item.core_tags)) {
        continue;
      }
      for (const rawTag of item.core_tags) {
        const tag = String(rawTag || "").trim();
        if (!tag) {
          continue;
        }
        counts[tag] = (counts[tag] || 0) + 1;
      }
    }
    return counts;
  }

  function findNegativeConflictMessage(items) {
    const selectedKeys = new Set(getNegativeModifierKeys(items));
    for (const rule of getNegativeModifierRules().exactConflicts) {
      const keys = Array.isArray(rule && rule.keys)
        ? rule.keys.map((key) => String(key || "").trim()).filter(Boolean)
        : [];
      if (keys.length >= 2 && keys.every((key) => selectedKeys.has(key))) {
        return String((rule && rule.message) || "该组合不可同时抽取。").trim();
      }
    }
    return "";
  }

  function findNegativeQuotaExceededTag(items) {
    const counts = countNegativeCoreTags(items);
    for (const [tag, rawLimit] of Object.entries(getNegativeModifierRules().quotaLimits)) {
      const limit = Number(rawLimit);
      if (Number.isFinite(limit) && limit >= 0 && (counts[tag] || 0) > limit) {
        return tag;
      }
    }
    return "";
  }

  function pickNextNegativeModifier() {
    const selectedKeys = new Set(getNegativeModifierKeys(state.modifiers.negative));
    const remaining = shuffleList(
      getNegativeModifierPool().filter((item) => !selectedKeys.has(resolveNegativeModifierKey(item)))
    );
    for (const candidate of remaining) {
      const nextSelection = [...state.modifiers.negative, candidate];
      if (findNegativeConflictMessage(nextSelection)) {
        continue;
      }
      if (findNegativeQuotaExceededTag(nextSelection)) {
        continue;
      }
      return candidate;
    }
    return null;
  }

  function totalModifierCount() {
    return state.modifierFeed.length;
  }

  function drawModifier(type) {
    if (totalModifierCount() >= MAX_MODIFIER_COUNT) {
      state.hasShownModifierCapNotice = true;
      state.warning = MODIFIER_CAP_NOTICE;
      renderModifiers();
      return;
    }
    if (type === "positive") {
      const pool = getPositiveModifierPool();
      if (!pool.length) {
        state.warning = "当前未导入正面词条规则";
        renderModifiers();
        return;
      }
      state.warning = "";
      const picked = randomPick(pool);
      state.modifiers.positive.push(picked);
      state.modifierFeed.push(buildModifierFeedEntry("positive", picked));
      renderModifiers();
      return;
    }

    const picked = pickNextNegativeModifier();
    if (!picked) {
      state.warning = getNegativeModifierPool().length
        ? "当前无可抽取的合法词条"
        : "当前未导入负面词条规则";
      renderModifiers();
      return;
    }

    state.warning = "";
    state.modifiers.negative.push(picked);
    state.modifierFeed.push(buildModifierFeedEntry("negative", picked));
    renderModifiers();
  }

  function syncModifierWarning() {
    if (state.warning && state.warning !== MODIFIER_CAP_NOTICE) {
      state.warning = "";
    }

    if (totalModifierCount() < MAX_MODIFIER_COUNT) {
      state.hasShownModifierCapNotice = false;
      if (state.warning === MODIFIER_CAP_NOTICE) {
        state.warning = "";
      }
    }

    if (!totalModifierCount() && state.warning && state.warning !== MODIFIER_CAP_NOTICE) {
      state.warning = "";
    }
  }

  function removeModifierById(id) {
    const index = state.modifierFeed.findIndex((item) => item.id === id);
    if (index < 0) {
      return;
    }

    const [removed] = state.modifierFeed.splice(index, 1);
    if (removed.type === "negative") {
      const keyToRemove = String(removed.key || "").trim();
      const negativeIndex = state.modifiers.negative.findIndex((item) => {
        if (keyToRemove) {
          return resolveNegativeModifierKey(item) === keyToRemove;
        }
        return resolveModifierTitle(item) === removed.displayTitle;
      });
      if (negativeIndex >= 0) {
        state.modifiers.negative.splice(negativeIndex, 1);
      }
    } else {
      const positiveIndex = state.modifiers.positive.findIndex((item) => {
        if (removed.key) {
          return item && item.key === removed.key;
        }
        return resolveModifierTitle(item) === removed.displayTitle;
      });
      if (positiveIndex >= 0) {
        state.modifiers.positive.splice(positiveIndex, 1);
      }
    }

    syncModifierWarning();
    hideModifierTooltip();
    renderModifiers();
  }

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function normalizeTalentDescription(item) {
    const description = String((item && item.description) || "").trim();
    return description ? description : "暂无详细介绍";
  }

  function resolveTalentDisplayName(item) {
    const zhName = String((item && item.talent_name_zh) || "").trim();
    const enName = String((item && item.talent_name_en) || "").trim();
    return zhName ? zhName : enName || "未命名天赋";
  }

  function resolveTalentMetaLine(detail, gridLabel) {
    const displayName = resolveTalentDisplayName(detail);
    const enName = String((detail && detail.talent_name_en) || "").trim();
    if (enName && enName !== displayName) {
      return enName;
    }
    return gridLabel ? `矩阵 ${gridLabel}` : "";
  }

  function getTooltipLayer() {
    return document.getElementById("talent-tooltip-layer");
  }

  function getModifierTooltipLayer() {
    return document.getElementById("modifier-tooltip-layer");
  }

  function hideModifierTooltip() {
    const layer = getModifierTooltipLayer();
    if (!layer) {
      return;
    }
    layer.hidden = true;
    layer.innerHTML = "";
    layer.style.left = "";
    layer.style.top = "";
    layer.dataset.placement = "";
    state.modifierTooltipTarget = null;
  }

  function hideTalentTooltip() {
    const layer = getTooltipLayer();
    if (!layer) {
      return;
    }
    layer.hidden = true;
    layer.innerHTML = "";
    layer.style.left = "";
    layer.style.top = "";
    layer.dataset.placement = "";
    state.tooltipTarget = null;
  }

  function positionTooltipLayer(layer, target) {
    if (!layer || !target || layer.hidden) {
      return;
    }

    const targetRect = target.getBoundingClientRect();
    const gap = 14;
    const viewportPadding = 12;

    layer.style.left = "0px";
    layer.style.top = "0px";
    layer.style.visibility = "hidden";

    const tooltipRect = layer.getBoundingClientRect();
    let top = targetRect.top - tooltipRect.height - gap;
    let placement = "top";

    if (top < viewportPadding) {
      top = targetRect.bottom + gap;
      placement = "bottom";
    }

    if (top + tooltipRect.height > window.innerHeight - viewportPadding) {
      top = Math.max(viewportPadding, targetRect.top - tooltipRect.height - gap);
      placement = "top";
    }

    let left = targetRect.left + (targetRect.width / 2) - (tooltipRect.width / 2);
    left = Math.max(viewportPadding, Math.min(left, window.innerWidth - tooltipRect.width - viewportPadding));

    layer.dataset.placement = placement;
    layer.style.left = `${left}px`;
    layer.style.top = `${top}px`;
    layer.style.visibility = "visible";
  }

  function positionTalentTooltip(cell) {
    positionTooltipLayer(getTooltipLayer(), cell);
  }

  function positionModifierTooltip(card) {
    positionTooltipLayer(getModifierTooltipLayer(), card);
  }

  function showTalentTooltip(cell) {
    const layer = getTooltipLayer();
    if (!layer || !cell || !cell.dataset.tooltipBody) {
      return;
    }

    layer.innerHTML = `
      <p class="talent-tooltip__kicker">${escapeHtml(cell.dataset.tooltipKicker || "")}</p>
      <h4>${escapeHtml(cell.dataset.tooltipTitle || "")}</h4>
      ${cell.dataset.tooltipMeta ? `<p class="talent-tooltip__meta">${escapeHtml(cell.dataset.tooltipMeta)}</p>` : ""}
      <p class="talent-tooltip__body">${escapeHtml(cell.dataset.tooltipBody || "")}</p>
    `;
    layer.hidden = false;
    state.tooltipTarget = cell;
    positionTalentTooltip(cell);
  }

  function showModifierTooltip(card) {
    const layer = getModifierTooltipLayer();
    if (!layer || !card || !card.dataset.tooltipBody) {
      return;
    }

    layer.innerHTML = `
      <p class="modifier-tooltip__kicker">${escapeHtml(card.dataset.tooltipKicker || "")}</p>
      <h4>${escapeHtml(card.dataset.tooltipTitle || "")}</h4>
      ${card.dataset.tooltipMeta ? `<p class="modifier-tooltip__meta">${escapeHtml(card.dataset.tooltipMeta)}</p>` : ""}
      <p class="modifier-tooltip__body">${escapeHtml(card.dataset.tooltipBody || "")}</p>
    `;
    layer.hidden = false;
    state.modifierTooltipTarget = card;
    positionModifierTooltip(card);
  }

  function bindTalentTooltipTargets() {
    const cells = document.querySelectorAll(".talent-cell[data-tooltip-body]");
    cells.forEach((cell) => {
      cell.addEventListener("mouseenter", () => showTalentTooltip(cell));
      cell.addEventListener("mouseleave", hideTalentTooltip);
      cell.addEventListener("focus", () => showTalentTooltip(cell));
      cell.addEventListener("blur", hideTalentTooltip);
    });
  }

  function bindModifierTooltipTargets() {
    const cards = document.querySelectorAll(".modifier-card[data-tooltip-body]");
    cards.forEach((card) => {
      card.addEventListener("mouseenter", () => showModifierTooltip(card));
      card.addEventListener("mouseleave", hideModifierTooltip);
      card.addEventListener("focus", () => showModifierTooltip(card));
      card.addEventListener("blur", hideModifierTooltip);
    });
  }

  function modifierTypeMeta(value) {
    if (!value || typeof value !== "object") {
      return "";
    }
    const riskLabel = {
      must_exclude: "必排组合",
      high_risk: "高风险",
      mid_high_risk: "中高风险",
      advisory: "提示项"
    };
    if (value.type === "positive") {
      return "正面词条";
    }
    const parts = ["负面词条"];
    if (value.risk_level && riskLabel[value.risk_level]) {
      parts.push(riskLabel[value.risk_level]);
    }
    return parts.join(" · ");
  }

  function resolveModifierTitle(value) {
    if (value && typeof value === "object") {
      const label = String((value.label || value.name || "")).trim();
      if (label) {
        return titleOfModifier(label) || "未命名因子";
      }
    }
    return titleOfModifier(value) || "未命名因子";
  }

  function resolveModifierDetail(value) {
    const detail = String((value && value.detail) || "").trim();
    if (detail) {
      return detail;
    }
    const label = modifierLabel(value);
    const parts = label.split("：");
    return parts.slice(1).join("：").trim() || label || "机魂未提供额外备注。";
  }

  function buildModifierFeedEntry(type, modifier) {
    const normalizedType = type === "negative" ? "negative" : "positive";
    const key = modifier && typeof modifier === "object" && modifier.key
      ? String(modifier.key).trim()
      : normalizedType === "negative"
        ? resolveNegativeModifierKey(modifier)
        : "";
    const displayTitle = resolveModifierTitle(modifier);
    const tooltipBody = resolveModifierDetail(modifier);
    return {
      id: ++modifierIdSeed,
      type: normalizedType,
      key,
      displayTitle,
      displaySummary: summarizeModifierBody(tooltipBody),
      tooltipTitle: displayTitle,
      tooltipMeta: modifierTypeMeta({ ...(modifier || {}), type: normalizedType }),
      tooltipBody
    };
  }

  function renderSquadTabs() {
    const root = document.getElementById("squad-tabs");
    root.innerHTML = state.team.map((player, index) => {
      const entry = state.classLookup[player.classSlug];
      const meta = entry ? entry.name : "待配置";
      const status = player.confirmed
        ? "已确认"
        : !player.classSlug
          ? "待抽职业"
          : !player.hasDrawnLoadout
            ? "待抽武器"
            : !player.hasDrawnTalents
              ? "待抽天赋"
              : "可保存";
      const activeClass = index === state.activeIndex ? "active" : "";
      const confirmedClass = player.confirmed ? "confirmed" : "";
      return `
        <button type="button" class="squad-tab ${activeClass} ${confirmedClass}" data-player-index="${index}">
          <span class="squad-tab__label">小队 ${index + 1}</span>
          <span class="squad-tab__meta">${escapeHtml(meta)}</span>
          <span class="squad-tab__status">${escapeHtml(status)}</span>
        </button>
      `;
    }).join("");
  }

  function renderHero() {
    const player = currentPlayer();
    const entry = currentClass();
    const image = document.getElementById("class-image");
    const placeholder = document.getElementById("class-image-placeholder");
    const imageSrc = resolveClassImage(entry);
    const hasImage = Boolean(imageSrc);

    image.src = imageSrc || "";
    image.style.display = hasImage ? "block" : "none";
    placeholder.style.display = hasImage ? "none" : "grid";

    document.getElementById("current-member-label").textContent = player.confirmed
      ? `小队 ${state.activeIndex + 1} · 已确认`
      : `小队 ${state.activeIndex + 1} · 当前成员`;
    document.getElementById("class-name").textContent = entry ? entry.name : "等待抽取";
    document.getElementById("class-role").textContent = entry ? (entry.role || "未定义角色") : "等待数据";
    document.getElementById("class-tagline").textContent = entry
      ? (entry.tagline || buildPlayerStatus(player))
      : "请选择一名成员后再抽取。";
    document.getElementById("save-progress-bar").style.width = `${Math.max((confirmedCount() / state.team.length) * 100, 4)}%`;
  }

  function renderWeaponGrid() {
    const player = currentPlayer();
    const entry = currentClass();
    const iconBySlot = {
      "主武器": "hardware",
      "副武器": "9mp",
      "近战武器": "swords"
    };

    const weaponSlots = [
      ["主武器", player.primary],
      ["副武器", player.secondary],
      ["近战武器", player.melee]
    ];

    document.getElementById("weapon-grid").innerHTML = weaponSlots.map(([label, item]) => {
      let title = "待抽取";
      let hint = "职业锁定后，需手动抽取该槽位配置。";

      if (!entry) {
        title = "等待职业锁定";
        hint = "先抽取职业，再分配军械库。";
      } else if (player.hasDrawnLoadout) {
        if (item) {
          title = item.name;
          hint = "机魂已批准当前军械分配。";
        } else {
          title = label === "主武器" ? "该职业无主武器" : "未分配";
          hint = "该槽位在当前职业配置中不提供可用武器。";
        }
      }

      const image = item ? resolveWeaponImage(item) : "";
      const cardClass = player.hasDrawnLoadout ? "weapon-card" : "weapon-card empty";
      return `
        <article class="${cardClass}">
          <div class="weapon-card__header">
            <span class="weapon-card__label">${escapeHtml(label)}</span>
          </div>
          <h4 class="weapon-card__name">${escapeHtml(title)}</h4>
          <div class="weapon-card__art">
            ${image
              ? `<img src="${escapeHtml(image)}" alt="${escapeHtml(title)}" />`
              : `<span class="material-symbols-outlined" aria-hidden="true">${escapeHtml(iconBySlot[label])}</span>`}
          </div>
          <p class="weapon-card__hint">${escapeHtml(hint)}</p>
        </article>
      `;
    }).join("");
  }

  function renderTalentCell(visualColumn, visualRow, item, isActive) {
    const gridLabel = `${visualRow}/${visualColumn}`;
    const image = item && item.icon_path ? `../assets/${item.icon_path}` : "";
    const displayName = item ? resolveTalentDisplayName(item) : "等待灌注";
    const metaLine = item ? resolveTalentMetaLine(item, gridLabel) : "";
    const description = item ? normalizeTalentDescription(item) : "";
    const cellClass = [isActive ? "active" : "", !item ? "placeholder" : ""].filter(Boolean).join(" ");
    const tooltipData = item ? `
      data-tooltip-kicker="${escapeHtml(gridLabel)}"
      data-tooltip-title="${escapeHtml(displayName)}"
      data-tooltip-meta="${escapeHtml(metaLine)}"
      data-tooltip-body="${escapeHtml(description)}"
    ` : "";

    return `
      <article
        class="talent-cell ${cellClass}"
        style="grid-column:${visualColumn};grid-row:${visualRow};"
        tabindex="${item ? "0" : "-1"}"
        ${tooltipData}
      >
        ${image
          ? `<img class="talent-cell__icon" src="${escapeHtml(image)}" alt="${escapeHtml(displayName)}" />`
          : `<div class="talent-cell__fallback">+</div>`}
      </article>
    `;
  }

  function renderTalents() {
    const player = currentPlayer();
    const activeSet = new Set((player.talents || []).map((item) => item.talent_slug));
    const entry = currentClass();
    const classSlug = entry ? entry.slug : "";
    const talentGroup = state.talents[classSlug] || { nodes: [] };
    const matrixLookup = new Map();

    hideTalentTooltip();

    (talentGroup.nodes || []).forEach((item) => {
      const visualColumn = Number(item.row || 0);
      const visualRow = Number(item.col || 0);
      const key = `${visualColumn}:${visualRow}`;
      matrixLookup.set(key, item);
    });

    const markup = [];
    for (let visualRow = 1; visualRow <= TALENT_VISUAL_ROWS; visualRow += 1) {
      for (let visualColumn = 1; visualColumn <= TALENT_VISUAL_COLUMNS; visualColumn += 1) {
        const key = `${visualColumn}:${visualRow}`;
        const item = matrixLookup.get(key) || null;
        markup.push(
          renderTalentCell(
            visualColumn,
            visualRow,
            item,
            Boolean(item && activeSet.has(item.talent_slug))
          )
        );
      }
    }

    document.getElementById("talent-grid").innerHTML = markup.join("");
    document.getElementById("talent-status").textContent = buildPlayerStatus(player);
    bindTalentTooltipTargets();
  }

  function renderModifierCards(items) {
    if (!items.length) {
      return `<div class="modifier-empty">当前还没有已抽取的战场词条。</div>`;
    }

    return items.map((item) => {
      const type = item.type === "negative" ? "negative" : "positive";
      const icon = type === "positive" ? "verified_user" : "dangerous";
      return `
        <article
          class="modifier-card ${type}"
          tabindex="0"
          data-tooltip-kicker="${escapeHtml(type === "positive" ? "正面策略词条" : "负面策略词条")}"
          data-tooltip-title="${escapeHtml(item.tooltipTitle)}"
          data-tooltip-meta="${escapeHtml(item.tooltipMeta)}"
          data-tooltip-body="${escapeHtml(item.tooltipBody)}"
        >
          <button type="button" class="modifier-card__remove" data-remove-modifier-id="${item.id}" aria-label="删除${escapeHtml(item.displayTitle)}">
            <span class="material-symbols-outlined" aria-hidden="true">close</span>
          </button>
          <span class="material-symbols-outlined modifier-card__icon" aria-hidden="true">${icon}</span>
          <div class="modifier-card__content">
            <p class="modifier-card__title">${escapeHtml(item.displayTitle)}</p>
            <p class="modifier-card__body">${escapeHtml(item.displaySummary)}</p>
          </div>
        </article>
      `;
    }).join("");
  }

  function renderModifiers() {
    hideModifierTooltip();
    document.getElementById("modifier-feed").innerHTML = renderModifierCards(state.modifierFeed);
    document.getElementById("positive-count").textContent = String(state.modifiers.positive.length);
    document.getElementById("negative-count").textContent = String(state.modifiers.negative.length);
    document.getElementById("modifier-warning").textContent = state.warning;
    bindModifierTooltipTargets();
  }

  function renderActionButtons() {
    const player = currentPlayer();
    const hasClass = Boolean(player.classSlug);
    const reachedModifierCap = totalModifierCount() >= MAX_MODIFIER_COUNT;
    document.getElementById("reset-current-button").disabled = !hasClass;
    document.getElementById("reroll-loadout-button").disabled = !hasClass;
    document.getElementById("reroll-talents-button").disabled = !hasClass;
    document.getElementById("draw-positive-button").disabled = reachedModifierCap;
    document.getElementById("draw-negative-button").disabled = reachedModifierCap;
  }

  function renderFooter() {
    const player = currentPlayer();
    const saveButton = document.getElementById("save-config-button");
    const readyToSave = player.classSlug && player.hasDrawnLoadout && player.hasDrawnTalents && !player.confirmed;

    let statusText = buildPlayerStatus(player);
    if (state.squadCompleted) {
      statusText = "整队配置已完成。可手动复核任意成员，或直接重置全部配置。";
    }
    document.getElementById("footer-status").textContent = statusText;

    if (state.squadCompleted) {
      saveButton.textContent = "整队已完成";
      saveButton.disabled = true;
      return;
    }

    if (!player.classSlug) {
      saveButton.textContent = "待抽职业";
      saveButton.disabled = true;
      return;
    }

    if (!player.hasDrawnLoadout) {
      saveButton.textContent = "待抽武器";
      saveButton.disabled = true;
      return;
    }

    if (!player.hasDrawnTalents) {
      saveButton.textContent = "待抽天赋";
      saveButton.disabled = true;
      return;
    }

    if (player.confirmed) {
      saveButton.textContent = "当前成员已确认";
      saveButton.disabled = true;
      return;
    }

    saveButton.disabled = !readyToSave;
    saveButton.textContent = state.activeIndex === state.team.length - 1
      ? "完成整队"
      : `保存并切换到小队 ${state.activeIndex + 2}`;
  }

  function render() {
    renderSquadTabs();
    renderHero();
    renderWeaponGrid();
    renderTalents();
    renderModifiers();
    renderActionButtons();
    renderFooter();
  }

  async function loadJson(url) {
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`${url}: ${response.status}`);
    }
    return response.json();
  }

  function bindEvents() {
    document.getElementById("draw-class-button").addEventListener("click", drawSingle);
    document.getElementById("reroll-loadout-button").addEventListener("click", rerollLoadout);
    document.getElementById("reroll-talents-button").addEventListener("click", rerollTalents);
    document.getElementById("draw-positive-button").addEventListener("click", () => drawModifier("positive"));
    document.getElementById("draw-negative-button").addEventListener("click", () => drawModifier("negative"));
    document.getElementById("reset-current-button").addEventListener("click", resetCurrentPlayer);
    document.getElementById("save-config-button").addEventListener("click", saveCurrentConfig);
    document.getElementById("reset-all-button").addEventListener("click", resetAll);
    document.getElementById("modifier-feed").addEventListener("click", (event) => {
      const button = event.target.closest("[data-remove-modifier-id]");
      if (!button) return;
      event.stopPropagation();
      removeModifierById(Number(button.getAttribute("data-remove-modifier-id")));
    });
    document.getElementById("squad-tabs").addEventListener("click", (event) => {
      const button = event.target.closest("[data-player-index]");
      if (!button) return;
      state.activeIndex = Number(button.getAttribute("data-player-index"));
      hideTalentTooltip();
      render();
    });
    window.addEventListener("resize", () => {
      if (state.tooltipTarget) {
        positionTalentTooltip(state.tooltipTarget);
      }
      if (state.modifierTooltipTarget) {
        positionModifierTooltip(state.modifierTooltipTarget);
      }
    });
    window.addEventListener("scroll", () => {
      if (state.tooltipTarget) {
        positionTalentTooltip(state.tooltipTarget);
      }
      if (state.modifierTooltipTarget) {
        positionModifierTooltip(state.modifierTooltipTarget);
      }
    }, true);
  }

  async function initialize() {
    const [classesPayload, talentsPayload, metaPayload] = await Promise.all([
      loadJson(DATA_FILES.classes),
      loadJson(DATA_FILES.talents),
      loadJson(DATA_FILES.meta)
    ]);
    state.classes = uniqueBySlug(classesPayload.classes || []);
    state.classLookup = Object.fromEntries(state.classes.map((entry) => [entry.slug, entry]));
    state.talents = Object.fromEntries((talentsPayload.classes || []).map((entry) => [entry.class_slug, entry]));
    state.meta = metaPayload || {};
    render();
  }

  bindEvents();
  initialize().catch((error) => {
    console.error(error);
    document.getElementById("footer-status").textContent = "初始化失败，请先运行 pipeline/transforms/build_runtime_data.py。";
  });
})();
