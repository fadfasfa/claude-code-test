// 首页 T 度榜渲染：分档分组、英雄卡片模板与备战席高亮增量更新。
// 纯渲染层，不持有页面状态；数据与容器由入口注入。

import { escapeHtml } from '../shared/dom.js';
import { peekDdragonVersion } from '../shared/ddragon.js';
import { CHAMPION_PINYIN } from './champion_pinyin.js';

// 视觉样式仍由 Web 负责，但分档只消费 generation 的权威 `英雄评级`。
// 阈值唯一维护在 Python 的 champion_tier.py，避免两端随时间漂移。
export const TIERS = [
    { id: 'T1', name: '夯', enName: 'God Tier', cssClass: 'hx-tier-op' },
    { id: 'T2', name: '顶级', enName: 'Top', cssClass: 'hx-tier-s' },
    { id: 'T3', name: '人上人', enName: 'Elite', cssClass: 'hx-tier-a' },
    { id: 'T4', name: 'npc', enName: 'Average', cssClass: 'hx-tier-npc' },
    { id: 'T5', name: '拉', enName: 'Trash', cssClass: 'hx-tier-trash' },
];
const TIER_BY_ID = new Map(TIERS.map((tier) => [tier.id, tier]));

export function formatPercent(num) {
    return (num * 100).toFixed(1) + '%';
}

// 胜率强弱着色：与详情页 heat 阈值一致（>53% 绿、<47% 红、居中中性）。
export function winRateClass(winRate) {
    const value = Number(winRate);
    if (!Number.isFinite(value)) return 'hx-wr-flat';
    if (value > 0.53) return 'hx-wr-up';
    if (value < 0.47) return 'hx-wr-down';
    return 'hx-wr-flat';
}

export function createChampionCard(champion, index, activeIds) {
    const heroId = champion['英雄 ID'] || '';
    const enName = champion['英文名'] || CHAMPION_PINYIN[champion['英雄名称']] || '';
    const wr = formatPercent(champion['英雄胜率']);
    const pr = formatPercent(champion['英雄出场率']);
    const heroName = String(champion['英雄名称'] || '');

    // detail URL query 契约（hero/wr/pr/id/en）被详情页与自动跳转依赖，逐字不动。
    const detailUrl = `detail.html?hero=${encodeURIComponent(heroName)}&wr=${encodeURIComponent(wr)}&pr=${encodeURIComponent(pr)}&id=${encodeURIComponent(heroId)}&en=${encodeURIComponent(enName)}`;

    const isHighlighted = activeIds.has(String(heroId)) ? 'hx-hero-avatar-highlight' : '';
    const wrClass = winRateClass(champion['英雄胜率']);

    let avatarUrl;
    if (heroId) {
        avatarUrl = `/assets/champions/${heroId}.png`;
    } else if (enName) {
        avatarUrl = `https://ddragon.leagueoflegends.com/cdn/${peekDdragonVersion()}/img/champion/${enName}.png`;
    } else {
        avatarUrl = '';
    }

    const safeHeroName = escapeHtml(heroName);
    const safeDetailUrl = escapeHtml(detailUrl);
    const safeAvatarUrl = escapeHtml(avatarUrl);
    const safeWr = escapeHtml(wr);
    const safePr = escapeHtml(pr);
    const avatarHiddenStyle = avatarUrl ? '' : 'visibility:hidden;';

    // 备战席高亮/预加载的 DOM 契约：.hx-hero-avatar 与 data-hero-id/name
    // 必须落在同一元素上（updateHighlights/bindChampionPreloads 依赖）。
    // hover 详情弹卡为纯 CSS，替代原 title 属性避免双 tooltip。
    return `
        <a
            href="${safeDetailUrl}"
            class="hx-hero-card cursor-pointer"
            style="animation-delay: ${index * 30}ms"
            data-en-name="${escapeHtml(String(enName || ''))}"
        >
            <div class="hx-hero-avatar relative rounded-lg overflow-hidden ${isHighlighted}"
                data-hero-name="${safeHeroName}"
                data-hero-id="${escapeHtml(String(heroId || ''))}"
            >
                <img loading="lazy" width="60" height="60"
                    src="${safeAvatarUrl}"
                    alt="${safeHeroName}"
                    class="w-full h-full object-cover bg-slate-800"
                    style="${avatarHiddenStyle}"
                    data-hero-name="${safeHeroName}"
                    onerror="this.removeAttribute('src'); this.style.visibility='hidden';"
                />
                <div class="hx-hero-wr text-2xs tabular-nums ${wrClass}">${safeWr}</div>
            </div>
            <span class="hx-hero-name text-2xs">${safeHeroName}</span>
            <span class="hx-hero-pop" aria-hidden="true">
                <b>${safeHeroName}</b>
                <span>胜率 <i class="${wrClass}">${safeWr}</i></span>
                <span>出场率 ${safePr}</span>
            </span>
        </a>
    `;
}

// 拼完整字符串后单次 innerHTML 赋值，避免循环内 += 的逐档重解析。
export function renderTiers(champions, { container, activeIds, onEmpty, afterRender }) {
    if (!container) return;

    if (champions.length === 0) {
        container.innerHTML = `
            <div class="text-center py-20 text-slate-300">
                <div class="font-bold text-xl">数据准备中</div>
                <div class="text-sm mt-2 text-slate-400">后台正在加载英雄数据，稍后刷新即可显示完整列表</div>
            </div>
        `;
        if (onEmpty) onEmpty();
        return;
    }

    const sorted = [...champions].sort((a, b) => b['综合分数'] - a['综合分数']);

    const tierGroups = {};
    TIERS.forEach((t) => { tierGroups[t.id] = []; });
    sorted.forEach((champ) => {
        const tier = TIER_BY_ID.get(String(champ['英雄评级'] || '').toUpperCase()) || TIER_BY_ID.get('T3');
        tierGroups[tier.id].push(champ);
    });

    const rows = [];
    TIERS.forEach((tier) => {
        const champs = tierGroups[tier.id];
        if (champs.length === 0) return;
        const champHTML = champs.map((c, i) => createChampionCard(c, i, activeIds)).join('');
        rows.push(`
            <div class="hx-tier-row hx-tier-group-shell rounded-2xl">
                <div class="hx-tier-label shrink-0 ${tier.cssClass} flex flex-col items-center justify-center font-bold text-xl shadow-[2px_0_10px_rgba(0,0,0,0.3)] z-10">
                    <span>${tier.name}</span>
                    <span class="text-2xs opacity-80 mt-1 uppercase tracking-wider">${tier.enName}</span>
                </div>
                <div class="hx-tier-champions flex-1 flex flex-wrap">
                    ${champHTML}
                </div>
            </div>
        `);
    });
    container.innerHTML = rows.join('');
    if (afterRender) afterRender();
}

// 备战席变化只增量切换高亮类，不再全量重建 DOM。
export function updateHighlights(container, activeIds) {
    if (!container) return;
    container.querySelectorAll('.hx-hero-avatar[data-hero-id]').forEach((anchor) => {
        const heroId = String(anchor.dataset.heroId || '');
        anchor.classList.toggle('hx-hero-avatar-highlight', activeIds.has(heroId));
    });
}

export function renderLoadFailure(container, unavailable) {
    if (!container) return;
    container.innerHTML = `
        <div class="text-center py-20 ${unavailable ? 'text-slate-300' : 'text-red-400'}">
            <div class="text-4xl mb-4">${unavailable ? '等待' : '警告'}</div>
            <div class="font-bold text-xl">${unavailable ? '统计快照准备中' : '数据加载失败'}</div>
            <div class="text-sm mt-2">${unavailable ? 'DataService 正在生成可用数据，请稍后刷新' : '请确保后端服务已启动'}</div>
        </div>
    `;
}
