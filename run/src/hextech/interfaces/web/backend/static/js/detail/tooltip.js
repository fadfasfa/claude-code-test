// 海克斯悬浮 tooltip：跟随鼠标、按阶级着色标题、textContent 安全渲染描述。

import { normalizeTierName } from './augments.js';

export function setupHextechTooltip(containers) {
    const tooltipEl = document.createElement('div');
    tooltipEl.className = 'hextech-tooltip';
    tooltipEl.innerHTML = `
        <div class="hextech-tooltip-header">
            <span class="hextech-tooltip-title" id="tt-title"></span>
        </div>
        <div class="hextech-tooltip-body text-gray-300 whitespace-pre-wrap text-[13px]" id="tt-body"></div>
    `;
    document.body.appendChild(tooltipEl);
    const ttTitle = tooltipEl.querySelector('#tt-title');
    const ttBody = tooltipEl.querySelector('#tt-body');

    // 描述一律走 textContent，杜绝 tooltip 数据里携带 HTML 的注入面
    function safelyRenderTooltipDesc(desc, containerEl) {
        containerEl.textContent = '';
        if (!desc) {
            const fallbackSpan = document.createElement('span');
            fallbackSpan.className = 'text-gray-500 text-xs';
            fallbackSpan.textContent = '暂无详细描述';
            containerEl.appendChild(fallbackSpan);
            return;
        }
        containerEl.textContent = String(desc);
    }

    containers.filter(Boolean).forEach((container) => {
        container.addEventListener('mouseover', (e) => {
            const target = e.target.closest('[data-hex-info]');
            if (!target || !container.contains(target)) return;

            const dataStr = target.getAttribute('data-hex-info');
            if (!dataStr) return;

            try {
                const info = JSON.parse(decodeURIComponent(dataStr));
                ttTitle.textContent = info.name || '';
                ttTitle.className = 'hextech-tooltip-title';
                const tier = normalizeTierName(info.tier || '');

                if (tier === 'Prismatic') {
                    ttTitle.classList.add('hextech-tooltip-tier-Prismatic');
                } else if (tier === 'Gold') {
                    ttTitle.classList.add('hextech-tooltip-tier-Gold');
                } else {
                    ttTitle.classList.add('hextech-tooltip-tier-Silver');
                }

                safelyRenderTooltipDesc(info.desc, ttBody);
                tooltipEl.classList.add('show');
            } catch (err) {
                console.error('Hextech tooltip JSON parse error:', err);
            }
        });

        container.addEventListener('mousemove', (e) => {
            if (!tooltipEl.classList.contains('show')) return;
            const offset = 15;
            let x = e.clientX + offset;
            let y = e.clientY + offset;
            const rect = tooltipEl.getBoundingClientRect();
            if (x + rect.width > window.innerWidth) x = e.clientX - rect.width - 10;
            if (y + rect.height > window.innerHeight) y = e.clientY - rect.height - 10;
            tooltipEl.style.left = x + 'px';
            tooltipEl.style.top = y + 'px';
        });

        container.addEventListener('mouseout', (e) => {
            const target = e.target.closest('[data-hex-info]');
            if (!target) return;
            const related = e.relatedTarget;
            if (related && target.contains(related)) return;
            tooltipEl.classList.remove('show');
        });
    });
}
