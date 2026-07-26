// 联动文章：legacy 竖线字符串与结构化两种来源的解析、按阶级过滤与卡片渲染。
// resolveAugment 回调由调用方注入（携带当前页面的基础表/图标映射）。

import { escapeHtml } from '../shared/dom.js';
import { normalizeTierName, renderAugmentIcon, splitAugmentCandidates } from './augments.js';

export function normalizeGradeValue(grade) {
    return String(grade || '未知').replace(/^评分\s*/i, '').trim().toUpperCase() || '未知';
}

export function getGradeScore(grade) {
    const gradeVal = normalizeGradeValue(grade);
    if (gradeVal.includes('SS')) return 6;
    if (gradeVal.includes('S')) return 5;
    if (gradeVal.includes('A')) return 4;
    if (gradeVal.includes('B')) return 3;
    if (gradeVal.includes('C')) return 2;
    if (gradeVal.includes('D')) return 1;
    return 0;
}

export function parseLegacySynergyString(raw) {
    const str = String(raw || '');
    const parts = str.split('|').map((s) => s.trim());
    if (parts.length < 4) {
        return { original: str, name: '未知联动', tier: '', grade: '未知', tags: [], content: str };
    }

    const nameInfo = parts[0];
    const tierName = parts[1];
    const gradeStr = parts[2];
    const tagGroup = parts[3];
    let upvotes = 0;
    let downvotes = 0;
    let contentStartIndex = 5;
    if (parts.length > 5 && /^\d+$/.test(parts[4]) && /^\d+$/.test(parts[5])) {
        upvotes = parseInt(parts[4] || '0');
        downvotes = parseInt(parts[5] || '0');
        contentStartIndex = 6;
    }

    let author = '佚名';
    let content = '';
    let isOriginal = false;

    if (parts.length > contentStartIndex) {
        const nextPart = parts[contentStartIndex];
        if (nextPart.startsWith('作者：') || nextPart.startsWith('作者:')) {
            author = nextPart.substring(3).trim();
            contentStartIndex += 1;
            if (parts.length > contentStartIndex && (parts[contentStartIndex] === '原创' || parts[contentStartIndex] === '非原创')) {
                isOriginal = parts[contentStartIndex] === '原创';
                contentStartIndex += 1;
            }
        } else if (nextPart === '原创') {
            isOriginal = true;
            contentStartIndex += 1;
            if (parts.length > contentStartIndex && (parts[contentStartIndex].startsWith('作者：') || parts[contentStartIndex].startsWith('作者:'))) {
                author = parts[contentStartIndex].substring(3).trim();
                contentStartIndex += 1;
            }
        } else if (parts.length > contentStartIndex + 1 && (parts[contentStartIndex + 1] === '原创' || parts[contentStartIndex + 1] === '非原创')) {
            author = nextPart || author;
            isOriginal = parts[contentStartIndex + 1] === '原创';
            contentStartIndex += 2;
        }
        content = parts.slice(contentStartIndex).join(' | ');
    }

    return {
        original: str,
        name: nameInfo,
        augmentNames: splitAugmentCandidates(nameInfo),
        tier: tierName,
        grade: normalizeGradeValue(gradeStr),
        tags: tagGroup.split(/\s+/).filter((t) => t),
        upvotes: upvotes,
        downvotes: downvotes,
        author: author,
        isOriginal: isOriginal,
        content: content,
    };
}

export function normalizeSynergyArticle(raw) {
    if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
        const augmentNames = Array.isArray(raw.augment_names)
            ? raw.augment_names
            : (Array.isArray(raw.augmentNames) ? raw.augmentNames : splitAugmentCandidates(raw.name));
        const nameInfo = String(raw.name || augmentNames.join(', ') || '未知联动');
        return {
            original: raw,
            name: nameInfo,
            augmentNames: augmentNames.filter(Boolean),
            tier: String(raw.tier || ''),
            grade: normalizeGradeValue(raw.rating || raw.grade),
            tags: String(raw.tag || (Array.isArray(raw.tags) ? raw.tags.join(' ') : '') || '强力联动').split(/\s+/).filter((t) => t),
            upvotes: Number(raw.upvotes || 0),
            downvotes: Number(raw.downvotes || 0),
            author: String(raw.author || '佚名'),
            isOriginal: Boolean(raw.is_original || raw.isOriginal),
            content: String(raw.content || ''),
        };
    }
    return parseLegacySynergyString(raw);
}

function isKnownSynergyTier(rawTier) {
    const text = String(rawTier || '').trim();
    return Boolean(text && (
        text.includes('棱彩') || text.includes('彩色') || text === 'Prismatic' ||
        text.includes('黄金') || text.includes('金') || text === 'Gold' ||
        text.includes('白银') || text.includes('银') || text === 'Silver'
    ));
}

function articleMatchesTier(item, tier, resolveAugment) {
    if (tier === 'all') return true;
    if (isKnownSynergyTier(item.tier)) {
        return normalizeTierName(item.tier) === tier;
    }
    const resolvedAugment = resolveAugment(item);
    return resolvedAugment.tier && normalizeTierName(resolvedAugment.tier) === tier;
}

// 联动数据过期横幅：apex/mayhem 冻结超阈值时按 synergy_data_at 现算年龄如实标注；
// 时间不可解析时回退通用文案，不虚构年龄。
export function buildSynergyStaleBanner(synergyMeta, nowMs = Date.now()) {
    if (!synergyMeta || !synergyMeta.synergy_data_stale) return '';
    let ageText = '联动数据为上一代';
    const dataAtMs = synergyMeta.synergy_data_at ? Date.parse(synergyMeta.synergy_data_at) : NaN;
    if (Number.isFinite(dataAtMs)) {
        const hours = Math.max(0, Math.floor((nowMs - dataAtMs) / 3600000));
        if (hours >= 48) {
            ageText = `联动数据为 ${Math.floor(hours / 24)} 天前`;
        } else if (hours >= 1) {
            ageText = `联动数据为 ${hours} 小时前`;
        }
    }
    return `<div class="glass-panel rounded-xl p-2 mb-4 text-center text-xs text-amber-300">${escapeHtml(ageText)}，来源更新恢复前仅供参考</div>`;
}

// 按当前阶级过滤联动文章并渲染；空态文案区分隔离/失败/无数据三种来源状态。
export function updateFilteredSynergies({ container, tier, synergyData, synergyMeta, synergyLoaded, resolveAugment }) {
    container.dataset.synergyLoaded = synergyLoaded ? '1' : '0';
    container.dataset.synergyStatus = synergyMeta && synergyMeta.status ? synergyMeta.status : 'ok';
    const staleBanner = buildSynergyStaleBanner(synergyMeta);
    const normalized = (synergyData || []).map(normalizeSynergyArticle).filter((item) => item.content);
    if (normalized.length === 0) {
        let message = '暂无联动文章';
        if (synergyMeta && synergyMeta.status === 'quarantined') {
            message = synergyMeta.message || '联动数据待校准';
        } else if (synergyMeta && synergyMeta.status === 'error') {
            message = synergyMeta.message || '联动数据读取失败';
        } else if (synergyMeta && synergyMeta.status === 'empty') {
            message = synergyMeta.message || '暂无联动数据';
        }
        container.innerHTML = `<div class="glass-panel rounded-xl p-4"><div class="text-sm text-gray-400 text-center">${escapeHtml(message)}</div></div>`;
        return;
    }

    const filtered = normalized.filter((item) => articleMatchesTier(item, tier, resolveAugment));

    if (filtered.length === 0) {
        container.innerHTML = `${staleBanner}<div class="glass-panel rounded-xl p-4"><div class="text-sm text-gray-400 text-center">该阶级无联动文章</div></div>`;
        return;
    }

    renderSynergyArticles(filtered, container, resolveAugment);
    if (staleBanner) {
        container.insertAdjacentHTML('afterbegin', staleBanner);
    }
}

export function renderSynergyArticles(articles, container, resolveAugment) {
    if (!articles || articles.length === 0) {
        container.innerHTML = '<div class="glass-panel rounded-xl p-4"><div class="text-sm text-gray-400 text-center">暂无联动文章</div></div>';
        return;
    }

    const parsedArticles = articles.map((item) => {
        const resolvedAugment = resolveAugment(item);
        const gradeScore = getGradeScore(item.grade);
        return {
            ...item,
            gradeScore: gradeScore,
            fallbackScore: resolvedAugment.score || 0,
            resolvedAugment: resolvedAugment,
        };
    });

    const html = parsedArticles.map((item) => {
        if (item.gradeScore === -1) {
            return `
                <div class="glass-panel rounded-xl p-4 border border-gray-800 hover:border-blue-500/30 transition-colors">
                    <div class="hextech-article-content text-sm text-gray-300">${escapeHtml(item.content)}</div>
                </div>
            `;
        }

        let badgeBg = 'bg-[#8c9ba5]';
        let badgeText = 'text-[#000000]';
        if (item.gradeScore >= 5) {
            badgeBg = 'bg-[#ebd55b]';
            badgeText = 'text-[#000000]';
        } else if (item.gradeScore === 4) {
            badgeBg = 'bg-[#9a73d1]';
            badgeText = 'text-[#ffffff]';
        } else if (item.gradeScore === 3) {
            badgeBg = 'bg-[#4b7cf3]';
            badgeText = 'text-[#ffffff]';
        }

        const resolvedAugment = item.resolvedAugment || resolveAugment(item);
        const iconUrl = resolvedAugment.icon;
        const safeName = escapeHtml(item.name);
        const displayTier = item.tier || resolvedAugment.tier;
        const safeTier = escapeHtml(displayTier);
        const safeGrade = escapeHtml(item.grade);
        const safeAuthor = escapeHtml(item.author);

        const getTagStyle = (tag) => {
            if (tag.includes('强力联动')) return 'border border-[#2bd5c2]/40 text-[#2bd5c2] bg-[#2bd5c2]/10';
            if (tag.includes('娱乐')) return 'border border-[#9a73d1]/40 text-[#9a73d1] bg-[#9a73d1]/10';
            if (tag.includes('陷阱')) return 'border border-[#e28544]/40 text-[#e28544] bg-[#e28544]/10';
            return 'border border-gray-500/40 text-gray-400 bg-gray-500/10';
        };

        const hexInfo = {
            name: resolvedAugment.name || item.name,
            tier: resolvedAugment.tier || item.tier,
            desc: resolvedAugment.desc || '无详细描述',
        };
        const bindData = encodeURIComponent(JSON.stringify(hexInfo));

        return `
<div class="hextech-card p-[1px] hx-surface-card mb-4" style="clip-path: polygon(0 0, calc(100% - 14px) 0, 100% 14px, 100% 100%, 14px 100%, 0 calc(100% - 14px));">
    <div class="hextech-article-inner bg-[#060b10]" style="clip-path: polygon(0 0, calc(100% - 13px) 0, 100% 13px, 100% 100%, 13px 100%, 0 calc(100% - 13px));">

        <div class="cursor-pointer" data-hex-info="${bindData}">
            ${renderAugmentIcon(iconUrl, resolvedAugment.name || item.name, displayTier, 'detail')}
        </div>

        <div class="hextech-article-body flex flex-col flex-grow">
            <div class="hextech-article-head">

                <div class="hextech-article-title-block flex flex-col">
                    <div class="flex items-center gap-2.5 flex-wrap">
                        <span class="text-white text-[16px] font-bold tracking-wide leading-none">${safeName}</span>
                        <div class="${badgeBg} px-1.5 h-[20px] rounded flex items-center shadow-sm">
                            <span class="${badgeText} text-[11px] font-black">${safeGrade}</span>
                        </div>
                        ${item.tags.map((tag) => `<div class="${getTagStyle(tag)} px-1.5 h-[20px] rounded flex items-center text-[10px] font-bold">${escapeHtml(tag)}</div>`).join('')}
                    </div>
                    <div class="text-[#8c9ba5] text-[11px] mt-1.5 leading-none">
                        ${safeTier}
                    </div>
                </div>

                <div class="hextech-article-meta flex items-center gap-2 mt-0">
                    <div class="flex items-center bg-[#1e2333] border border-[#2a3040] rounded-full overflow-hidden h-[24px]">
                        <div class="flex items-center gap-1.5 px-3 h-full border-r border-[#2a3040]">
                            <svg class="w-3.5 h-3.5 text-[#507c59] fill-current" viewBox="0 0 16 16"><path d="M3 1.5A.5.5 0 0 1 3.5 1h9a.5.5 0 0 1 .4.82l-2 2.68 2 2.68a.5.5 0 0 1-.4.82h-9v6.5a.5.5 0 0 1-1 0v-13z"/></svg>
                            <span class="text-[#8c9ba5] text-[12px] font-medium pt-0.5">${item.upvotes}</span>
                        </div>
                        <div class="flex items-center gap-1.5 px-3 h-full">
                            <svg class="w-3.5 h-3.5 text-[#c29845] fill-current" viewBox="0 0 16 16"><path d="M8 13.25 2.75 5.5h10.5L8 13.25z"/></svg>
                            <span class="text-[#8c9ba5] text-[12px] font-medium pt-0.5">${item.downvotes}</span>
                        </div>
                    </div>
                    ${item.author ? `<div class="text-[#8c9ba5] text-[12px] flex flex-wrap items-center justify-end gap-1.5 min-w-0 text-right">作者: ${safeAuthor} ${item.isOriginal ? '<span class="bg-[#ebd55b] text-black px-1 py-[1px] rounded-[3px] text-[10px] font-black tracking-wide leading-none pt-[3px]">原创</span>' : ''}</div>` : ''}
                </div>

            </div>

            <div class="hextech-article-content text-[#b3b9c6] text-[14px] mt-3">
                ${escapeHtml(item.content)}
            </div>

        </div>
    </div>
</div>
        `;
    }).join('');

    container.innerHTML = html;
}
