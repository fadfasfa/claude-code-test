// 搜索/海克斯名称归一化：小写并剥离分隔符号。
// 两个归一化器共用同一工厂，避免 index 与 detail 各自维护正则漂移。

export function makeNormalizer(extraChars = '') {
    const pattern = new RegExp(`[\\s·_\\-()/【】\\[\\]（）${extraChars}]`, 'g');
    return (value) => String(value || '')
        .toLowerCase()
        .replace(pattern, '')
        .trim();
}

// 英雄搜索归一化（保留冒号引号等，历史行为）
export const normalizeSearchText = makeNormalizer();

// 海克斯名称归一化：额外剥离冒号与引号
export const normalizeAugmentKey = makeNormalizer(':：\'"');
