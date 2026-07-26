// 详情页入口模块：URL 参数解析、页头渲染、WS、休眠控制与 tab 事件委托装配。
// 全部顶层副作用收敛在 bootstrapDetailPage()；数据与视图逻辑在 detail/ 子模块。

import { apiBase, wsUrl, createReconnectingWS } from './shared/net.js';
import { peekDdragonVersion, refreshDdragonVersion } from './shared/ddragon.js';
import {
    clearDetailRetry,
    initDetailPage,
    loadHextechs,
    setDisplayMode,
    switchView,
} from './detail/loading.js';
import { createDormancyController } from './detail/dormancy.js';
import { setupHextechTooltip } from './detail/tooltip.js';

const API_BASE = apiBase();

let wsController = null;
let dormancy = null;

function renderHeroHeader({ hero, enName, heroId, heroWR, heroPR, isAuto }) {
    if (hero) document.getElementById('heroName').textContent = hero;
    if (heroWR) document.getElementById('heroWR').textContent = heroWR;
    if (heroPR) document.getElementById('heroPR').textContent = heroPR;

    if (heroId) {
        // 资产路由要求 champions/ 前缀；本地缺图时后端会 307 回退 DDragon。
        document.getElementById('heroAvatar').src = `/assets/champions/${heroId}.png`;
    } else if (enName) {
        document.getElementById('heroAvatar').src = `https://ddragon.leagueoflegends.com/cdn/${peekDdragonVersion()}/img/champion/${enName}.png`;
    } else {
        document.getElementById('headerBg').style.backgroundColor = '#181825';
        document.getElementById('heroAvatar').removeAttribute('src');
    }

    if (enName) {
        document.getElementById('headerBg').style.backgroundImage = `url('https://ddragon.leagueoflegends.com/cdn/img/champion/splash/${enName}_0.jpg')`;
    }

    if (isAuto === '1') {
        document.getElementById('mainHeader').classList.add('anim-auto-lock');
    }
}

// 模式/阶级按钮统一走事件委托；ESM 作用域下不再暴露全局 onclick 函数
function bindViewControls() {
    const controlBar = document.querySelector('.detail-control-bar');
    if (!controlBar) return;
    controlBar.addEventListener('click', (event) => {
        const modeBtn = event.target.closest('[data-mode]');
        if (modeBtn) {
            setDisplayMode(modeBtn.dataset.mode);
            return;
        }
        const viewBtn = event.target.closest('[data-view]');
        if (viewBtn) {
            switchView(viewBtn.dataset.view);
        }
    });
}

function connectWS() {
    const status = document.getElementById('wsStatus');
    wsController = createReconnectingWS({
        url: wsUrl() + '/ws',
        onOpen: () => {
            status.innerHTML = '<span class="w-2 h-2 bg-green-500 rounded-full animate-pulse"></span><span class="text-green-400">已连接</span>';
        },
        onClose: () => {
            status.innerHTML = '<span class="w-2 h-2 bg-red-500 rounded-full"></span><span class="text-red-400">已断开</span>';
        },
        onMessage: (msg) => {
            if (msg.type === 'data_updated') {
                console.log('收到 CSV 数据更新通知，自动热重载...');
                loadHextechs();
                const degraded = Array.isArray(msg.degraded_sources) ? msg.degraded_sources : [];
                if (degraded.length) {
                    status.innerHTML = `<span class="w-2 h-2 bg-amber-500 rounded-full"></span><span class="text-amber-300">沿用 ${degraded.join('/')}</span>`;
                }
            } else if (msg.type === 'local_player_locked') {
                const enName = msg.en_name ? `&en=${encodeURIComponent(msg.en_name)}` : '';
                const detailFirst = msg.detail_first ? '&detailFirst=1' : '';
                window.location.href = `detail.html?hero=${encodeURIComponent(msg.hero_name)}&id=${encodeURIComponent(msg.champion_id)}${enName}&auto=1${detailFirst}`;
            }
        },
    });
}

let detailBootstrapped = false;
function bootstrapDetailPage() {
    if (detailBootstrapped) return;
    detailBootstrapped = true;

    refreshDdragonVersion();

    const urlParams = new URLSearchParams(window.location.search);
    const hero = urlParams.get('hero');
    const enName = urlParams.get('en');
    const heroId = urlParams.get('id');

    renderHeroHeader({
        hero,
        enName,
        heroId,
        heroWR: urlParams.get('wr'),
        heroPR: urlParams.get('pr'),
        isAuto: urlParams.get('auto'),
    });

    initDetailPage({ apiBase: API_BASE, hero, heroId });
    bindViewControls();

    dormancy = createDormancyController({
        // 休眠：停 WS 重连并清掉加载中重试，彻底静默本标签
        onDormant: () => {
            if (wsController) wsController.close();
            clearDetailRetry();
        },
        onReactivate: () => {
            connectWS();
            loadHextechs();
        },
    });
    dormancy.announce();

    setupHextechTooltip([
        document.getElementById('compList'),
        document.getElementById('synergyArticleScroll'),
    ]);

    loadHextechs();
    connectWS();
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootstrapDetailPage, { once: true });
} else {
    bootstrapDetailPage();
}
