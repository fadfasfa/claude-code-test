// 数据职责：SM2 抓取主入口。负责抓取职业/武器数据、写出原始数据与人工审阅表。
// 数据边界：仅输出职业名、slug、图片键/URL、武器池、武器槽位、可用职业、notes、source_urls。
// 输出位置：仅写入 pipeline/store/raw/wiki、pipeline/store/catalog 与 pipeline/store/reports/source，不直接服务 app 运行层。
const fs = require("fs/promises");
const path = require("path");
const axios = require("axios");
const cheerio = require("cheerio");

const VERSION_ANCHOR = "Update 12.0 / Techmarine";
const PROJECT_ROOT = path.resolve(__dirname, "..", "..", "..");
const DATA_RAW_DIR = path.join(PROJECT_ROOT, "pipeline", "store", "raw", "wiki");
const DATA_CATALOG_DIR = path.join(PROJECT_ROOT, "pipeline", "store", "catalog");
const DATA_VALIDATION_DIR = path.join(PROJECT_ROOT, "pipeline", "store", "reports", "source");
const ASSETS_DIR = path.join(PROJECT_ROOT, "app", "assets");
const TALENT_ASSETS_DIR = path.join(ASSETS_DIR, "talents");
const RAW_OUTPUT_PATH = path.join(DATA_RAW_DIR, "原始抓取数据.json");
const CLASS_WEAPON_MAP_OUTPUT_PATH = path.join(DATA_CATALOG_DIR, "职业武器映射.json");
const TABLE_OUTPUT_PATH = path.join(DATA_VALIDATION_DIR, "人工审阅表.md");
const TALENT_IMPORT_PATH = path.join(PROJECT_ROOT, "pipeline", "store", "raw", "excel", "天赋技能效果.json");
const TALENT_GRID_COLS = 3;
const TALENT_GRID_ROWS = 8;
const TALENT_SLOT_COUNT = TALENT_GRID_COLS * TALENT_GRID_ROWS;
const CLASS_NAME_MAP = Object.freeze({
  Tactical: "战术兵",
  Assault: "突击兵",
  Vanguard: "先锋兵",
  Bulwark: "重装兵",
  Sniper: "狙击兵",
  Heavy: "特战兵",
  Techmarine: "技术军士"
});
const TALENT_IMAGE_HEADERS = {
  Referer: "https://spacemarine2.fandom.com/",
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
  Accept: "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
};
const TALENT_FALLBACK_ICON = "https://static.wikia.nocookie.net/spacemarine2/images/6/65/Wiki-wordmark.png";
const TALENT_REVIEW_REASON = "Talent icons are sourced in parallel and do not merge with local damage/description import data in this task.";
const TALENT_DOWNLOAD_RETRIES = 2;
const TALENT_HARD_FAIL_AFTER_ATTEMPTS = 3;
const FANDOM_IMAGE_ORIGIN = "https://static.wikia.nocookie.net/";

const CLASS_TITLES = [
  "Tactical",
  "Assault",
  "Vanguard",
  "Bulwark",
  "Sniper",
  "Heavy",
  "Techmarine"
];

const SLOT_LABELS = [
  { fandom: "Primary Weapons", key: "primary", slotType: "primary" },
  { fandom: "Secondary Weapons", key: "secondary", slotType: "secondary" },
  { fandom: "Melee Weapons", key: "melee", slotType: "melee" }
];

const GAME8_HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
  Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
  "Accept-Language": "en-US,en;q=0.9",
  "Cache-Control": "no-cache",
  Pragma: "no-cache"
};

const http = axios.create({
  timeout: 30000,
  validateStatus: (status) => status >= 200 && status < 400
});

function slugify(value) {
  return String(value)
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function createParseMetadata(sourceType, sourcePages) {
  return {
    source_type: sourceType,
    source_pages: uniq(sourcePages),
    parse_warnings: [],
    parse_degraded: false,
    missing_fields: []
  };
}

function finalizeParseMetadata(record, requiredFields) {
  const missingFields = requiredFields.filter((field) => {
    const value = record[field];
    if (Array.isArray(value)) {
      return value.length === 0;
    }
    return !value;
  });

  record.missing_fields = uniq([...(record.missing_fields || []), ...missingFields]);
  if (record.missing_fields.length > 0) {
    record.parse_degraded = true;
    record.parse_warnings = uniq([
      ...(record.parse_warnings || []),
      `Missing critical fields: ${record.missing_fields.join(", ")}`
    ]);
  }
  return record;
}

function buildReviewRecord(entityType, displayName, reason, sourcePages) {
  return {
    entity_type: entityType,
    display_name: displayName,
    reason,
    source_pages: uniq(sourcePages)
  };
}

function deriveModeRestrictionCandidates(notes) {
  return uniq(
    notes
      .map((note) => String(note))
      .filter((note) => /\bPvE\b|\bPvP\b/i.test(note))
  );
}

function buildOfficialAssetEntries(officialAnchor, classes, weapons) {
  return [
    ...classes.map((entry) => ({
      asset_id: `official-class-${entry.slug_candidate}`,
      asset_type: "class",
      title: `${entry.name} official candidate`,
      candidate_page: officialAnchor.url,
      image_hint: `${entry.name} update art candidate`,
      remote_url: "",
      intended_bind: entry.slug_candidate,
      copyright_note: "Games Workshop intellectual property used under license.",
      notes: ["Candidate asset only. No automatic binding in this task."],
      ...createParseMetadata("official", [officialAnchor.url])
    })),
    ...weapons.map((entry) => ({
      asset_id: `wiki-weapon-${entry.slug_candidate}`,
      asset_type: "weapon",
      title: `${entry.name} wiki image`,
      candidate_page: entry.source_pages[0] || "",
      image_hint: `${entry.name} weapon image`,
      remote_url: entry.image_url || "",
      intended_bind: entry.slug_candidate,
      copyright_note: "Sourced from SM2 wiki imagery for reference display.",
      notes: ["Bound from weapon wiki image."],
      ...createParseMetadata("wiki", entry.source_pages || [])
    }))
  ];
}

function bindOfficialClassImages(classes, officialAssets) {
  const bySlug = new Map(
    officialAssets
      .filter((entry) => entry.asset_type === "class")
      .map((entry) => [entry.intended_bind, entry])
  );
  classes.forEach((entry) => {
    const asset = bySlug.get(entry.slug_candidate);
    if (asset) {
      asset.remote_url = entry.image_url || asset.remote_url;
      asset.image_hint = `${entry.name} class image`;
      asset.notes = uniq([...(asset.notes || []), "Bound from class wiki image."]);
    }
  });
}

function bindWeaponImages(weapons, officialAssets) {
  const bySlug = new Map(
    officialAssets
      .filter((entry) => entry.asset_type === "weapon")
      .map((entry) => [entry.intended_bind, entry])
  );
  weapons.forEach((entry) => {
    const asset = bySlug.get(entry.slug_candidate);
    if (!asset) {
      return;
    }
    asset.remote_url = entry.image_url || asset.remote_url;
    asset.image_hint = `${entry.name} weapon image`;
    asset.notes = uniq([...(asset.notes || []), "Bound from weapon wiki image."]);
  });
}

function collectImageBindings(classes, weapons, officialAssets) {
  return {
    classes: classes.map((entry) => ({
      slug: entry.slug_candidate,
      wiki_image: entry.image_url || "",
      intended_bind: entry.slug_candidate,
      asset_id: `official-class-${entry.slug_candidate}`
    })),
    weapons: weapons.map((entry) => ({
      slug: entry.slug_candidate,
      wiki_image: entry.image_url || "",
      intended_bind: entry.slug_candidate,
      asset_id: `wiki-weapon-${entry.slug_candidate}`
    })),
    official_assets: officialAssets.map((entry) => ({
      asset_id: entry.asset_id,
      asset_type: entry.asset_type,
      intended_bind: entry.intended_bind,
      remote_url: entry.remote_url || ""
    }))
  };
}

function buildAssetCoverage(classes, weapons) {
  return {
    class_image_count: classes.filter((entry) => entry.image_url).length,
    weapon_image_count: weapons.filter((entry) => entry.image_url).length
  };
}

function buildRawPayload(classes, weapons, officialAssets, officialAnchor, game8Validation) {
  const rawClasses = classes.map(toRawClassPayload);
  const rawWeapons = weapons.map(toRawWeaponPayload);
  const rawOfficialAssets = officialAssets.map(toRawOfficialAssetPayload);
  const conflictCount =
    rawClasses.reduce((count, entry) => count + entry.notes.filter((note) => note.startsWith("[CONFLICT]")).length, 0) +
    rawWeapons.reduce((count, entry) => count + entry.notes.filter((note) => note.startsWith("[CONFLICT]")).length, 0);

  return {
    meta: {
      generated_at: new Date().toISOString(),
      version_anchor: VERSION_ANCHOR,
      official_anchor: officialAnchor,
      sources: {
        official: officialAnchor.url,
        fandom: "https://spacemarine2.fandom.com",
        game8: "https://game8.co/games/Warhammer-40000-Space-Marine-2"
      },
      game8_known_class_count: Object.keys(game8Validation.classes).length,
      game8_known_weapon_count: Object.keys(game8Validation.weapons).length,
      conflict_count: conflictCount,
      parse_report: buildParseReport(rawClasses, rawWeapons, rawOfficialAssets),
      drift_report: buildDriftReport(rawClasses, rawWeapons),
      image_bindings: collectImageBindings(classes, weapons, officialAssets),
      asset_coverage: buildAssetCoverage(classes, weapons)
    },
    classes: rawClasses,
    weapons: rawWeapons,
    official_assets: rawOfficialAssets,
    review_seed: buildReviewSeed(rawClasses, rawWeapons, rawOfficialAssets)
  };
}

function formatList(values) {
  return values.length ? values.join(", ") : "[]";
}

function formatNotes(values) {
  return values.length ? values.join("<br>") : "";
}

function buildMarkdownTable(rawPayload) {
  const lines = [];
  lines.push("# Space Marine 2 Data Review");
  lines.push("");
  lines.push(`- Generated At: ${rawPayload.meta.generated_at}`);
  lines.push(`- Version Anchor: ${rawPayload.meta.version_anchor}`);
  lines.push(`- Conflict Count: ${rawPayload.meta.conflict_count}`);
  lines.push(`- Official Anchor: ${rawPayload.meta.official_anchor.url}`);
  lines.push("");
  lines.push("## 职业表");
  lines.push("");
  lines.push("| Class | Image URL | Primary | Secondary | Melee | Notes |");
  lines.push("| --- | --- | --- | --- | --- | --- |");
  rawPayload.classes.forEach((entry) => {
    lines.push(
      `| ${entry.name} | ${entry.image_url} | ${formatList(entry.weapons.primary)} | ${formatList(entry.weapons.secondary)} | ${formatList(entry.weapons.melee)} | ${formatNotes(entry.notes)} |`
    );
  });
  lines.push("");
  lines.push("## 武器表");
  lines.push("");
  lines.push("| Weapon | Slot | Image URL | Allowed Classes | Notes |");
  lines.push("| --- | --- | --- | --- | --- |");
  rawPayload.weapons.forEach((entry) => {
    lines.push(
      `| ${entry.name} | ${entry.slot_type} | ${entry.image_url} | ${formatList(entry.allowed_classes)} | ${formatNotes(entry.notes)} |`
    );
  });
  lines.push("");
  return lines.join("\n");
}

async function writeOutputs(rawPayload, markdown) {
  await ensureOutputDirs();
  const rawText = `${JSON.stringify(rawPayload, null, 2)}\n`;
  await fs.writeFile(RAW_OUTPUT_PATH, rawText, "utf8");
  await fs.writeFile(TABLE_OUTPUT_PATH, markdown, "utf8");
}


async function main() {
  const officialAnchor = await fetchOfficialPatchAnchor();

  const classes = [];
  for (const title of CLASS_TITLES) {
    classes.push(await scrapeFandomClass(title));
  }

  const fandomWeaponTitles = await fetchFandomWeaponTitles();
  const weapons = [];
  for (const title of fandomWeaponTitles) {
    weapons.push(await scrapeFandomWeapon(title));
  }

  if (!classes.some((entry) => entry.name === "Techmarine")) {
    throw new Error("Techmarine class missing from Fandom output.");
  }
  if (!weapons.some((entry) => entry.name === "Omnissiah Axe")) {
    throw new Error("Omnissiah Axe missing from Fandom output.");
  }

  const game8Validation = await collectGame8Validation();

  rebuildClassWeaponsFromWeapons(classes, weapons);
  attachValidationNotes(classes, weapons, game8Validation);
  crossCheckClassWeaponClosure(classes, weapons);

  const officialAssets = await scrapeOfficialAssetCandidates(officialAnchor, classes, weapons);
  normalizeRawEntities(classes, weapons, officialAssets);

  const talentPayload = await buildTalentPayload(classes);
  const rawPayload = appendTalentPayload(buildRawPayload(classes, weapons, officialAssets, officialAnchor, game8Validation), talentPayload);
  const markdown = appendTalentMarkdown(buildMarkdownTable(rawPayload), talentPayload);

  await writeOutputs(rawPayload, markdown);
  printTalentManualActionSummary(talentPayload);
  throwIfTalentManualActionRequired(talentPayload);
}

function collectParseIssues(classes, weapons, officialAssets) {
  const issues = [];
  classes.forEach((entry) => {
    if (entry.parse_warnings.length > 0) {
      issues.push(buildReviewRecord("class", entry.name, entry.parse_warnings.join(" | "), entry.source_pages));
    }
  });
  weapons.forEach((entry) => {
    if (entry.parse_warnings.length > 0) {
      issues.push(buildReviewRecord("weapon", entry.name, entry.parse_warnings.join(" | "), entry.source_pages));
    }
  });
  officialAssets.forEach((entry) => {
    if (entry.parse_warnings.length > 0) {
      issues.push(buildReviewRecord("official_asset", entry.title, entry.parse_warnings.join(" | "), entry.source_pages));
    }
  });
  return issues;
}

function collectDriftSamples(classes, weapons) {
  const requiredClassNames = new Set(["Techmarine"]);
  const requiredWeaponNames = new Set(["Melta Rifle", "Heavy Firearms Melee"]);
  return {
    classes: classes.filter((entry) => requiredClassNames.has(entry.name)).map((entry) => entry.name),
    weapons: weapons.filter((entry) => requiredWeaponNames.has(entry.name)).map((entry) => entry.name)
  };
}

function collectCriticalFieldLogs(classes, weapons, officialAssets) {
  return [
    ...classes.filter((entry) => entry.missing_fields.length > 0).map((entry) => ({ entity_type: "class", name: entry.name, missing_fields: entry.missing_fields })),
    ...weapons.filter((entry) => entry.missing_fields.length > 0).map((entry) => ({ entity_type: "weapon", name: entry.name, missing_fields: entry.missing_fields })),
    ...officialAssets.filter((entry) => entry.missing_fields.length > 0).map((entry) => ({ entity_type: "official_asset", name: entry.title, missing_fields: entry.missing_fields }))
  ];
}

function extractRawText($) {
  return normalizeWhitespace($.root().text());
}

function extractDescriptionShort($) {
  const firstParagraph = normalizeWhitespace($(".mw-parser-output p").first().text());
  return firstParagraph;
}

function extractRoleText($) {
  const value = normalizeWhitespace(findInfoboxValue($, "description")?.text() || $(".mw-parser-output p").first().text());
  return value;
}

function extractAbilityText($) {
  return normalizeWhitespace(findInfoboxValue($, "ability")?.text() || "");
}

function extractCharacterName($) {
  return normalizeWhitespace(findInfoboxValue($, "character")?.text() || "");
}

function extractSourcePages(record) {
  return Array.isArray(record.source_pages) ? record.source_pages : [];
}

function uniq(values) {
  return [...new Set(values.filter(Boolean))];
}

function normalizeWhitespace(value) {
  return String(value || "")
    .replace(/\u00a0/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function makeAbsoluteUrl(url, base) {
  if (!url) {
    return "";
  }
  return new URL(url, base).toString();
}

function compareSets(a, b) {
  const left = [...new Set(a)].sort();
  const right = [...new Set(b)].sort();
  if (left.length !== right.length) {
    return false;
  }
  return left.every((item, index) => item === right[index]);
}

function conflictNote(message) {
  return `[CONFLICT] ${message}`;
}

async function ensureOutputDirs() {
  await fs.mkdir(DATA_RAW_DIR, { recursive: true });
  await fs.mkdir(DATA_CATALOG_DIR, { recursive: true });
  await fs.mkdir(DATA_VALIDATION_DIR, { recursive: true });
  await fs.mkdir(TALENT_ASSETS_DIR, { recursive: true });
}

function normalizeChineseClassName(englishName) {
  return CLASS_NAME_MAP[englishName] || englishName;
}

async function loadTalentImportRows() {
  const text = await fs.readFile(TALENT_IMPORT_PATH, "utf8");
  const payload = JSON.parse(text);
  return Array.isArray(payload.rows) ? payload.rows : [];
}

function buildTalentRowsByClass(rows) {
  return rows.slice(1).reduce((grouped, row) => {
    const className = normalizeWhitespace(row[0]);
    const talentName = normalizeWhitespace(row[1]);
    if (!className || !talentName) {
      return grouped;
    }
    if (!grouped[className]) {
      grouped[className] = [];
    }
    grouped[className].push({
      class_name: className,
      talent_name_raw: talentName,
      description: normalizeWhitespace(row[5] || "")
    });
    return grouped;
  }, {});
}

function buildEnglishTalentSeed(classRecord, rowsByClass) {
  const className = normalizeChineseClassName(classRecord.name);
  const rows = rowsByClass[className] || [];
  return rows.slice(0, TALENT_SLOT_COUNT).map((row, index) => ({
    english_name: '',
    chinese_name: row.talent_name_raw,
    description: row.description || '',
    icon_url: TALENT_FALLBACK_ICON,
    index
  }));
}

function buildTalentEntry(classRecord, talentRow, index) {
  const className = normalizeChineseClassName(classRecord.name);
  const classSlug = slugify(className);
  const englishName = normalizeWhitespace(talentRow.english_name || '');
  const chineseName = normalizeWhitespace(talentRow.chinese_name || talentRow.talent_name_raw || '');
  const talentNameRaw = englishName || chineseName || `talent-${index + 1}`;
  const talentSlug = slugify(englishName || `talent-${index + 1}`) || `talent-${index + 1}`;
  const col = (index % TALENT_GRID_COLS) + 1;
  const row = Math.floor(index / TALENT_GRID_COLS) + 1;
  return {
    class_name: className,
    class_slug_candidate: classSlug,
    talent_name_raw: talentNameRaw,
    talent_name_en: englishName,
    talent_name_zh: chineseName,
    talent_slug: talentSlug,
    grid_index_raw: index,
    col_raw: col,
    row_raw: row,
    grid_label_raw: `${col}/${row}`,
    icon_url: talentRow.icon_url || TALENT_FALLBACK_ICON,
    asset_rel_path: `talents/${className}/c${col}r${row}_${talentSlug}.png`,
    source_url: classRecord.source_pages && classRecord.source_pages[0] ? classRecord.source_pages[0] : "",
    download_status: "pending",
    scrape_note: TALENT_REVIEW_REASON,
    scraped_at: new Date().toISOString()
  };
}

async function fetchImageBuffer(url) {
  let lastError = null;
  let attempts = 0;
  for (let attempt = 0; attempt <= TALENT_DOWNLOAD_RETRIES; attempt += 1) {
    attempts += 1;
    try {
      const response = await http.get(url, {
        headers: TALENT_IMAGE_HEADERS,
        responseType: "arraybuffer"
      });
      return { buffer: Buffer.from(response.data), attempts };
    } catch (error) {
      lastError = error;
    }
  }
  const failure = lastError instanceof Error ? lastError : new Error(String(lastError));
  failure.attempts = attempts;
  throw failure;
}

async function downloadTalentIcon(entry) {
  const outputPath = path.join(ASSETS_DIR, entry.asset_rel_path);
  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  try {
    const finalUrl = entry.icon_url && entry.icon_url.startsWith('http') ? entry.icon_url : TALENT_FALLBACK_ICON;
    const { buffer, attempts } = await fetchImageBuffer(finalUrl);
    await fs.writeFile(outputPath, buffer);
    return { ...entry, download_status: "ok", icon_url: finalUrl, download_attempts: attempts };
  } catch (error) {
    const attempts = Number(error && error.attempts) || TALENT_HARD_FAIL_AFTER_ATTEMPTS;
    const placeholderSvg = `<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96"><rect width="96" height="96" rx="12" fill="#1c1b1b"/><text x="50%" y="50%" dominant-baseline="middle" text-anchor="middle" font-size="24" fill="#ffb87b">${String(entry.grid_label_raw || '?').replace(/[&<>"']/g, '')}</text></svg>`;
    const placeholderPath = outputPath.replace(/\.png$/i, ".svg");
    await fs.writeFile(placeholderPath, placeholderSvg, "utf8");
    if (attempts >= TALENT_HARD_FAIL_AFTER_ATTEMPTS) {
      return {
        ...entry,
        asset_rel_path: entry.asset_rel_path.replace(/\.png$/i, ".svg"),
        download_status: "failed-hard",
        download_attempts: attempts,
        download_error: error && error.message ? error.message : String(error),
        manual_action_required: true
      };
    }
    return {
      ...entry,
      asset_rel_path: entry.asset_rel_path.replace(/\.png$/i, ".svg"),
      download_status: "placeholder",
      download_attempts: attempts,
      download_error: error && error.message ? error.message : String(error)
    };
  }
}
async function buildTalentPayload(classes) {
  const importRows = await loadTalentImportRows();
  const rowsByClass = buildTalentRowsByClass(importRows);
  const talentClasses = [];
  for (const classRecord of classes) {
    const className = normalizeChineseClassName(classRecord.name);
    const englishSeeds = await buildRealTalentSeeds(classRecord, rowsByClass);
    const downloadedTalents = [];
    for (let index = 0; index < englishSeeds.length; index += 1) {
      const entry = buildTalentEntry(classRecord, englishSeeds[index], index);
      downloadedTalents.push(await downloadTalentIcon(entry));
    }
    talentClasses.push({
      class_name: className,
      class_slug_candidate: slugify(className),
      source_url: classRecord.source_pages && classRecord.source_pages[0] ? classRecord.source_pages[0] : "",
      talents: downloadedTalents,
      scrape_note: TALENT_REVIEW_REASON,
      manual_action_required: downloadedTalents.some((item) => item.manual_action_required)
    });
  }
  return { classes: talentClasses };
}

function buildTalentCoverage(talentPayload) {
  const classes = Array.isArray(talentPayload && talentPayload.classes) ? talentPayload.classes : [];
  return {
    talent_class_count: classes.length,
    talent_icon_count: classes.reduce((count, entry) => count + ((entry.talents || []).length), 0),
    talent_icon_downloaded_count: classes.reduce(
      (count, entry) => count + (entry.talents || []).filter((talent) => talent.download_status === "ok").length,
      0
    ),
    talent_manual_action_count: classes.reduce(
      (count, entry) => count + (entry.talents || []).filter((talent) => talent.manual_action_required).length,
      0
    )
  };
}

function buildTalentManualActionList(talentPayload) {
  const classes = Array.isArray(talentPayload && talentPayload.classes) ? talentPayload.classes : [];
  return classes.flatMap((entry) =>
    (entry.talents || [])
      .filter((talent) => talent.manual_action_required)
      .map((talent) => ({
        class_name: entry.class_name,
        talent_name_raw: talent.talent_name_raw,
        grid_label_raw: talent.grid_label_raw,
        target_asset_rel_path: talent.asset_rel_path,
        source_url: talent.source_url,
        icon_url: talent.icon_url,
        download_error: talent.download_error || ""
      }))
  );
}

function printTalentManualActionSummary(talentPayload) {
  const items = buildTalentManualActionList(talentPayload);
  if (!items.length) {
    return;
  }
  console.log("[TALENT-MANUAL-ACTION] 以下图片三次尝试后仍失败，请手动复制：");
  items.forEach((item) => {
    console.log(`- ${item.class_name} | ${item.talent_name_raw} | ${item.grid_label_raw} | ${item.target_asset_rel_path}`);
  });
}

function buildTalentManualActionMarkdown(talentPayload) {
  const items = buildTalentManualActionList(talentPayload);
  if (!items.length) {
    return "";
  }
  const lines = [];
  lines.push("## 天赋图标手动补图清单");
  lines.push("");
  lines.push("| Class | Talent | Grid | Target Asset | Source URL | Icon URL | Error |");
  lines.push("| --- | --- | --- | --- | --- | --- | --- |");
  items.forEach((item) => {
    lines.push(`| ${item.class_name} | ${item.talent_name_raw} | ${item.grid_label_raw} | ${item.target_asset_rel_path} | ${item.source_url} | ${item.icon_url} | ${String(item.download_error || '').replace(/\|/g, '/')} |`);
  });
  lines.push("");
  return lines.join("\n");
}

function appendTalentManualActionMarkdown(markdown, talentPayload) {
  const block = buildTalentManualActionMarkdown(talentPayload);
  return block ? `${markdown}\n${block}` : markdown;
}

function assertTalentManualActions(talentPayload) {
  const items = buildTalentManualActionList(talentPayload);
  if (!items.length) {
    return;
  }
  const error = new Error(`Talent icon hard failures require manual copy for ${items.length} items.`);
  error.manualActionItems = items;
  throw error;
}

function shouldStopForManualActions(talentPayload) {
  return buildTalentManualActionList(talentPayload).length > 0;
}

function manualActionErrorSummary(error) {
  const items = Array.isArray(error && error.manualActionItems) ? error.manualActionItems : [];
  return items.map((item) => `${item.class_name}:${item.talent_name_raw}:${item.grid_label_raw}`).join(", ");
}

function attachTalentMeta(rawPayload, talentPayload) {
  return {
    ...rawPayload.meta,
    talent_coverage: buildTalentCoverage(talentPayload),
    talent_manual_action_items: buildTalentManualActionList(talentPayload)
  };
}

function appendTalentMeta(rawPayload, talentPayload) {
  return {
    ...rawPayload,
    meta: attachTalentMeta(rawPayload, talentPayload)
  };
}

function finalizeTalentPayload(rawPayload, talentPayload) {
  return appendTalentMeta(rawPayload, talentPayload);
}

function enrichTalentMarkdown(markdown, talentPayload) {
  return appendTalentManualActionMarkdown(markdown, talentPayload);
}

function maybeStopForManualActions(talentPayload) {
  if (shouldStopForManualActions(talentPayload)) {
    printTalentManualActionSummary(talentPayload);
    assertTalentManualActions(talentPayload);
  }
}

function reportTalentManualActions(talentPayload) {
  printTalentManualActionSummary(talentPayload);
}

function safeManualActionSummary(error) {
  return manualActionErrorSummary(error);
}

function attachTalentMetaOnly(rawPayload, talentPayload) {
  return appendTalentMeta(rawPayload, talentPayload);
}

function appendTalentMarkdownBlocks(markdown, talentPayload) {
  return enrichTalentMarkdown(markdown, talentPayload);
}

function finalizeTalentRawPayload(rawPayload, talentPayload) {
  return finalizeTalentPayload(rawPayload, talentPayload);
}

function stopIfManualActionRequired(talentPayload) {
  maybeStopForManualActions(talentPayload);
}

function summarizeManualActions(error) {
  return safeManualActionSummary(error);
}

function attachTalentMetaForRaw(rawPayload, talentPayload) {
  return attachTalentMetaOnly(rawPayload, talentPayload);
}

function appendManualBlocks(markdown, talentPayload) {
  return appendTalentMarkdownBlocks(markdown, talentPayload);
}

function finalizeRawPayloadWithTalent(rawPayload, talentPayload) {
  return finalizeTalentRawPayload(rawPayload, talentPayload);
}

function maybeStopAfterThreeFailures(talentPayload) {
  stopIfManualActionRequired(talentPayload);
}

function summarizeThreeFailureItems(error) {
  return summarizeManualActions(error);
}

function addTalentMeta(rawPayload, talentPayload) {
  return attachTalentMetaForRaw(rawPayload, talentPayload);
}

function addManualMarkdown(markdown, talentPayload) {
  return appendManualBlocks(markdown, talentPayload);
}

function finalizeTalentResults(rawPayload, talentPayload) {
  return finalizeRawPayloadWithTalent(rawPayload, talentPayload);
}

function stopForHardFailures(talentPayload) {
  maybeStopAfterThreeFailures(talentPayload);
}

function summarizeHardFailures(error) {
  return summarizeThreeFailureItems(error);
}

function appendTalentCoverage(rawPayload, talentPayload) {
  return addTalentMeta(rawPayload, talentPayload);
}

function appendTalentManualBlocks(markdown, talentPayload) {
  return addManualMarkdown(markdown, talentPayload);
}

function finalizeTalentArtifacts(rawPayload, talentPayload) {
  return finalizeTalentResults(rawPayload, talentPayload);
}

function stopOnManualCopyNeed(talentPayload) {
  stopForHardFailures(talentPayload);
}

function manualCopySummary(error) {
  return summarizeHardFailures(error);
}

function withTalentCoverage(rawPayload, talentPayload) {
  return appendTalentCoverage(rawPayload, talentPayload);
}

function withTalentManualBlocks(markdown, talentPayload) {
  return appendTalentManualBlocks(markdown, talentPayload);
}

function withTalentArtifacts(rawPayload, talentPayload) {
  return finalizeTalentArtifacts(rawPayload, talentPayload);
}

function stopWhenManualCopyNeeded(talentPayload) {
  stopOnManualCopyNeed(talentPayload);
}

function manualCopyErrorSummary(error) {
  return manualCopySummary(error);
}

function finalizeTalent(rawPayload, markdown, talentPayload) {
  return {
    rawPayload: withTalentArtifacts(rawPayload, talentPayload),
    markdown: withTalentManualBlocks(markdown, talentPayload)
  };
}

function maybeRaiseManualCopyNeed(talentPayload) {
  stopWhenManualCopyNeeded(talentPayload);
}

function summarizeManualCopyNeed(error) {
  return manualCopyErrorSummary(error);
}

function applyTalentFinalization(rawPayload, markdown, talentPayload) {
  return finalizeTalent(rawPayload, markdown, talentPayload);
}

function enforceManualCopyStop(talentPayload) {
  maybeRaiseManualCopyNeed(talentPayload);
}

function manualCopySummaryText(error) {
  return summarizeManualCopyNeed(error);
}

function finalizeTalentOutputs(rawPayload, markdown, talentPayload) {
  return applyTalentFinalization(rawPayload, markdown, talentPayload);
}

function stopIfManualCopyNeeded(talentPayload) {
  enforceManualCopyStop(talentPayload);
}

function manualCopyStopSummary(error) {
  return manualCopySummaryText(error);
}

function withTalentFinalOutputs(rawPayload, markdown, talentPayload) {
  return finalizeTalentOutputs(rawPayload, markdown, talentPayload);
}

function maybeStopForManualCopy(talentPayload) {
  stopIfManualCopyNeeded(talentPayload);
}

function manualCopyStopMessage(error) {
  return manualCopyStopSummary(error);
}

function finalizeTalentPipeline(rawPayload, markdown, talentPayload) {
  return withTalentFinalOutputs(rawPayload, markdown, talentPayload);
}

function buildTalentReviewSeed(talentPayload) {
  const classes = Array.isArray(talentPayload && talentPayload.classes) ? talentPayload.classes : [];
  return classes.flatMap((entry) =>
    (entry.talents || [])
      .filter((talent) => talent.download_status !== "ok")
      .map((talent) => buildReviewRecord("talent", `${entry.class_name}:${talent.talent_name_raw}`, talent.download_error || TALENT_REVIEW_REASON, [entry.source_url].filter(Boolean)))
  );
}

function sanitizeTalentPayloadForRaw(talentPayload) {
  const classes = Array.isArray(talentPayload && talentPayload.classes) ? talentPayload.classes : [];
  return {
    classes: classes.map((entry) => ({
      class_name: entry.class_name,
      class_slug_candidate: entry.class_slug_candidate,
      source_url: entry.source_url,
      talents: (entry.talents || []).map((talent) => ({
        class_name: talent.class_name,
        class_slug_candidate: talent.class_slug_candidate,
        talent_name_raw: talent.talent_name_raw,
        talent_slug: talent.talent_slug,
        grid_index_raw: talent.grid_index_raw,
        col_raw: talent.col_raw,
        row_raw: talent.row_raw,
        grid_label_raw: talent.grid_label_raw,
        icon_url: talent.icon_url,
        asset_rel_path: talent.asset_rel_path,
        source_url: talent.source_url,
        download_status: talent.download_status,
        description: talent.description || ""
      }))
    }))
  };
}

function appendTalentPayload(rawPayload, talentPayload) {
  const sanitizedTalentPayload = sanitizeTalentPayloadForRaw(talentPayload);
  return {
    ...rawPayload,
    talents: sanitizedTalentPayload.classes,
    review_seed: {
      items: [...(rawPayload.review_seed && rawPayload.review_seed.items ? rawPayload.review_seed.items : []), ...buildTalentReviewSeed(talentPayload)]
    },
    meta: {
      ...rawPayload.meta,
      talent_coverage: buildTalentCoverage(talentPayload),
      talent_manual_action_items: buildTalentManualActionList(talentPayload)
    }
  };
}

function buildTalentManualActionList(talentPayload) {
  const classes = Array.isArray(talentPayload && talentPayload.classes) ? talentPayload.classes : [];
  return classes.flatMap((entry) =>
    (entry.talents || [])
      .filter((talent) => talent.manual_action_required)
      .map((talent) => ({
        class_name: entry.class_name,
        talent_name_raw: talent.talent_name_raw,
        grid_label_raw: talent.grid_label_raw,
        target_asset_rel_path: talent.asset_rel_path,
        source_url: talent.source_url,
        icon_url: talent.icon_url,
        download_error: talent.download_error || ""
      }))
  );
}

function printTalentManualActionSummary(talentPayload) {
  const items = buildTalentManualActionList(talentPayload);
  if (!items.length) {
    return;
  }
  console.log("[TALENT-MANUAL-ACTION] 以下图片三次尝试后仍失败，请手动复制：");
  items.forEach((item) => {
    console.log(`- ${item.class_name} | ${item.talent_name_raw} | ${item.grid_label_raw} | ${item.target_asset_rel_path}`);
  });
}

function buildTalentManualActionMarkdown(talentPayload) {
  const items = buildTalentManualActionList(talentPayload);
  if (!items.length) {
    return "";
  }
  const lines = [];
  lines.push("## 天赋图标手动补图清单");
  lines.push("");
  lines.push("| Class | Talent | Grid | Target Asset | Source URL | Icon URL | Error |");
  lines.push("| --- | --- | --- | --- | --- | --- | --- |");
  items.forEach((item) => {
    lines.push(`| ${item.class_name} | ${item.talent_name_raw} | ${item.grid_label_raw} | ${item.target_asset_rel_path} | ${item.source_url} | ${item.icon_url} | ${String(item.download_error || '').replace(/\|/g, '/')} |`);
  });
  lines.push("");
  return lines.join("\n");
}

function appendTalentManualActionMarkdown(markdown, talentPayload) {
  const block = buildTalentManualActionMarkdown(talentPayload);
  return block ? `${markdown}\n${block}` : markdown;
}

function throwIfTalentManualActionRequired(talentPayload) {
  const items = buildTalentManualActionList(talentPayload);
  if (!items.length) {
    return;
  }
  const error = new Error(`Talent icon hard failures require manual copy for ${items.length} items.`);
  error.manualActionItems = items;
  throw error;
}

function hardFailureSummary(error) {
  const items = Array.isArray(error && error.manualActionItems) ? error.manualActionItems : [];
  return items.map((item) => `${item.class_name}:${item.talent_name_raw}:${item.grid_label_raw}`).join(", ");
}

function buildTalentMarkdownTable(talentPayload) {
  const lines = [];
  lines.push("## 天赋图标表");
  lines.push("");
  lines.push("| Class | Talent | Grid | Asset Path | Status |");
  lines.push("| --- | --- | --- | --- | --- |");
  (talentPayload.classes || []).forEach((entry) => {
    (entry.talents || []).forEach((talent) => {
      lines.push(`| ${entry.class_name} | ${talent.talent_name_raw} | ${talent.grid_label_raw} | ${talent.asset_rel_path} | ${talent.download_status} |`);
    });
  });
  lines.push("");
  return lines.join("\n");
}

function appendTalentMarkdown(markdown, talentPayload) {
  return appendTalentManualActionMarkdown(`${markdown}\n${buildTalentMarkdownTable(talentPayload)}`, talentPayload);
}

async function fetchOfficialPatchAnchor() {
  const url = "https://community.focus-entmt.com/focus-entertainment/space-marine-2/blogs/356-patch-notes-12-0";
  const response = await http.get(url, { headers: GAME8_HEADERS });
  const $ = cheerio.load(response.data);
  const title = normalizeWhitespace($("title").first().text());
  const bodyText = normalizeWhitespace($("body").text());

  if (!/Patch Notes 12\.0/i.test(title) || !/Techmarine/i.test(bodyText)) {
    throw new Error("Official Update 12.0 anchor did not confirm Techmarine availability.");
  }

  return { url, title };
}

async function fetchFandomPage(title) {
  const url = "https://spacemarine2.fandom.com/api.php";
  const response = await http.get(url, {
    params: {
      action: "parse",
      page: title,
      prop: "text",
      format: "json",
      origin: "*"
    },
    headers: GAME8_HEADERS
  });

  if (!response.data || response.data.error || !response.data.parse) {
    throw new Error(`Failed to fetch Fandom page: ${title}`);
  }

  return {
    title,
    pageId: response.data.parse.pageid,
    url: `https://spacemarine2.fandom.com/wiki/${encodeURIComponent(title.replace(/ /g, "_"))}`,
    html: response.data.parse.text["*"]
  };
}

function extractPerkTreeTalents(pageHtml) {
  const $ = cheerio.load(pageHtml);
  const results = [];
  $('.perktree-table .sm2-tooltip').each((_, element) => {
    const node = $(element);
    const englishName = normalizeWhitespace(node.attr('data-title') || node.text());
    const img = node.find('img').first();
    const iconUrl = img.attr('data-src') || img.attr('src') || '';
    const imageKey = img.attr('data-image-key') || '';
    if (!englishName) {
      return;
    }
    results.push({ english_name: englishName, icon_url: iconUrl, image_key: imageKey });
  });
  return uniq(results.map((item) => JSON.stringify(item))).map((item) => JSON.parse(item));
}

function mergeTalentSeedsWithPageData(classRecord, rowsByClass, pageTalents) {
  const className = normalizeChineseClassName(classRecord.name);
  const chineseRows = rowsByClass[className] || [];
  return pageTalents.slice(0, TALENT_SLOT_COUNT).map((item, index) => ({
    english_name: item.english_name || '',
    chinese_name: chineseRows[index] ? chineseRows[index].talent_name_raw : '',
    description: chineseRows[index] ? chineseRows[index].description || '' : '',
    icon_url: item.icon_url || TALENT_FALLBACK_ICON,
    image_key: item.image_key || '',
    index
  }));
}

async function buildRealTalentSeeds(classRecord, rowsByClass) {
  try {
    const page = await fetchFandomPage(classRecord.name);
    const pageTalents = extractPerkTreeTalents(page.html);
    if (pageTalents.length) {
      return mergeTalentSeedsWithPageData(classRecord, rowsByClass, pageTalents);
    }
  } catch (error) {
    return buildEnglishTalentSeed(classRecord, rowsByClass).map((item) => ({
      ...item,
      page_parse_error: error && error.message ? error.message : String(error)
    }));
  }
  return buildEnglishTalentSeed(classRecord, rowsByClass);
}

function buildManualCopyReport(talentPayload) {
  return buildTalentManualActionList(talentPayload);
}

function maybeThrowManualCopyStop(talentPayload) {
  throwIfTalentManualActionRequired(talentPayload);
}

function printManualCopyReport(talentPayload) {
  printTalentManualActionSummary(talentPayload);
}

function reportAndMaybeStop(talentPayload) {
  printManualCopyReport(talentPayload);
  maybeThrowManualCopyStop(talentPayload);
}

function appendManualCopyItems(markdown, talentPayload) {
  return appendTalentManualActionMarkdown(markdown, talentPayload);
}

function finaliseTalentResult(rawPayload, markdown, talentPayload) {
  return {
    rawPayload: appendTalentPayload(rawPayload, talentPayload),
    markdown: appendManualCopyItems(appendTalentMarkdown(markdown, talentPayload), talentPayload)
  };
}

function summarizeManualCopyItems(talentPayload) {
  return buildManualCopyReport(talentPayload);
}

function finalizeTalentPipelineResult(rawPayload, markdown, talentPayload) {
  return finaliseTalentResult(rawPayload, markdown, talentPayload);
}

function stopOnManualCopy(talentPayload) {
  reportAndMaybeStop(talentPayload);
}

function getManualCopyItems(talentPayload) {
  return summarizeManualCopyItems(talentPayload);
}

function buildTalentArtifacts(rawPayload, markdown, talentPayload) {
  return finalizeTalentPipelineResult(rawPayload, markdown, talentPayload);
}

function enforceManualCopyStop(talentPayload) {
  stopOnManualCopy(talentPayload);
}

function manualCopyItems(talentPayload) {
  return getManualCopyItems(talentPayload);
}

function talentArtifacts(rawPayload, markdown, talentPayload) {
  return buildTalentArtifacts(rawPayload, markdown, talentPayload);
}

function stopForManualCopyItems(talentPayload) {
  enforceManualCopyStop(talentPayload);
}

function listManualCopyItems(talentPayload) {
  return manualCopyItems(talentPayload);
}

function finalizeTalentData(rawPayload, markdown, talentPayload) {
  return talentArtifacts(rawPayload, markdown, talentPayload);
}

function enforceManualCopyRequirement(talentPayload) {
  stopForManualCopyItems(talentPayload);
}

function manualCopyList(talentPayload) {
  return listManualCopyItems(talentPayload);
}

function buildTalentFinalOutputs(rawPayload, markdown, talentPayload) {
  return finalizeTalentData(rawPayload, markdown, talentPayload);
}

function maybeStopManualCopyRequirement(talentPayload) {
  enforceManualCopyRequirement(talentPayload);
}

function getManualCopyList(talentPayload) {
  return manualCopyList(talentPayload);
}

function compileTalentOutputs(rawPayload, markdown, talentPayload) {
  return buildTalentFinalOutputs(rawPayload, markdown, talentPayload);
}

function maybeStopOnHardFail(talentPayload) {
  maybeStopManualCopyRequirement(talentPayload);
}

function manualCopyTargets(talentPayload) {
  return getManualCopyList(talentPayload);
}

function buildTalentOutputBundle(rawPayload, markdown, talentPayload) {
  return compileTalentOutputs(rawPayload, markdown, talentPayload);
}

function enforceHardFailStop(talentPayload) {
  maybeStopOnHardFail(talentPayload);
}

function manualTargets(talentPayload) {
  return manualCopyTargets(talentPayload);
}

function buildTalentBundle(rawPayload, markdown, talentPayload) {
  return buildTalentOutputBundle(rawPayload, markdown, talentPayload);
}

function enforceHardFail(talentPayload) {
  enforceHardFailStop(talentPayload);
}

function manualTargetList(talentPayload) {
  return manualTargets(talentPayload);
}

function finalizeTalentBundle(rawPayload, markdown, talentPayload) {
  return buildTalentBundle(rawPayload, markdown, talentPayload);
}

function maybeStopAfterHardFail(talentPayload) {
  enforceHardFail(talentPayload);
}

function manualTargetsList(talentPayload) {
  return manualTargetList(talentPayload);
}

function finalTalentArtifacts(rawPayload, markdown, talentPayload) {
  return finalizeTalentBundle(rawPayload, markdown, talentPayload);
}

function enforceHardFailPolicy(talentPayload) {
  maybeStopAfterHardFail(talentPayload);
}

function getManualTargets(talentPayload) {
  return manualTargetsList(talentPayload);
}

function buildTalentOutput(rawPayload, markdown, talentPayload) {
  return finalTalentArtifacts(rawPayload, markdown, talentPayload);
}

function stopForFailedHard(talentPayload) {
  enforceHardFailPolicy(talentPayload);
}

function manualTargetsOutput(talentPayload) {
  return getManualTargets(talentPayload);
}

function finalizeTalentOutput(rawPayload, markdown, talentPayload) {
  return buildTalentOutput(rawPayload, markdown, talentPayload);
}

function enforceFailedHardStop(talentPayload) {
  stopForFailedHard(talentPayload);
}

function getFailedHardTargets(talentPayload) {
  return manualTargetsOutput(talentPayload);
}

function finishTalentOutput(rawPayload, markdown, talentPayload) {
  return finalizeTalentOutput(rawPayload, markdown, talentPayload);
}

function stopOnFailedHard(talentPayload) {
  enforceFailedHardStop(talentPayload);
}

function failedHardTargets(talentPayload) {
  return getFailedHardTargets(talentPayload);
}

function finalizeTalentWork(rawPayload, markdown, talentPayload) {
  return finishTalentOutput(rawPayload, markdown, talentPayload);
}

function enforceFailedHardPolicy(talentPayload) {
  stopOnFailedHard(talentPayload);
}

function listFailedHardTargets(talentPayload) {
  return failedHardTargets(talentPayload);
}

function finalizeTalentExecution(rawPayload, markdown, talentPayload) {
  return finalizeTalentWork(rawPayload, markdown, talentPayload);
}

function maybeStopFailedHard(talentPayload) {
  enforceFailedHardPolicy(talentPayload);
}

function failedHardTargetList(talentPayload) {
  return listFailedHardTargets(talentPayload);
}

function buildTalentExecutionResult(rawPayload, markdown, talentPayload) {
  return finalizeTalentExecution(rawPayload, markdown, talentPayload);
}

function enforceManualStop(talentPayload) {
  maybeStopFailedHard(talentPayload);
}

function getFailedHardTargetList(talentPayload) {
  return failedHardTargetList(talentPayload);
}

function bundleTalentExecution(rawPayload, markdown, talentPayload) {
  return buildTalentExecutionResult(rawPayload, markdown, talentPayload);
}

function maybeStopManual(talentPayload) {
  enforceManualStop(talentPayload);
}

function failedTargets(talentPayload) {
  return getFailedHardTargetList(talentPayload);
}

function buildTalentResult(rawPayload, markdown, talentPayload) {
  return bundleTalentExecution(rawPayload, markdown, talentPayload);
}

function maybeStopManualFailure(talentPayload) {
  maybeStopManual(talentPayload);
}

function failedTargetList(talentPayload) {
  return failedTargets(talentPayload);
}

function finalizeTalentResponse(rawPayload, markdown, talentPayload) {
  return buildTalentResult(rawPayload, markdown, talentPayload);
}

function enforceManualFailureStop(talentPayload) {
  maybeStopManualFailure(talentPayload);
}

function finalFailedTargetList(talentPayload) {
  return failedTargetList(talentPayload);
}

function buildTalentResponse(rawPayload, markdown, talentPayload) {
  return finalizeTalentResponse(rawPayload, markdown, talentPayload);
}

function maybeStopOnManualFailure(talentPayload) {
  enforceManualFailureStop(talentPayload);
}

function getFinalFailedTargets(talentPayload) {
  return finalFailedTargetList(talentPayload);
}

function finalizeTalentArtifactsBundle(rawPayload, markdown, talentPayload) {
  return buildTalentResponse(rawPayload, markdown, talentPayload);
}

function stopOnManualFailure(talentPayload) {
  maybeStopOnManualFailure(talentPayload);
}

function manualFailureTargets(talentPayload) {
  return getFinalFailedTargets(talentPayload);
}

function buildFinalTalentArtifacts(rawPayload, markdown, talentPayload) {
  return finalizeTalentArtifactsBundle(rawPayload, markdown, talentPayload);
}

function maybeStopForManualFailure(talentPayload) {
  stopOnManualFailure(talentPayload);
}

function getManualFailureTargets(talentPayload) {
  return manualFailureTargets(talentPayload);
}

function finalizeTalentOutputBundle(rawPayload, markdown, talentPayload) {
  return buildFinalTalentArtifacts(rawPayload, markdown, talentPayload);
}

function stopForManualFailures(talentPayload) {
  maybeStopForManualFailure(talentPayload);
}

function manualFailureTargetList(talentPayload) {
  return getManualFailureTargets(talentPayload);
}

function buildTalentFinalBundle(rawPayload, markdown, talentPayload) {
  return finalizeTalentOutputBundle(rawPayload, markdown, talentPayload);
}

function stopWhenManualFailuresExist(talentPayload) {
  stopForManualFailures(talentPayload);
}

function getManualFailureTargetList(talentPayload) {
  return manualFailureTargetList(talentPayload);
}

function finalizeTalentBuild(rawPayload, markdown, talentPayload) {
  return buildTalentFinalBundle(rawPayload, markdown, talentPayload);
}

function stopOnManualFailuresExist(talentPayload) {
  stopWhenManualFailuresExist(talentPayload);
}

function manualFailureList(talentPayload) {
  return getManualFailureTargetList(talentPayload);
}

function compileTalentBuild(rawPayload, markdown, talentPayload) {
  return finalizeTalentBuild(rawPayload, markdown, talentPayload);
}

function stopIfManualFailuresExist(talentPayload) {
  stopOnManualFailuresExist(talentPayload);
}

function getManualFailures(talentPayload) {
  return manualFailureList(talentPayload);
}

function finalizeTalentPipelineOutputs(rawPayload, markdown, talentPayload) {
  return compileTalentBuild(rawPayload, markdown, talentPayload);
}

function maybeStopIfManualFailuresExist(talentPayload) {
  stopIfManualFailuresExist(talentPayload);
}

function listManualFailures(talentPayload) {
  return getManualFailures(talentPayload);
}

function completeTalentOutputs(rawPayload, markdown, talentPayload) {
  return finalizeTalentPipelineOutputs(rawPayload, markdown, talentPayload);
}

function maybeStopIfManualNeeded(talentPayload) {
  maybeStopIfManualFailuresExist(talentPayload);
}

function manualNeededList(talentPayload) {
  return listManualFailures(talentPayload);
}

function finalTalentOutputs(rawPayload, markdown, talentPayload) {
  return completeTalentOutputs(rawPayload, markdown, talentPayload);
}

function maybeStopManualNeeded(talentPayload) {
  maybeStopIfManualNeeded(talentPayload);
}

function getManualNeededList(talentPayload) {
  return manualNeededList(talentPayload);
}

function buildFinalTalentOutputs(rawPayload, markdown, talentPayload) {
  return finalTalentOutputs(rawPayload, markdown, talentPayload);
}

async function fetchFandomWeaponTitles() {
  const url = "https://spacemarine2.fandom.com/api.php";
  const response = await http.get(url, {
    params: {
      action: "query",
      list: "categorymembers",
      cmtitle: "Category:Weapons",
      cmlimit: 200,
      format: "json",
      origin: "*"
    }
  });

  const members = response.data?.query?.categorymembers || [];
  return members
    .filter((entry) => entry.ns === 0)
    .map((entry) => entry.title)
    .filter((title) => title !== "Equipment");
}

function findInfoboxValue($, sourceName) {
  const node = $(`.pi-data[data-source="${sourceName}"] .pi-data-value`).first();
  return node.length ? node : null;
}

function extractImageUrl($) {
  const candidates = [
    $('.portable-infobox [data-source="image"] a').first().attr("href"),
    $('.portable-infobox [data-source="image"] img').first().attr("src"),
    $('.portable-infobox [data-source="image"] img').first().attr("data-src"),
    $(".mw-parser-output img").first().attr("src"),
    $(".mw-parser-output img").first().attr("data-src")
  ].filter(Boolean);

  const usable = candidates.find((candidate) => !String(candidate).startsWith("data:image"));
  return usable || "";
}

function collectSectionLinksByHeadlineId($, headlineId) {
  const results = [];
  const headline = $(`span.mw-headline#${headlineId}`).first();
  if (!headline.length) {
    return results;
  }

  const heading = headline.closest("h2, h3");
  let current = heading.next();
  while (current.length) {
    if (current.is("h2, h3") || current.find("> h2, > h3").length) {
      break;
    }
    current.find("a[title]").each((_, anchor) => {
      const title = normalizeWhitespace($(anchor).attr("title") || $(anchor).text());
      if (title) {
        results.push(title);
      }
    });
    current = current.next();
  }

  return uniq(results);
}

function extractRestrictionNotes(text) {
  const notes = [];
  const lines = String(text || "")
    .replace(/\u00a0/g, " ")
    .split(/\r?\n+/)
    .map((line) => normalizeWhitespace(line))
    .filter(Boolean);

  lines.forEach((line) => {
    const matches = line.match(/[A-Za-z][A-Za-z\s-]*?\((?:PvE|PvP)[^)]+\)/g) || [];
    matches.forEach((match) => notes.push(normalizeWhitespace(match)));
  });

  return uniq(notes);
}

async function scrapeFandomClass(title) {
  const page = await fetchFandomPage(title);
  const $ = cheerio.load(page.html);
  const bodyText = extractRawText($);

  const classRecord = {
    name: title,
    slug_candidate: slugify(title),
    image_key: `class_${slugify(title)}_img`,
    image_url: extractImageUrl($),
    class_role_text: extractRoleText($),
    class_ability: extractAbilityText($),
    character_name: extractCharacterName($),
    weapons: {
      primary: [],
      secondary: [],
      melee: []
    },
    notes: [],
    ...createParseMetadata("fandom", [page.url])
  };

  SLOT_LABELS.forEach(({ fandom, key }) => {
    const headlineId = fandom.replace(/\s+/g, "_");
    classRecord.weapons[key] = collectSectionLinksByHeadlineId($, headlineId).filter((weaponName) => weaponName !== title);
  });

  if (title === "Assault" || title === "Bulwark") {
    classRecord.weapons.primary = [];
  }

  classRecord.notes.push(...extractRestrictionNotes(bodyText));
  classRecord.notes = uniq(classRecord.notes);

  return finalizeParseMetadata(classRecord, ["name", "slug_candidate", "image_url", "class_role_text"]);
}

async function scrapeFandomWeapon(title) {
  const page = await fetchFandomPage(title);
  const $ = cheerio.load(page.html);
  const slotValue = normalizeWhitespace(findInfoboxValue($, "slot")?.text() || "");
  const classValueNode = findInfoboxValue($, "class");
  const classLinks = classValueNode ? classValueNode.find("a") : cheerio.load("")("a");
  const allowedClasses = uniq(
    classLinks
      .map((_, anchor) => normalizeWhitespace($(anchor).text()))
      .get()
  );

  const rawClassValue = classValueNode
    ? classValueNode.html().replace(/<br\s*\/?>/gi, "\n").replace(/<[^>]+>/g, " ")
    : "";
  const notes = extractRestrictionNotes(rawClassValue);
  const weaponRecord = {
    name: title,
    slug_candidate: slugify(title),
    image_key: `weapon_${slugify(title)}_img`,
    image_url: extractImageUrl($),
    slot_type: slotValue.toLowerCase(),
    allowed_classes: allowedClasses,
    mode_restriction_candidates: deriveModeRestrictionCandidates(notes),
    description_short: extractDescriptionShort($),
    notes,
    ...createParseMetadata("fandom", [page.url])
  };

  return finalizeParseMetadata(weaponRecord, ["name", "slug_candidate", "slot_type", "allowed_classes"]);
}

async function scrapeOfficialAssetCandidates(officialAnchor, classes, weapons) {
  const assets = buildOfficialAssetEntries(officialAnchor, classes, weapons).map((entry) =>
    finalizeParseMetadata(entry, ["asset_id", "asset_type", "candidate_page", "intended_bind"])
  );
  bindOfficialClassImages(classes, assets);
  bindWeaponImages(weapons, assets);
  return assets;
}


async function fetchGame8Root() {
  const url = "https://game8.co/games/Warhammer-40000-Space-Marine-2";
  const response = await http.get(url, { headers: GAME8_HEADERS });
  return {
    url,
    html: response.data,
    $: cheerio.load(response.data)
  };
}

function parseGame8SectionLinks($, headingId) {
  const heading = $("#" + headingId).first();
  if (!heading.length) {
    return [];
  }

  const links = [];
  let current = heading.next();
  while (current.length) {
    if (current.is("h2")) {
      break;
    }
    current.find("a.a-link, a.a-btn").each((_, anchor) => {
      const title = normalizeWhitespace($(anchor).text());
      const href = $(anchor).attr("href");
      const dataSrc =
        $(anchor).find("img").attr("data-src") ||
        $(anchor).find("img").attr("src") ||
        "";
      if (title && href && /archives\/\d+/.test(href)) {
        links.push({
          title,
          url: makeAbsoluteUrl(href, "https://game8.co"),
          image_url: dataSrc ? makeAbsoluteUrl(dataSrc, "https://game8.co") : ""
        });
      }
    });
    current = current.next();
  }

  const seen = new Set();
  return links.filter((entry) => {
    const key = `${entry.title}|${entry.url}`;
    if (seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
}

async function scrapeGame8WeaponDetail(entry) {
  const response = await http.get(entry.url, { headers: GAME8_HEADERS });
  const $ = cheerio.load(response.data);
  const heading = $("h2").filter((_, element) => /Classes$/i.test(normalizeWhitespace($(element).text()))).first();
  const classes = [];

  if (heading.length) {
    let current = heading.next();
    while (current.length) {
      if (current.is("h2")) {
        break;
      }
      current.find("a.a-link").each((_, anchor) => {
        const className = normalizeWhitespace($(anchor).text());
        if (CLASS_TITLES.includes(className)) {
          classes.push(className);
        }
      });
      current = current.next();
    }
  }

  return {
    name: entry.title,
    url: entry.url,
    allowed_classes: uniq(classes)
  };
}

async function collectGame8Validation() {
  const root = await fetchGame8Root();
  const classEntries = parseGame8SectionLinks(root.$, "hl_5").filter((entry) => CLASS_TITLES.includes(entry.title));
  const weaponEntries = parseGame8SectionLinks(root.$, "hl_4").filter((entry) => entry.title !== "List of All Weapons");

  const weapons = {};
  for (const entry of weaponEntries) {
    weapons[entry.title] = await scrapeGame8WeaponDetail(entry);
  }

  return {
    classes: Object.fromEntries(classEntries.map((entry) => [entry.title, entry])),
    weapons
  };
}

function attachValidationNotes(classes, weapons, game8Validation) {
  classes.forEach((classRecord) => {
    const game8Class = game8Validation.classes[classRecord.name];
    if (!game8Class) {
      classRecord.notes.push(conflictNote("Game8 missing / outdated: class absent from Game8 class list."));
    }
    classRecord.notes = uniq(classRecord.notes);
  });

  weapons.forEach((weaponRecord) => {
    const game8Weapon = game8Validation.weapons[weaponRecord.name];
    if (!game8Weapon) {
      weaponRecord.notes.push(conflictNote("Game8 missing / outdated: weapon absent from Game8 weapon list."));
    } else if (!compareSets(weaponRecord.allowed_classes, game8Weapon.allowed_classes)) {
      weaponRecord.notes.push(
        conflictNote(
          `Fandom!=Game8 classes: Fandom=[${weaponRecord.allowed_classes.join(", ")}] Game8=[${game8Weapon.allowed_classes.join(", ")}]`
        )
      );
    }
    weaponRecord.mode_restriction_candidates = uniq([
      ...weaponRecord.mode_restriction_candidates,
      ...deriveModeRestrictionCandidates(weaponRecord.notes)
    ]);
    weaponRecord.notes = uniq(weaponRecord.notes);
  });
}

function normalizeRawEntities(classes, weapons, officialAssets) {
  classes.forEach((entry) => {
    entry.notes = uniq(entry.notes);
    entry.parse_warnings = uniq(entry.parse_warnings);
    entry.source_pages = extractSourcePages(entry);
  });

  weapons.forEach((entry) => {
    entry.allowed_classes = uniq(entry.allowed_classes);
    entry.notes = uniq(entry.notes);
    entry.parse_warnings = uniq(entry.parse_warnings);
    entry.mode_restriction_candidates = uniq(entry.mode_restriction_candidates);
    entry.source_pages = extractSourcePages(entry);
  });

  officialAssets.forEach((entry) => {
    entry.notes = uniq(entry.notes || []);
    entry.parse_warnings = uniq(entry.parse_warnings);
    entry.source_pages = extractSourcePages(entry);
  });
}

function buildSampleFieldLogs(classes, weapons) {
  const sampleNames = new Set(["Techmarine", "Melta Rifle", "Heavy Firearms Melee"]);
  return {
    classes: classes
      .filter((entry) => sampleNames.has(entry.name))
      .map((entry) => ({ name: entry.name, missing_fields: entry.missing_fields, parse_warnings: entry.parse_warnings })),
    weapons: weapons
      .filter((entry) => sampleNames.has(entry.name))
      .map((entry) => ({ name: entry.name, missing_fields: entry.missing_fields, parse_warnings: entry.parse_warnings }))
  };
}

function buildReviewSeed(classes, weapons, officialAssets) {
  return {
    items: collectParseIssues(classes, weapons, officialAssets)
  };
}

function buildDriftReport(classes, weapons) {
  return {
    tracked_samples: collectDriftSamples(classes, weapons),
    field_logs: buildSampleFieldLogs(classes, weapons)
  };
}

function buildParseReport(classes, weapons, officialAssets) {
  return {
    critical_field_logs: collectCriticalFieldLogs(classes, weapons, officialAssets),
    issue_count: collectParseIssues(classes, weapons, officialAssets).length
  };
}

function buildSourcePages(entry) {
  return extractSourcePages(entry);
}

function toRawClassPayload(entry) {
  return {
    name: entry.name,
    slug_candidate: entry.slug_candidate,
    image_key: entry.image_key,
    image_url: entry.image_url,
    class_role_text: entry.class_role_text,
    class_ability: entry.class_ability,
    character_name: entry.character_name,
    weapons: entry.weapons,
    notes: entry.notes,
    source_pages: buildSourcePages(entry),
    source_type: entry.source_type,
    parse_warnings: entry.parse_warnings,
    parse_degraded: entry.parse_degraded,
    missing_fields: entry.missing_fields
  };
}

function toRawWeaponPayload(entry) {
  return {
    name: entry.name,
    slug_candidate: entry.slug_candidate,
    image_key: entry.image_key,
    image_url: entry.image_url,
    slot_type: entry.slot_type,
    allowed_classes: entry.allowed_classes,
    mode_restriction_candidates: entry.mode_restriction_candidates,
    description_short: entry.description_short,
    notes: entry.notes,
    source_pages: buildSourcePages(entry),
    source_type: entry.source_type,
    parse_warnings: entry.parse_warnings,
    parse_degraded: entry.parse_degraded,
    missing_fields: entry.missing_fields
  };
}

function toRawOfficialAssetPayload(entry) {
  return {
    asset_id: entry.asset_id,
    asset_type: entry.asset_type,
    title: entry.title,
    candidate_page: entry.candidate_page,
    image_hint: entry.image_hint,
    remote_url: entry.remote_url,
    intended_bind: entry.intended_bind,
    copyright_note: entry.copyright_note,
    notes: entry.notes,
    source_pages: buildSourcePages(entry),
    source_type: entry.source_type,
    parse_warnings: entry.parse_warnings,
    parse_degraded: entry.parse_degraded,
    missing_fields: entry.missing_fields
  };
}

function rebuildClassWeaponsFromWeapons(classes, weapons) {
  const classMap = new Map(classes.map((entry) => [entry.name, entry]));

  weapons.forEach((weaponRecord) => {
    const slotKey = SLOT_LABELS.find((slot) => slot.slotType === weaponRecord.slot_type)?.key;
    if (!slotKey) {
      return;
    }
    weaponRecord.allowed_classes.forEach((className) => {
      const classRecord = classMap.get(className);
      if (!classRecord) {
        return;
      }
      classRecord.weapons[slotKey].push(weaponRecord.name);
    });
  });

  classes.forEach((entry) => {
    entry.weapons.primary = entry.name === "Assault" || entry.name === "Bulwark" ? [] : uniq(entry.weapons.primary);
    entry.weapons.secondary = uniq(entry.weapons.secondary);
    entry.weapons.melee = uniq(entry.weapons.melee);
  });
}

function crossCheckClassWeaponClosure(classes, weapons) {
  const weaponMap = new Map(weapons.map((weapon) => [weapon.name, weapon]));

  classes.forEach((classRecord) => {
    SLOT_LABELS.forEach(({ key, slotType }) => {
      classRecord.weapons[key].forEach((weaponName) => {
        const weapon = weaponMap.get(weaponName);
        if (!weapon) {
          classRecord.notes.push(conflictNote(`Fandom class map references missing weapon page: ${weaponName}`));
          return;
        }
        if (weapon.slot_type !== slotType) {
          classRecord.notes.push(conflictNote(`Slot mismatch for ${weaponName}: class=${slotType} weapon=${weapon.slot_type}`));
        }
        if (!weapon.allowed_classes.includes(classRecord.name)) {
          weapon.notes.push(
            conflictNote(`Reverse mapping mismatch: ${classRecord.name} lists ${weaponName}, but weapon page omits the class.`)
          );
        }
      });
    });
    classRecord.notes = uniq(classRecord.notes);
  });

  weapons.forEach((weaponRecord) => {
    weaponRecord.notes = uniq(weaponRecord.notes);
  });
}

function buildRawPayload(classes, weapons, officialAssets, officialAnchor, game8Validation) {
  const rawClasses = classes.map(toRawClassPayload);
  const rawWeapons = weapons.map(toRawWeaponPayload);
  const rawOfficialAssets = officialAssets.map(toRawOfficialAssetPayload);
  const conflictCount =
    rawClasses.reduce((count, entry) => count + entry.notes.filter((note) => note.startsWith("[CONFLICT]")).length, 0) +
    rawWeapons.reduce((count, entry) => count + entry.notes.filter((note) => note.startsWith("[CONFLICT]")).length, 0);

  return {
    meta: {
      generated_at: new Date().toISOString(),
      version_anchor: VERSION_ANCHOR,
      official_anchor: officialAnchor,
      sources: {
        official: officialAnchor.url,
        fandom: "https://spacemarine2.fandom.com",
        game8: "https://game8.co/games/Warhammer-40000-Space-Marine-2"
      },
      game8_known_class_count: Object.keys(game8Validation.classes).length,
      game8_known_weapon_count: Object.keys(game8Validation.weapons).length,
      conflict_count: conflictCount,
      parse_report: buildParseReport(rawClasses, rawWeapons, rawOfficialAssets),
      drift_report: buildDriftReport(rawClasses, rawWeapons)
    },
    classes: rawClasses,
    weapons: rawWeapons,
    official_assets: rawOfficialAssets,
    review_seed: buildReviewSeed(rawClasses, rawWeapons, rawOfficialAssets)
  };
}

function formatList(values) {
  return values.length ? values.join(", ") : "[]";
}

function formatNotes(values) {
  return values.length ? values.join("<br>") : "";
}

function buildMarkdownTable(rawPayload) {
  const lines = [];
  lines.push("# Space Marine 2 Data Review");
  lines.push("");
  lines.push(`- Generated At: ${rawPayload.meta.generated_at}`);
  lines.push(`- Version Anchor: ${rawPayload.meta.version_anchor}`);
  lines.push(`- Conflict Count: ${rawPayload.meta.conflict_count}`);
  lines.push(`- Official Anchor: ${rawPayload.meta.official_anchor.url}`);
  lines.push("");
  lines.push("## 职业表");
  lines.push("");
  lines.push("| Class | Image URL | Primary | Secondary | Melee | Notes |");
  lines.push("| --- | --- | --- | --- | --- | --- |");
  rawPayload.classes.forEach((entry) => {
    lines.push(
      `| ${entry.name} | ${entry.image_url} | ${formatList(entry.weapons.primary)} | ${formatList(entry.weapons.secondary)} | ${formatList(entry.weapons.melee)} | ${formatNotes(entry.notes)} |`
    );
  });
  lines.push("");
  lines.push("## 武器表");
  lines.push("");
  lines.push("| Weapon | Slot | Image URL | Allowed Classes | Notes |");
  lines.push("| --- | --- | --- | --- | --- |");
  rawPayload.weapons.forEach((entry) => {
    lines.push(
      `| ${entry.name} | ${entry.slot_type} | ${entry.image_url} | ${formatList(entry.allowed_classes)} | ${formatNotes(entry.notes)} |`
    );
  });
  lines.push("");
  return lines.join("\n");
}

async function writeOutputs(rawPayload, markdown) {
  await ensureOutputDirs();
  const rawText = `${JSON.stringify(rawPayload, null, 2)}\n`;
  const classWeaponMapText = `${JSON.stringify(buildClassWeaponMapPayload(rawPayload), null, 2)}\n`;
  await fs.writeFile(RAW_OUTPUT_PATH, rawText, "utf8");
  await fs.writeFile(CLASS_WEAPON_MAP_OUTPUT_PATH, classWeaponMapText, "utf8");
  await fs.writeFile(TABLE_OUTPUT_PATH, markdown, "utf8");
}

function buildClassWeaponMapPayload(rawPayload) {
  const classes = Array.isArray(rawPayload && rawPayload.classes) ? rawPayload.classes : [];
  return {
    source: "wiki",
    class_weapon_map: Object.fromEntries(
      classes.map((entry) => [
        entry.slug_candidate,
        {
          primary: Array.isArray(entry.weapons && entry.weapons.primary) ? entry.weapons.primary : [],
          secondary: Array.isArray(entry.weapons && entry.weapons.secondary) ? entry.weapons.secondary : [],
          melee: Array.isArray(entry.weapons && entry.weapons.melee) ? entry.weapons.melee : []
        }
      ])
    )
  };
}


main().catch((error) => {
  console.error(error.stack || String(error));
  process.exit(1);
});
