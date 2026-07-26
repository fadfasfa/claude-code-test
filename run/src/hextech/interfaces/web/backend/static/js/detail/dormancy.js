// 多标签休眠控制：BroadcastChannel 单活跃标签 + “双开此视口”白名单。
// onDormant/onReactivate 由入口注入（关 WS/清重试、重连并重载数据）。

export function createDormancyController({ onDormant, onReactivate }) {
    let tabId = Math.random().toString(36).substring(2, 9);
    const bc = new BroadcastChannel('hextech_nexus_channel');
    // 白名单集合：在该集合中的 tabId 不会触发对方休眠，用于"双开此视口"工作流
    const dormancyAllowList = new Set();

    function dormancyDormant() {
        if (document.getElementById('dormancyOverlay')) return;

        const overlay = document.createElement('div');
        overlay.id = 'dormancyOverlay';
        overlay.className = 'dormancy-overlay';
        overlay.innerHTML = `
            <div class="dormancy-card">
                <div class="dormancy-crystal-wrap">
                    <div class="dormancy-crystal"></div>
                </div>
                <h2 class="text-xl font-bold text-white tracking-widest mb-2">海克斯核心已转移</h2>
                <p class="text-xs text-slate-400 leading-relaxed mb-6">
                    已在其他悬浮窗/标签页中激活了新的枢纽，当前窗口已自动进入魔法休眠状态，以降低系统资源开销。
                </p>
                <div class="dormancy-btn-row">
                    <button type="button" id="btnReactivate" class="dormancy-btn">重新激活此窗口</button>
                    <button type="button" id="btnAllowDuo" class="dormancy-btn dormancy-btn-secondary">双开此视口</button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);
        onDormant();
    }

    function removeOverlay() {
        const overlay = document.getElementById('dormancyOverlay');
        if (overlay) {
            overlay.remove();
        }
    }

    function reactivateTab() {
        removeOverlay();
        tabId = Math.random().toString(36).substring(2, 9);
        bc.postMessage({ type: 'new_tab_opened', id: tabId });
        onReactivate();
    }

    // 双开模式：把本标签加入白名单广播给其他标签，对方收到后不再对本 tabId 触发休眠
    function allowDuoTab() {
        removeOverlay();
        dormancyAllowList.add(tabId);
        bc.postMessage({ type: 'allow_duo', id: tabId });
        onReactivate();
    }

    bc.onmessage = (e) => {
        if (e.data.type === 'new_tab_opened' && e.data.id !== tabId) {
            // 对方在白名单内则不强制本标签休眠
            if (dormancyAllowList.has(e.data.id)) return;
            dormancyDormant();
        } else if (e.data.type === 'allow_duo' && e.data.id !== tabId) {
            dormancyAllowList.add(e.data.id);
        }
    };

    document.addEventListener('click', (event) => {
        if (event.target && event.target.id === 'btnReactivate') {
            reactivateTab();
        } else if (event.target && event.target.id === 'btnAllowDuo') {
            allowDuoTab();
        }
    });

    return {
        // 入口 bootstrap 时广播本标签已打开，触发其他标签休眠
        announce() {
            bc.postMessage({ type: 'new_tab_opened', id: tabId });
        },
    };
}
