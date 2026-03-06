/**
 * Canvas 动态占位渲染引擎
 *
 * 职责：在本地资产和 CDN 均缺失时，通过 Canvas 生成确定性的占位符
 * - 英雄头像：根据 name 生成 HSL 渐变背景 + 首字母
 * - 海克斯图标：根据 tier 渲染对应的几何图案（棱彩=闪电，黄金=六边形，白银=圆形）
 *
 * 约束：
 * - 禁止外部闭包，保证纯粹性和复用性
 * - 原生 Canvas API，不使用第三方图形库
 * - 显式内存销毁，防止堆积
 */

/**
 * 确定性哈希函数：基于字符串生成整数
 * @param {string} str - 输入字符串
 * @returns {number} 0-255 之间的整数
 */
function deterministicHash(str) {
    let hash = 0;
    for (let i = 0; i < str.length; i++) {
        hash = ((hash << 5) - hash) + str.charCodeAt(i);
        hash = hash & hash; // Convert to 32-bit integer
    }
    return Math.abs(hash) % 256;
}

/**
 * 根据字符串生成确定性 HSL 色相（0-360 度）
 * @param {string} name - 输入名称
 * @returns {number} HSL 色相值 0-359
 */
function generateHue(name) {
    const hash = deterministicHash(name);
    return (hash * 360 / 256) | 0; // 转换为 0-359 范围
}

/**
 * 绘制 Canvas 渐变背景 + 文本
 * @param {HTMLImageElement} el - img 元素
 * @param {string} text - 显示文字（通常为首字母或缩写）
 * @param {number} hue - HSL 色相值 (0-360)
 * @param {string} mode - 'avatar'(头像) 或 'icon'(图标)
 */
function drawCanvasPlaceholder(el, text, hue, mode) {
    const size = mode === 'avatar' ? 80 : 32;

    // 创建离屏 Canvas
    const canvas = document.createElement('canvas');
    canvas.width = size;
    canvas.height = size;

    const ctx = canvas.getContext('2d');
    if (!ctx) return; // 环境不支持 Canvas

    // 绘制渐变背景
    const gradient = ctx.createLinearGradient(0, 0, size, size);
    gradient.addColorStop(0, `hsl(${hue}, 70%, 40%)`);
    gradient.addColorStop(1, `hsl(${(hue + 40) % 360}, 70%, 50%)`);

    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, size, size);

    // 绘制暗圆形背景层（增加对比度）
    ctx.fillStyle = 'rgba(0, 0, 0, 0.3)';
    ctx.beginPath();
    ctx.arc(size / 2, size / 2, size * 0.35, 0, Math.PI * 2);
    ctx.fill();

    // 绘制文字
    ctx.font = mode === 'avatar' ? 'bold 40px Arial' : 'bold 14px Arial';
    ctx.fillStyle = 'white';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, size / 2, size / 2);

    // 转换为 Data URL 并赋值给 img 元素
    el.src = canvas.toDataURL('image/png');

    // 显式清理 Canvas 内存（关键）
    canvas.width = 0;
    canvas.height = 0;
    ctx.clearRect(0, 0, 0, 0);
}

/**
 * 绘制海克斯几何图案
 * @param {HTMLImageElement} el - img 元素
 * @param {string} tier - 海克斯阶级字符串 (Prismatic|Gold|Silver)
 */
function drawHextechIcon(el, tier) {
    const size = 32;
    const canvas = document.createElement('canvas');
    canvas.width = size;
    canvas.height = size;

    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const center = size / 2;
    const lineWidth = 2;

    // 根据阶级绘制不同的几何图案
    if (tier.includes('棱彩') || tier.includes('彩') || tier === 'Prismatic') {
        // 棱彩阶：闪电符号
        ctx.strokeStyle = 'hsl(45, 100%, 60%)'; // 金黄色
        ctx.fillStyle = 'hsl(45, 100%, 50%)';
        ctx.lineWidth = lineWidth;
        ctx.lineCap = 'round';
        ctx.lineJoin = 'round';

        ctx.beginPath();
        ctx.moveTo(center, 6);
        ctx.lineTo(center + 6, center - 2);
        ctx.lineTo(center + 2, center + 2);
        ctx.lineTo(center + 8, center + 12);
        ctx.lineTo(center, center + 6);
        ctx.lineTo(center - 2, center + 2);
        ctx.lineTo(center - 8, center + 12);
        ctx.closePath();
        ctx.fill();
        ctx.stroke();

    } else if (tier.includes('金') || tier.includes('黄') || tier === 'Gold') {
        // 黄金阶：六边形
        ctx.strokeStyle = 'hsl(51, 100%, 50%)'; // 金色
        ctx.fillStyle = 'rgba(250, 204, 21, 0.2)';
        ctx.lineWidth = lineWidth;

        ctx.beginPath();
        for (let i = 0; i < 6; i++) {
            const angle = (Math.PI / 3) * i;
            const x = center + 10 * Math.cos(angle);
            const y = center + 10 * Math.sin(angle);
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        }
        ctx.closePath();
        ctx.fill();
        ctx.stroke();

    } else {
        // 白银阶：圆形 + 圆环
        ctx.strokeStyle = 'hsl(210, 20%, 60%)'; // 银色
        ctx.fillStyle = 'rgba(203, 213, 225, 0.1)';
        ctx.lineWidth = lineWidth;

        ctx.beginPath();
        ctx.arc(center, center, 10, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();

        // 绘制内圆
        ctx.strokeStyle = 'hsl(210, 20%, 70%)';
        ctx.beginPath();
        ctx.arc(center, center, 6, 0, Math.PI * 2);
        ctx.stroke();
    }

    el.src = canvas.toDataURL('image/png');

    // 显式清理内存
    canvas.width = 0;
    canvas.height = 0;
    ctx.clearRect(0, 0, 0, 0);
}

/**
 * 核心处理函数：根据资产类型和阶级生成占位符
 *
 * @param {HTMLImageElement} el - img DOM 元素
 * @param {string} name - 英雄名称或海克斯名称
 * @param {string} type - 资产类型 ('avatar'|'hextech')
 * @param {string} tier - 海克斯阶级或空字符串 (Prismatic|Gold|Silver)
 */
function handleAssetMissing(el, name, type, tier) {
    if (!el || !(el instanceof HTMLImageElement)) return;

    if (type === 'avatar') {
        // 英雄头像：使用 name 生成确定性 HSL 渐变
        const hue = generateHue(name || 'Unknown');
        const initial = (name || '?').charAt(0).toUpperCase();
        drawCanvasPlaceholder(el, initial, hue, 'avatar');

    } else if (type === 'hextech') {
        // 海克斯图标：根据 tier 渲染几何图案
        drawHextechIcon(el, tier);

    } else if (type === 'generic') {
        // 通用占位：使用 name 和 tier 的组合
        const text = (name || 'A').charAt(0).toUpperCase();
        const hue = generateHue(name || '');
        drawCanvasPlaceholder(el, text, hue, 'icon');
    }
}

/**
 * 导出公共接口（可选，用于全局访问）
 * 注：如果在 HTML <script> 中引入，自动进入全局作用域
 */
if (typeof window !== 'undefined') {
    window.handleAssetMissing = handleAssetMissing;
}
