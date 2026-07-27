// 首页入口模块：装配数据加载、T 度榜渲染、搜索控制器、WS 与提示气泡。
// 页面状态（英雄列表/备战席高亮）集中在本文件；渲染与搜索逻辑在 index/ 子模块。

import { apiBase, wsUrl, createReconnectingWS } from './shared/net.js';
import { refreshDdragonVersion } from './shared/ddragon.js';
import { renderTiers, renderLoadFailure, updateHighlights, formatPercent } from './index/render.js';
import { createSearchController, resolveAliasRecords } from './index/search.js';
import { bindChampionPreloads } from './index/preload.js';
import { CHAMPION_PINYIN } from './index/champion_pinyin.js';

const API_BASE = apiBase();

let allChampions = [];
let activeChampionIds = new Set();
let wsController = null;

const tierContainer = document.getElementById('tierContainer');
const wsStatus = document.getElementById('wsStatus');

const search = createSearchController({
    input: document.getElementById('searchInput'),
    overlay: document.getElementById('searchOverlay'),
    onRender: renderView,
});

function renderView(champions) {
    renderTiers(champions, {
        container: tierContainer,
        activeIds: activeChampionIds,
        afterRender: () => bindChampionPreloads(API_BASE),
    });
}

async function loadChampions() {
    try {
        const [championRes, aliasRes] = await Promise.all([
            fetch(`${API_BASE}/api/champions`),
            fetch(`${API_BASE}/api/champion_aliases`).catch(() => null),
        ]);
        if (!championRes.ok) {
            const failure = await championRes.json().catch(() => ({}));
            if (championRes.status === 503 && failure.error === 'snapshot_unavailable') {
                throw new Error('snapshot_unavailable');
            }
            throw new Error(`HTTP ${championRes.status}`);
        }
        allChampions = await championRes.json();
        const primaryRecords = aliasRes && aliasRes.ok ? await aliasRes.json() : null;
        const aliasRecords = await resolveAliasRecords({
            apiBase: API_BASE,
            primaryRecords,
            champions: allChampions,
        });
        search.setChampions(allChampions, aliasRecords);
        console.log('[INFO] 加载英雄数据成功，共', allChampions.length, '个英雄');
        // 按当前查询重放视图：无查询时等价于全量渲染
        search.reapply();
    } catch (err) {
        console.error('[ERROR] 加载英雄数据失败:', err);
        const unavailable = err && err.message === 'snapshot_unavailable';
        renderLoadFailure(tierContainer, unavailable);
    }
}

// 右上角短暂气泡，告知玩家备战席或数据已自动刷新；同时只挂一个 DOM 节点
let updateToastTimer = null;
function showUpdateToast(text) {
    let toast = document.getElementById('hxUpdateToast');
    if (!toast) {
        toast = document.createElement('div');
        toast.id = 'hxUpdateToast';
        toast.className = 'hx-update-toast';
        document.body.appendChild(toast);
    }
    toast.textContent = text;
    requestAnimationFrame(() => toast.classList.add('is-visible'));
    if (updateToastTimer) clearTimeout(updateToastTimer);
    updateToastTimer = setTimeout(() => toast.classList.remove('is-visible'), 1500);
}

// 首启引导气泡：用 localStorage 判定首次，仅在 WS 第一次连通时弹出，5s 自动消失
function showOnboardingToastOnce() {
    try {
        if (localStorage.getItem('hextech_onboarded') === '1') return;
    } catch (_e) {
        // 隐私模式下 localStorage 不可用，干脆跳过引导，避免重复打扰
        return;
    }
    const toast = document.createElement('div');
    toast.className = 'hx-onboarding-toast';
    toast.innerHTML = `
        <button type="button" class="close" aria-label="关闭">✕</button>
        <div class="title">海克斯核心共鸣成功</div>
        <div class="body">已自动锁定本地备战席。可按 <kbd class="hx-search-hint">/</kbd> 或 <kbd class="hx-search-hint">Ctrl+K</kbd> 唤起搜索框。</div>
    `;
    document.body.appendChild(toast);
    requestAnimationFrame(() => toast.classList.add('is-visible'));
    const dismiss = () => {
        toast.classList.remove('is-visible');
        setTimeout(() => toast.remove(), 320);
        try { localStorage.setItem('hextech_onboarded', '1'); } catch (_e) {}
    };
    toast.querySelector('.close').addEventListener('click', dismiss);
    setTimeout(dismiss, 5000);
}

function handleWsMessage(message) {
    console.log('[WS] 收到消息:', message);

    if (message.type === 'champion_update') {
        activeChampionIds = new Set(message.champion_ids.map(String));
        // 备战席变化只做增量高亮，不再全量重建列表
        updateHighlights(tierContainer, activeChampionIds);
        showUpdateToast('备战席已更新');
    } else if (message.type === 'data_updated') {
        console.log('[WS] 数据已更新，重新加载...');
        loadChampions();
        const degraded = Array.isArray(message.degraded_sources) ? message.degraded_sources : [];
        showUpdateToast(degraded.length ? `数据已刷新 · 沿用 ${degraded.join('/')}` : '数据已刷新');
    } else if (message.type === 'local_player_locked') {
        const heroName = message.hero_name;
        const championId = message.champion_id;
        const detailFirst = message.detail_first ? '&detailFirst=1' : '';
        const heroData = allChampions.find((c) => c['英雄名称'] === heroName);
        if (heroData) {
            const wr = formatPercent(heroData['英雄胜率']);
            const pr = formatPercent(heroData['英雄出场率']);
            const enName = heroData['英文名'] || CHAMPION_PINYIN[heroName] || '';
            const heroId = heroData['英雄 ID'] || '';

            console.log('[WS] 本地玩家锁定:', heroName, '-> 直接跳转详情页');
            window.location.href = `detail.html?hero=${encodeURIComponent(heroName)}&wr=${encodeURIComponent(wr)}&pr=${encodeURIComponent(pr)}&id=${encodeURIComponent(heroId)}&en=${encodeURIComponent(enName)}&auto=1${detailFirst}`;
        } else if (heroName) {
            window.location.href = `detail.html?hero=${encodeURIComponent(heroName)}&id=${encodeURIComponent(championId)}&auto=1${detailFirst}`;
        }
    }
}

function connectWS() {
    wsController = createReconnectingWS({
        url: wsUrl() + '/ws',
        onMessage: handleWsMessage,
        onOpen: () => {
            wsStatus.innerHTML = '<span class="w-2 h-2 bg-hx-win rounded-full animate-pulse"></span><span class="text-hx-win">已连接</span>';
            // 首次握手成功展示一次新手引导气泡，之后不再打扰
            showOnboardingToastOnce();
        },
        onClose: () => {
            wsStatus.innerHTML = '<span class="w-2 h-2 bg-hx-loss rounded-full"></span><span class="text-hx-loss">已断开</span>';
        },
    });
}

let indexBootstrapped = false;
function bootstrapIndexPage() {
    if (indexBootstrapped) return;
    indexBootstrapped = true;
    refreshDdragonVersion();
    loadChampions();
    connectWS();
    search.setup();
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootstrapIndexPage, { once: true });
} else {
    bootstrapIndexPage();
}
