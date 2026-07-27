// 详情页数据控制器：海克斯数据加载、加载中重试、视图切换与联动数据拉取。
// 页面状态（数据/视图模式/重试计时器）集中在本模块；initDetailPage 注入 URL 参数。
// 模块 import 阶段零副作用，可被 Node 测试直接 import 验证重试契约。

import { normalizeAugmentKey } from '../shared/text.js';
import { createHextechCard, resolveArticleAugment } from './augments.js';
import { updateFilteredSynergies } from './synergy.js';

const DETAIL_LOADING_RETRY_BASE_MS = 500;
const DETAIL_LOADING_RETRY_MAX_MS = 5000;
const DETAIL_LOADING_MAX_RETRIES = 12;

let API_BASE = '';
let hero = '';
let heroId = '';

let hextechData = null;
let currentView = 'all';
let currentMode = 'comprehensive';
let synergyData = null;
let synergyMeta = null;
let synergyLoaded = false;
let augmentIconMap = {};
let augmentCatalogMap = {};
let detailLoadingRetryTimer = null;
let detailPreloadRequested = false;

export function initDetailPage(options) {
    API_BASE = options.apiBase || '';
    hero = options.hero || '';
    heroId = options.heroId || '';
}

function resolveAugmentForArticle(item) {
    return resolveArticleAugment(item, {
        baseArray: hextechData ? (hextechData.comprehensive || []) : [],
        augmentIconMap,
        augmentCatalogMap,
    });
}

function refreshSynergyView() {
    const container = document.getElementById('synergyArticleScroll');
    if (!container) return;
    updateFilteredSynergies({
        container,
        tier: currentView,
        synergyData,
        synergyMeta,
        synergyLoaded,
        resolveAugment: resolveAugmentForArticle,
    });
}

export function renderCurrentView() {
    if (!hextechData) return;
    const compList = document.getElementById('compList');
    const dataContainer = document.getElementById('dataContainer');
    const noDataTip = document.getElementById('noDataTip');

    let leftData = [];

    const baseArray = currentMode === 'winrate_only' ? hextechData.winrate_only : hextechData.comprehensive;

    if (currentView === 'all') {
        leftData = baseArray || [];
    } else {
        const tierData = hextechData[currentView] || [];
        if (currentMode === 'winrate_only') {
            leftData = tierData.slice().sort((a, b) => b.海克斯胜率 - a.海克斯胜率);
        } else {
            leftData = tierData.slice().sort((a, b) => (b.综合得分 || 0) - (a.综合得分 || 0));
        }
    }

    if (leftData.length === 0) {
        dataContainer.classList.add('hidden');
        noDataTip.classList.remove('hidden');
    } else {
        noDataTip.classList.add('hidden');
        dataContainer.classList.remove('hidden');
        compList.innerHTML = leftData.map((h, i) => createHextechCard(h, i)).join('');

        refreshSynergyView();
    }
}

export function setDisplayMode(mode) {
    currentMode = mode;
    document.querySelectorAll('.mode-btn').forEach((btn) => {
        btn.className = 'mode-btn hx-tab-idle px-4 py-2 rounded-lg font-medium text-sm';
    });
    const activeBtn = mode === 'comprehensive' ? document.getElementById('btn-comprehensive') : document.getElementById('btn-winrate');
    if (activeBtn) {
        activeBtn.className = 'mode-btn hx-tab-active px-4 py-2 rounded-lg text-sm';
    }
    renderCurrentView();
}

export function switchView(view) {
    currentView = view;
    document.querySelectorAll('.tab-btn').forEach((btn) => {
        btn.className = 'tab-btn hx-tab-idle px-5 py-2 rounded-lg font-medium';
    });
    const activeBtn = document.getElementById(`tab-${view}`);
    if (activeBtn) {
        activeBtn.className = 'tab-btn hx-tab-active px-5 py-2 rounded-lg';
    }
    renderCurrentView();
}

export async function loadSynergies() {
    try {
        const synergyRes = await fetch(`${API_BASE}/api/synergies/${encodeURIComponent(heroId || hero)}`);
        const synergyResult = await synergyRes.json();
        synergyMeta = synergyResult && typeof synergyResult === 'object' ? synergyResult : null;
        synergyData = Array.isArray(synergyResult.synergy_items) && synergyResult.synergy_items.length > 0
            ? synergyResult.synergy_items
            : (synergyResult.synergies || []);
        synergyLoaded = true;
        refreshSynergyView();
    } catch (err) {
        console.error(err);
        synergyMeta = { status: 'error', message: '联动数据读取失败' };
        synergyData = [];
        synergyLoaded = true;
        refreshSynergyView();
    }
}

export async function loadAugmentIconMap() {
    try {
        const [iconResponse, manifestResponse] = await Promise.all([
            fetch(`${API_BASE}/api/augment_icon_map`).catch(() => null),
            fetch('/catalog/Augment_Icon_Manifest.json', { cache: 'no-store' }).catch(() => null),
        ]);

        if (iconResponse && iconResponse.ok) {
            const payload = await iconResponse.json();
            augmentIconMap = payload && typeof payload === 'object' ? payload : {};
        }

        if (manifestResponse && manifestResponse.ok) {
            const manifest = await manifestResponse.json();
            if (Array.isArray(manifest)) {
                const catalog = {};
                manifest.forEach((item) => {
                    if (!item || !item.name) return;
                    const icon = item.filename
                        ? `/assets/augments/${encodeURIComponent(item.filename)}`
                        : (item.icon_url || augmentIconMap[item.name] || '');
                    catalog[normalizeAugmentKey(item.name)] = {
                        name: item.name,
                        icon,
                        tier: item.tier || '',
                        tooltip_plain: item.tooltip_plain || '',
                        description: item.description || '',
                    };
                });
                augmentCatalogMap = catalog;
            }
        }
    } catch (_) {
        augmentIconMap = {};
        augmentCatalogMap = {};
    }
}

function detailRetryDelayMs(retryCount) {
    return Math.min(
        DETAIL_LOADING_RETRY_MAX_MS,
        DETAIL_LOADING_RETRY_BASE_MS * Math.pow(2, Math.min(retryCount, 4))
    );
}

export function describeLoadingStatus(payload) {
    const startup = payload && payload.startup_status ? payload.startup_status : {};
    const preload = payload && payload.preload_status ? payload.preload_status : {};
    const messages = [
        preload.last_error,
        preload.error,
        preload.reason,
        startup.last_error,
        startup.hextech_warning,
        startup.bundle_manifest && startup.bundle_manifest.warning,
    ].filter(Boolean);
    return messages.length > 0 ? String(messages[0]) : '详情页已打开，海克斯数据会在后台补齐后可用';
}

export function scheduleDetailRetry(retryCount) {
    if (detailLoadingRetryTimer) {
        return;
    }
    if (retryCount >= DETAIL_LOADING_MAX_RETRIES) {
        document.querySelector('#noDataTip .text-sm').textContent = '数据准备时间较长，请稍后刷新页面';
        return;
    }
    detailLoadingRetryTimer = window.setTimeout(() => {
        detailLoadingRetryTimer = null;
        loadHextechs(retryCount + 1);
    }, detailRetryDelayMs(retryCount));
}

export function clearDetailRetry() {
    if (!detailLoadingRetryTimer) {
        return;
    }
    window.clearTimeout(detailLoadingRetryTimer);
    detailLoadingRetryTimer = null;
}

export function requestDetailPreload() {
    if (detailPreloadRequested || !hero) {
        return;
    }
    detailPreloadRequested = true;
    fetch(`${API_BASE}/api/champion/${encodeURIComponent(hero)}/preload`, {
        method: 'POST',
        credentials: 'same-origin',
    }).catch(() => {});
}

export async function loadHextechs(retryCount = 0) {
    if (!hero) {
        clearDetailRetry();
        document.getElementById('skeletonContainer').classList.add('hidden');
        const spinner = document.getElementById('loadingSpinner');
        spinner.classList.remove('hidden');
        spinner.innerHTML = '<div class="text-red-400">错误：未获取到英雄名称</div>';
        return;
    }
    try {
        const hextechRes = await fetch(`${API_BASE}/api/champion/${encodeURIComponent(hero)}/hextechs`);
        hextechData = await hextechRes.json();
        const activeArray = hextechData.comprehensive || [];
        synergyData = [];
        synergyMeta = null;
        synergyLoaded = false;

        document.getElementById('loadingSpinner').classList.add('hidden');
        document.getElementById('skeletonContainer').classList.add('hidden');
        renderCurrentView();
        // 图标目录延后加载：先渲染数据，图标就绪后再补一次渲染
        loadAugmentIconMap().then(() => {
            if (hextechData) {
                renderCurrentView();
            }
        }).catch(() => {});
        if (hextechData.loading && activeArray.length === 0) {
            document.getElementById('noDataTip').classList.remove('hidden');
            document.querySelector('#noDataTip .text-xl').textContent = '数据准备中';
            document.querySelector('#noDataTip .text-sm').textContent = describeLoadingStatus(hextechData);
            requestDetailPreload();
            scheduleDetailRetry(retryCount);
            return;
        }
        clearDetailRetry();
        loadSynergies();
    } catch (err) {
        console.error(err);
        clearDetailRetry();
        document.getElementById('loadingSpinner').classList.add('hidden');
        document.getElementById('skeletonContainer').classList.add('hidden');
        document.getElementById('noDataTip').classList.remove('hidden');
        document.querySelector('#noDataTip .text-xl').textContent = '数据加载失败';
    }
}
