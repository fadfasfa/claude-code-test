// 海克斯渲染工具：阶级归一、tooltip 文案净化、图标解析与列表卡片模板。
// 纯函数模块；数据（基础表/图标映射/目录映射）由调用方传入。

import { escapeHtml } from '../shared/dom.js';
import { normalizeAugmentKey } from '../shared/text.js';

export function normalizeTierName(tier) {
    const text = String(tier || '').trim();
    if (text.includes('棱彩') || text.includes('彩色') || text === 'Prismatic') return 'Prismatic';
    if (text.includes('黄金') || text.includes('金') || text === 'Gold') return 'Gold';
    return 'Silver';
}

export function getTierBarClass(tier) {
    const normalized = normalizeTierName(tier);
    if (normalized === 'Prismatic') return 'hx-tier-prismatic-bar';
    if (normalized === 'Gold') return 'hx-tier-gold-bar';
    return 'hx-tier-silver-bar';
}

export function getAugmentTierClass(tier) {
    const normalized = normalizeTierName(tier);
    if (normalized === 'Prismatic') return 'tier-shell-prismatic';
    if (normalized === 'Gold') return 'tier-shell-gold';
    return 'tier-shell-silver';
}

export function toLocalAugmentIconUrl(iconUrl, hextechName) {
    if (iconUrl) {
        return iconUrl;
    }
    // augments/ 前缀必需；后端会按名称查目录映射真实文件名并兜底远端。
    return `/assets/augments/${encodeURIComponent(hextechName)}.png`;
}

export function splitAugmentCandidates(nameText) {
    return String(nameText || '')
        .split(/[，,、\/\|+]/)
        .map((part) => part.trim())
        .filter(Boolean);
}

export function isQuestionMarkAugmentName(text) {
    return /^[?？]{3,}$/.test(String(text || '').trim());
}

export function purifyHextechTooltip(text) {
    if (!text) return '';
    const original = String(text).trim();
    if (isQuestionMarkAugmentName(original)) {
        return original;
    }

    let s = String(text);
    s = s.replace(/\{\{\s*Item_Keyword_OnHit\s*\}\}/gi, '攻击特效');
    s = s.replace(/\{\{.*?\}\}/g, '');
    s = s.replace(/<\/?lol-uikit[^>]*>/gi, '');
    s = s.replace(/%i:[a-zA-Z0-9_]+%/gi, '');

    // 剥离动态追踪面板（包括已造成的伤害等变体）
    s = s.replace(/<br>\s*(已获得的额外属性|已造成的伤害|已造成的额外伤害|已提供的护盾|伤害|治疗)[：:][\s\S]*/g, '');
    s = s.replace(/\s*(已获得的额外属性|已造成的伤害|已造成的额外伤害|已提供的护盾|伤害|治疗)[：:][\s\S]*/g, '');

    // 仅清理 tooltip 内未解析变量留下的 ASCII 问号，不处理真实海克斯名“？？？”。
    s = s.replace(/\?{2,}/g, '');
    s = s.replace(/(?:\s*\?\s*){2,}/g, '');
    s = s.replace(/^[?\s]+|[?\s]+$/g, '');

    s = s.replace(/\n\s*\n/g, '\n');
    return s.trim();
}

export function renderAugmentIcon(iconUrl, hextechName, tier, variant = 'list') {
    const localIconUrl = toLocalAugmentIconUrl(iconUrl, hextechName);
    const tierClass = getAugmentTierClass(tier);
    const safeHextechName = escapeHtml(hextechName);
    const safeTier = escapeHtml(tier || '');
    const safeIconUrl = escapeHtml(localIconUrl);
    const shellClass = variant === 'detail'
        ? `augment-icon-shell augment-icon-shell--detail ${tierClass}`
        : `augment-icon-shell augment-icon-shell--list ${tierClass}`;

    return `
        <div class="${shellClass}">
            <img loading="lazy" width="46" height="46" src="${safeIconUrl}"
                 alt="${safeHextechName}"
                 decoding="async"
                 data-hextech-name="${safeHextechName}"
                 data-hextech-tier="${safeTier}">
        </div>
    `;
}

export function createHextechCard(hextech, index) {
    const iconUrl = toLocalAugmentIconUrl(hextech.icon, hextech.海克斯名称);
    const tierClass = getTierBarClass(hextech.海克斯阶级);

    const wrValue = Number(hextech.海克斯胜率) || 0;
    const wr = (wrValue * 100).toFixed(1);
    const pr = (hextech.海克斯出场率 * 100).toFixed(1);

    // 胜率热力图分级：>53% 为热点、<47% 为冷点，居中区段保持中性
    let heatClass = '';
    let trendArrowMarkup = '';
    if (wrValue > 0.53) {
        heatClass = 'heat-hot';
        trendArrowMarkup = '<span class="hextech-card-rate-trend text-emerald-300">▲</span>';
    } else if (wrValue < 0.47) {
        heatClass = 'heat-cold';
        trendArrowMarkup = '<span class="hextech-card-rate-trend text-rose-300">▼</span>';
    }

    const hexInfo = {
        name: hextech.海克斯名称,
        tier: hextech.海克斯阶级,
        desc: purifyHextechTooltip(hextech.tooltip_plain),
    };
    const bindData = encodeURIComponent(JSON.stringify(hexInfo));

    return `
        <div class="hextech-list-card list-row cursor-pointer p-2 rounded border border-[rgba(139,148,158,0.18)] bg-[rgba(19,26,34,0.92)] transition-colors ${heatClass}" data-hex-info="${bindData}">
            <div class="hextech-card-rank text-xs font-bold text-gray-500">${index + 1}</div>

            <div class="hextech-card-icon-group">
                <div class="w-1.5 h-6 rounded-full ${tierClass}"></div>
                ${renderAugmentIcon(iconUrl, hextech.海克斯名称, hextech.海克斯阶级, 'list')}
            </div>

            <div class="hextech-card-title">
                <div class="text-sm font-bold text-gray-200 truncate" title="${escapeHtml(hextech.海克斯名称)}">${escapeHtml(hextech.海克斯名称)}</div>
            </div>

            <div class="hextech-card-rate hextech-card-rate--win">
                <span class="hextech-card-rate-value">${wr}%</span>${trendArrowMarkup}
            </div>

            <div class="hextech-card-rate hextech-card-rate--pick">
                <span class="hextech-card-rate-value">${pr}%</span>
            </div>
        </div>
    `;
}

export function getArticleNameText(item) {
    if (item && Array.isArray(item.augmentNames) && item.augmentNames.length > 0) {
        return item.augmentNames.join(', ');
    }
    if (item && Array.isArray(item.augment_names) && item.augment_names.length > 0) {
        return item.augment_names.join(', ');
    }
    return item ? (item.name || '') : '';
}

function findBaseHexByName(nameText, baseArray) {
    const candidates = splitAugmentCandidates(nameText);
    const exactNames = [String(nameText || '').trim(), ...candidates].filter(Boolean);

    for (const exactName of exactNames) {
        const exact = baseArray.find((h) => h.海克斯名称 === exactName);
        if (exact) {
            return exact;
        }
    }

    const baseByKey = new Map();
    baseArray.forEach((hex) => {
        const key = normalizeAugmentKey(hex.海克斯名称);
        if (key && !baseByKey.has(key)) {
            baseByKey.set(key, hex);
        }
    });
    for (const candidate of candidates) {
        const matched = baseByKey.get(normalizeAugmentKey(candidate));
        if (matched) {
            return matched;
        }
    }
    return null;
}

function findAugmentIconFromMap(nameText, augmentIconMap) {
    const candidates = splitAugmentCandidates(nameText);
    const entries = Object.entries(augmentIconMap || {});
    const byKey = new Map(entries.map(([name, url]) => [normalizeAugmentKey(name), { name, url }]));
    for (const candidate of candidates) {
        const hit = byKey.get(normalizeAugmentKey(candidate));
        if (hit) {
            return hit;
        }
    }
    return null;
}

function findAugmentCatalogEntry(nameText, augmentCatalogMap) {
    const candidates = splitAugmentCandidates(nameText);
    for (const candidate of candidates) {
        const hit = augmentCatalogMap[normalizeAugmentKey(candidate)];
        if (hit) {
            return hit;
        }
    }
    return null;
}

// 联动文章 → 海克斯信息的四级解析链：基础表精确/归一匹配 → 目录 → 图标映射 → 名称兜底
export function resolveArticleAugment(item, { baseArray, augmentIconMap, augmentCatalogMap }) {
    const nameText = getArticleNameText(item);
    const baseHex = findBaseHexByName(nameText, baseArray || []);
    if (baseHex) {
        return {
            name: baseHex.海克斯名称,
            icon: toLocalAugmentIconUrl(baseHex.icon, baseHex.海克斯名称),
            tier: baseHex.海克斯阶级,
            desc: purifyHextechTooltip(baseHex.tooltip_plain),
            score: baseHex.综合得分 || baseHex.海克斯胜率 || 0,
        };
    }

    const catalogEntry = findAugmentCatalogEntry(nameText, augmentCatalogMap || {});
    if (catalogEntry) {
        return {
            name: catalogEntry.name,
            icon: catalogEntry.icon || toLocalAugmentIconUrl('', catalogEntry.name),
            tier: catalogEntry.tier || item.tier,
            desc: purifyHextechTooltip(catalogEntry.tooltip_plain || catalogEntry.description || ''),
            score: 0,
        };
    }

    const mapped = findAugmentIconFromMap(nameText, augmentIconMap || {});
    if (mapped) {
        return {
            name: mapped.name,
            icon: mapped.url,
            tier: item.tier,
            desc: '无详细描述',
            score: 0,
        };
    }

    const fallbackName = splitAugmentCandidates(nameText)[0] || nameText;
    return {
        name: fallbackName,
        icon: toLocalAugmentIconUrl('', fallbackName),
        tier: item.tier,
        desc: '无详细描述',
        score: 0,
    };
}
