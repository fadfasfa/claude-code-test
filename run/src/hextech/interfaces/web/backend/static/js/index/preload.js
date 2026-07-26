// 英雄卡片 hover 预加载：悬停 500ms 后请求后端预热该英雄详情。
// 幂等绑定（data-preload-bound），重复渲染后可安全重复调用。

const PRELOAD_HOVER_DELAY_MS = 500;

function scheduleChampionPreload(anchor, apiBase) {
    if (!anchor || anchor.dataset.preloadState === 'done') return;

    const heroName = (anchor.dataset.heroName || '').trim();
    if (!heroName) return;

    if (anchor.dataset.preloadTimer) {
        window.clearTimeout(Number(anchor.dataset.preloadTimer));
    }

    const timer = window.setTimeout(async () => {
        anchor.dataset.preloadState = 'done';
        delete anchor.dataset.preloadTimer;
        try {
            await fetch(`${apiBase}/api/champion/${encodeURIComponent(heroName)}/preload`, {
                method: 'POST',
            });
        } catch (_) {
        }
    }, PRELOAD_HOVER_DELAY_MS);

    anchor.dataset.preloadTimer = String(timer);

    const cancel = () => {
        if (!anchor.dataset.preloadTimer) return;
        window.clearTimeout(Number(anchor.dataset.preloadTimer));
        delete anchor.dataset.preloadTimer;
    };

    anchor.addEventListener('mouseleave', cancel, { once: true });
    anchor.addEventListener('blur', cancel, { once: true });
}

export function bindChampionPreloads(apiBase, root = document) {
    root.querySelectorAll('.hx-hero-avatar[data-hero-name]').forEach((anchor) => {
        if (anchor.dataset.preloadBound === 'true') return;
        anchor.dataset.preloadBound = 'true';
        anchor.addEventListener('mouseenter', () => scheduleChampionPreload(anchor, apiBase), { passive: true });
        anchor.addEventListener('focus', () => scheduleChampionPreload(anchor, apiBase), { passive: true });
    });
}
