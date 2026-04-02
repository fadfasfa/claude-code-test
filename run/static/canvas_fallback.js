
function deterministicHash(str) {
    let hash = 0;
    for (let i = 0; i < str.length; i++) {
        hash = ((hash << 5) - hash) + str.charCodeAt(i);
        hash = hash & hash;
    }
    return Math.abs(hash) % 256;
}

function generateHue(name) {
    const hash = deterministicHash(name);
    return (hash * 360 / 256) | 0;
}

function drawCanvasPlaceholder(el, text, hue, mode) {
    const size = mode === 'avatar' ? 80 : 32;

    const canvas = document.createElement('canvas');
    canvas.width = size;
    canvas.height = size;

    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const gradient = ctx.createLinearGradient(0, 0, size, size);
    gradient.addColorStop(0, `hsl(${hue}, 70%, 40%)`);
    gradient.addColorStop(1, `hsl(${(hue + 40) % 360}, 70%, 50%)`);

    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, size, size);

    ctx.fillStyle = 'rgba(0, 0, 0, 0.3)';
    ctx.beginPath();
    ctx.arc(size / 2, size / 2, size * 0.35, 0, Math.PI * 2);
    ctx.fill();

    ctx.font = mode === 'avatar' ? 'bold 40px Arial' : 'bold 14px Arial';
    ctx.fillStyle = 'white';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, size / 2, size / 2);

    el.src = canvas.toDataURL('image/png');

    canvas.width = 0;
    canvas.height = 0;
    ctx.clearRect(0, 0, 0, 0);
}

function drawHextechIcon(el, tier) {
    const size = 32;
    const canvas = document.createElement('canvas');
    canvas.width = size;
    canvas.height = size;

    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const center = size / 2;
    const lineWidth = 2;

    if (tier.includes('棱彩') || tier.includes('彩') || tier === 'Prismatic') {
        ctx.strokeStyle = 'hsl(45, 100%, 60%)';
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
        ctx.strokeStyle = 'hsl(51, 100%, 50%)';
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
        ctx.strokeStyle = 'hsl(210, 20%, 60%)';
        ctx.fillStyle = 'rgba(203, 213, 225, 0.1)';
        ctx.lineWidth = lineWidth;

        ctx.beginPath();
        ctx.arc(center, center, 10, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();

        ctx.strokeStyle = 'hsl(210, 20%, 70%)';
        ctx.beginPath();
        ctx.arc(center, center, 6, 0, Math.PI * 2);
        ctx.stroke();
    }

    el.src = canvas.toDataURL('image/png');

    canvas.width = 0;
    canvas.height = 0;
    ctx.clearRect(0, 0, 0, 0);
}

function handleAssetMissing(el, name, type, tier) {
    if (!el || !(el instanceof HTMLImageElement)) return;

    if (type === 'avatar') {
        const hue = generateHue(name || 'Unknown');
        const initial = (name || '?').charAt(0).toUpperCase();
        drawCanvasPlaceholder(el, initial, hue, 'avatar');

    } else if (type === 'hextech') {
        drawHextechIcon(el, tier);

    } else if (type === 'generic') {
        const text = (name || 'A').charAt(0).toUpperCase();
        const hue = generateHue(name || '');
        drawCanvasPlaceholder(el, text, hue, 'icon');
    }
}

if (typeof window !== 'undefined') {
    window.handleAssetMissing = handleAssetMissing;
}
