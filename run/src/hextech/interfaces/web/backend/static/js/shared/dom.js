// 通用 DOM 工具：HTML 转义与输入防抖。
// 调用方: index/detail 各模块; 无副作用，可在 Node 环境安全 import。

export function escapeHtml(value) {
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// 返回带 cancel() 的防抖包装；等待期内重复调用只保留最后一次。
export function debounce(fn, waitMs) {
    let timer = null;
    const wrapped = (...args) => {
        if (timer !== null) clearTimeout(timer);
        timer = setTimeout(() => {
            timer = null;
            fn(...args);
        }, waitMs);
    };
    wrapped.cancel = () => {
        if (timer !== null) {
            clearTimeout(timer);
            timer = null;
        }
    };
    return wrapped;
}
