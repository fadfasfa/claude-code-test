// 首页搜索控制器：快捷词目录、输入建议面板、未命中引导面板与过滤逻辑。
// 本轮把此前从未接线的建议面板/未命中面板正式接通（经用户批准）。
// IME（compositionstart/end）时序与 blur 锁保持历史行为不变。

import { escapeHtml, debounce } from '../shared/dom.js';
import { normalizeSearchText } from '../shared/text.js';
import { CHAMPION_PINYIN } from './champion_pinyin.js';

const SEARCH_DEBOUNCE_MS = 150;

function matchesSearchToken(token, normalizedQuery) {
    if (!token || !normalizedQuery) {
        return false;
    }
    if (token === normalizedQuery) {
        return true;
    }
    if (normalizedQuery.length === 1) {
        return token.includes(normalizedQuery);
    }
    if (normalizedQuery.length <= 3) {
        return token.startsWith(normalizedQuery);
    }
    return token.includes(normalizedQuery);
}

function buildChampionCoreIndex(champions) {
    const index = new Map();
    champions.forEach((champ) => {
        const heroName = normalizeSearchText(champ['英雄名称'] || '');
        const enName = normalizeSearchText(champ['英文名'] || '');
        const heroId = String(champ['英雄 ID'] || '');
        const entry = {
            heroName: champ['英雄名称'] || '',
            enName: champ['英文名'] || '',
            heroId,
        };
        if (heroName) index.set(heroName, entry);
        if (enName) index.set(enName, entry);
        if (heroId) index.set(heroId, entry);
    });
    return index;
}

async function loadJsonFromCandidates(paths) {
    for (const path of paths) {
        try {
            const response = await fetch(path, { cache: 'no-store' });
            if (!response.ok) continue;
            return await response.json();
        } catch (_) {}
    }
    return null;
}

// 别名目录兜底链：API → 静态 catalog → 内置最小映射
export async function resolveAliasRecords({ apiBase, primaryRecords, champions }) {
    if (Array.isArray(primaryRecords) && primaryRecords.length > 0) {
        return primaryRecords;
    }

    const aliasIndex = await loadJsonFromCandidates([
        `${apiBase}/api/champion_aliases`,
        '/catalog/Champion_Alias_Index.json',
    ]);
    if (Array.isArray(aliasIndex) && aliasIndex.length > 0) {
        return aliasIndex;
    }

    const coreData = await loadJsonFromCandidates(['/catalog/Champion_Core_Data.json']);
    if (coreData && typeof coreData === 'object') {
        const records = Object.entries(coreData).map(([heroId, entry]) => ({
            title: entry.name || '',
            heroName: entry.title || '',
            enName: entry.en_name || '',
            heroId: String(entry.id || entry.hero_id || heroId || ''),
            aliases: Array.isArray(entry.aliases) ? entry.aliases : [],
        }));
        if (records.length > 0) {
            return records;
        }
    }

    const fallback = [
        ['潘森', ['ps', 'panshen', 'pantheon', '不屈之枪']],
        ['亚索', ['ys', 'yasuo', '快乐风男', '疾风剑豪']],
    ];
    return fallback.map(([heroName, aliases]) => {
        const champ = champions.find((item) => normalizeSearchText(item['英雄名称'] || '') === normalizeSearchText(heroName));
        return {
            title: heroName,
            heroName,
            enName: champ ? (champ['英文名'] || '') : '',
            heroId: champ ? (champ['英雄 ID'] || '') : '',
            aliases,
        };
    });
}

export function createSearchController({ input, assistPanel, overlay, onRender }) {
    let allChampions = [];
    let searchShortcutCatalog = [];
    let championSearchIndex = new Map();
    let championCoreIndex = new Map();
    let currentSearchQuery = '';
    let overlayBlurLockUntil = 0;

    function buildSearchShortcutCatalog(champions, aliasRecords) {
        const catalog = [];
        const seen = new Set();
        const championByEn = new Map();
        const championByName = new Map();
        championSearchIndex = new Map();

        champions.forEach((champ) => {
            const heroName = champ['英雄名称'] || '';
            const enName = champ['英文名'] || '';
            const heroId = champ['英雄 ID'] || '';
            const popularity = Number(champ['综合分数'] || 0);
            const normalizedHeroName = normalizeSearchText(heroName);
            const normalizedEnName = normalizeSearchText(enName);
            const searchTerms = new Set();

            if (heroName) {
                const key = `name:${heroName}`;
                if (!seen.has(key)) {
                    seen.add(key);
                    catalog.push({
                        label: heroName,
                        query: heroName,
                        meta: enName ? `英雄名 · ${enName}` : '英雄名',
                        heroName,
                        popularity,
                        kind: '英雄名',
                        heroId,
                    });
                }
                championByName.set(normalizedHeroName, champ);
                searchTerms.add(normalizedHeroName);
            }

            if (enName) {
                const key = `en:${enName}`;
                if (!seen.has(key)) {
                    seen.add(key);
                    catalog.push({
                        label: enName,
                        query: heroName || enName,
                        meta: heroName ? `英文名 · ${heroName}` : '英文名',
                        heroName,
                        popularity: popularity - 0.1,
                        kind: '英文名',
                        heroId,
                    });
                }
                championByEn.set(normalizedEnName, champ);
                championByName.set(normalizedEnName, champ);
                searchTerms.add(normalizedEnName);
            }

            championSearchIndex.set(normalizedHeroName || normalizedEnName || heroId, searchTerms);
        });

        const aliasSource = Array.isArray(aliasRecords) ? aliasRecords : [];
        aliasSource.forEach((record) => {
            const heroName = String(record.heroName || '').trim();
            const enName = String(record.enName || '').trim();
            const heroId = String(record.heroId || '').trim();
            const aliases = Array.isArray(record.aliases) ? record.aliases : [];
            const titleName = String(record.title || '').trim();
            const champ = championByName.get(normalizeSearchText(heroName)) ||
                championByEn.get(normalizeSearchText(enName)) ||
                championCoreIndex.get(normalizeSearchText(titleName));
            const canonicalHeroName = champ ? (champ['英雄名称'] || heroName || enName) : (heroName || enName);
            const canonicalEnName = champ ? (champ['英文名'] || enName) : enName;
            const canonicalHeroId = champ ? (champ['英雄 ID'] || heroId) : heroId;
            const popularity = champ ? Number(champ['综合分数'] || 0) + 1.5 : 0.3;
            const heroKey = normalizeSearchText(canonicalHeroName || canonicalEnName || heroId);
            const searchTerms = championSearchIndex.get(heroKey) || new Set();

            if (canonicalHeroName) searchTerms.add(normalizeSearchText(canonicalHeroName));
            if (canonicalEnName) searchTerms.add(normalizeSearchText(canonicalEnName));
            if (titleName) {
                const normalizedTitle = normalizeSearchText(titleName);
                searchTerms.add(normalizedTitle);
                const titleKey = `title:${heroKey}:${normalizedTitle}`;
                if (!seen.has(titleKey)) {
                    seen.add(titleKey);
                    catalog.push({
                        label: titleName,
                        query: titleName,
                        meta: canonicalHeroName ? `称号 · ${canonicalHeroName}` : '称号',
                        heroName: canonicalHeroName,
                        popularity,
                        kind: '称号',
                        heroId: canonicalHeroId,
                    });
                }
            }

            aliases.forEach((alias) => {
                const normalizedAlias = normalizeSearchText(alias);
                if (!normalizedAlias) return;
                searchTerms.add(normalizedAlias);
                const key = `alias:${heroKey}:${normalizedAlias}`;
                if (seen.has(key)) return;
                seen.add(key);
                catalog.push({
                    label: alias,
                    query: canonicalHeroName || alias,
                    meta: canonicalHeroName ? `别名 · ${canonicalHeroName}` : '别名',
                    heroName: canonicalHeroName,
                    popularity,
                    kind: '别名',
                    heroId: canonicalHeroId,
                });
            });

            championSearchIndex.set(heroKey, searchTerms);
        });

        Object.entries(CHAMPION_PINYIN).forEach(([shortcut, enName]) => {
            const champ = championByEn.get(normalizeSearchText(enName));
            const heroName = champ ? (champ['英雄名称'] || '') : '';
            const heroId = champ ? (champ['英雄 ID'] || '') : '';
            const key = `shortcut:${shortcut}->${heroName || enName}`;
            if (!seen.has(key)) {
                seen.add(key);
                catalog.push({
                    label: shortcut,
                    query: heroName || shortcut,
                    meta: heroName ? `快捷词 · ${heroName}` : `快捷词 · ${enName}`,
                    heroName,
                    popularity: heroName ? (champ['综合分数'] || 0) + 1.5 : 0.2,
                    kind: '快捷词',
                    heroId,
                });
            }
        });

        return catalog;
    }

    function getSearchSuggestions(query, limit = 12) {
        const normalizedQuery = normalizeSearchText(query);
        const ranked = searchShortcutCatalog
            .map((item) => {
                const haystack = normalizeSearchText(`${item.label} ${item.meta} ${item.heroName} ${item.query}`);
                if (!normalizedQuery) {
                    return { ...item, score: item.popularity || 0 };
                }
                let score = -1;
                if (haystack === normalizedQuery) {
                    score = 1000;
                } else if (haystack.startsWith(normalizedQuery)) {
                    score = 800;
                } else if (haystack.includes(normalizedQuery)) {
                    score = 500;
                } else {
                    return null;
                }
                return { ...item, score };
            })
            .filter(Boolean)
            .sort((a, b) => {
                if (b.score !== a.score) return b.score - a.score;
                return (b.popularity || 0) - (a.popularity || 0);
            });
        return ranked.slice(0, limit);
    }

    function highlightMatchText(text, query) {
        if (!query) return escapeHtml(text);
        const normalizedText = String(text || '');
        const normalizedQuery = normalizeSearchText(query);
        if (!normalizedQuery) return escapeHtml(normalizedText);

        const idx = normalizedText.toLowerCase().indexOf(normalizedQuery);
        if (idx !== -1) {
            const prefix = normalizedText.substring(0, idx);
            const match = normalizedText.substring(idx, idx + normalizedQuery.length);
            const suffix = normalizedText.substring(idx + normalizedQuery.length);
            return `${escapeHtml(prefix)}<span class="text-amber-400 font-extrabold">${escapeHtml(match)}</span>${escapeHtml(suffix)}`;
        }
        return escapeHtml(normalizedText);
    }

    function renderSearchAssistPanel(query) {
        if (!assistPanel) return;

        const normalizedQuery = normalizeSearchText(query);
        if (!normalizedQuery) {
            hideSearchAssistPanel();
            return;
        }
        const suggestions = getSearchSuggestions(query, 10);
        const title = '快捷匹配';
        const subtitle = `找到 ${suggestions.length} 个可直接点选的结果`;

        if (suggestions.length === 0) {
            assistPanel.innerHTML = `
                <div class="search-assist-header">
                    <div>
                        <div class="search-assist-title">${title}</div>
                        <div class="search-assist-subtitle">${subtitle}</div>
                    </div>
                    <button type="button" class="search-assist-tag" data-search-close>收起</button>
                </div>
                <div class="search-assist-list">
                    <div class="px-4 py-6 text-sm text-slate-400">没有匹配到快捷词，可以继续输入。</div>
                </div>
            `;
        } else {
            assistPanel.innerHTML = `
                <div class="search-assist-header">
                    <div>
                        <div class="search-assist-title">${title}</div>
                        <div class="search-assist-subtitle">${subtitle}</div>
                    </div>
                    <button type="button" class="search-assist-tag" data-search-close>收起</button>
                </div>
                <div class="search-assist-list">
                    ${suggestions.map((item) => `
                        <button type="button"
                            class="search-assist-item"
                            data-search-apply="${escapeHtml(item.query)}"
                            title="${escapeHtml(item.meta)}">
                            <div class="search-assist-item-main">
                                <div class="search-assist-item-title">${highlightMatchText(item.label, query)}</div>
                                <div class="search-assist-item-meta">${highlightMatchText(item.meta, query)}</div>
                            </div>
                            <span class="search-assist-tag">${escapeHtml(item.kind)}</span>
                        </button>
                    `).join('')}
                </div>
            `;
        }
        assistPanel.classList.remove('hidden');
        assistPanel.classList.add('search-assist-open');
        bindSearchAssistPanel();
    }

    function hideSearchAssistPanel() {
        if (!assistPanel) return;
        assistPanel.classList.remove('search-assist-open');
        assistPanel.classList.add('hidden');
        assistPanel.innerHTML = '';
    }

    function bindSearchAssistPanel() {
        if (!assistPanel) return;
        const closeBtn = assistPanel.querySelector('[data-search-close]');
        const suggestionButtons = assistPanel.querySelectorAll('[data-search-apply]');

        if (closeBtn) {
            closeBtn.addEventListener('click', () => {
                hideSearchAssistPanel();
            });
        }

        suggestionButtons.forEach((button) => {
            // mousedown 抢在 input blur 之前，避免面板先被 blur 收起导致点击丢失
            button.addEventListener('mousedown', (event) => {
                event.preventDefault();
            });
            button.addEventListener('click', () => {
                const nextQuery = button.getAttribute('data-search-apply') || '';
                applySearchQuery(nextQuery);
                hideSearchAssistPanel();
                if (input) input.focus();
            });
        });
    }

    function renderNoResultsPanel(query) {
        const safeQuery = escapeHtml(query);
        const chips = getSearchSuggestions(query, 12)
            .map((item) => `<button type="button" class="search-chip" data-search-chip="${escapeHtml(item.query)}">${escapeHtml(item.label)}</button>`)
            .join('');

        return `
            <section class="search-empty-state">
                <div class="search-empty-panel glass-panel">
                    <div class="search-empty-eyebrow">搜索未命中</div>
                    <h2 class="search-empty-title">没有找到「${safeQuery}」</h2>
                    <p class="search-empty-text">
                        可以换个英雄名、外号、拼音或简称继续搜索。下面这些快捷词都能直接点。
                    </p>
                    <div class="search-empty-input-wrap relative">
                        <span class="search-empty-icon">搜索</span>
                        <input
                            id="emptySearchInput"
                            type="text"
                            value="${safeQuery}"
                            placeholder="继续搜索英雄..."
                            class="search-empty-input"
                            autocomplete="off"
                            spellcheck="false"
                        >
                    </div>
                    <div class="search-empty-actions">
                        ${chips}
                        <button type="button" class="search-chip" data-search-clear>清空搜索</button>
                    </div>
                </div>
            </section>
        `;
    }

    function showSearchOverlay(query) {
        if (!overlay) return;
        overlay.innerHTML = renderNoResultsPanel(query);
        overlay.classList.remove('hidden');
        document.body.style.overflow = 'hidden';
        bindNoResultsSearchPanel();
    }

    function hideSearchOverlay() {
        if (!overlay) return;
        overlay.classList.add('hidden');
        overlay.innerHTML = '';
        document.body.style.overflow = '';
    }

    function bindNoResultsSearchPanel() {
        const overlayInput = overlay ? overlay.querySelector('#emptySearchInput') : null;
        const clearBtn = overlay ? overlay.querySelector('[data-search-clear]') : null;
        const chips = overlay ? overlay.querySelectorAll('[data-search-chip]') : [];

        if (overlayInput) {
            overlayInput.focus();
            overlayInput.setSelectionRange(overlayInput.value.length, overlayInput.value.length);
            overlayInput.addEventListener('compositionstart', () => {
                overlayInput.dataset.composing = '1';
            });
            overlayInput.addEventListener('compositionend', () => {
                overlayInput.dataset.composing = '';
                applySearchQuery(overlayInput.value);
            });
            overlayInput.addEventListener('input', () => {
                if (overlayInput.dataset.composing === '1') return;
                applySearchQuery(overlayInput.value);
            });
            overlayInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    const rawQuery = String(e.target.value || '').trim();
                    overlayInput.dataset.composing = '';
                    applySearchQuery(rawQuery);
                }
            });
            overlayInput.addEventListener('blur', () => {
                if (performance.now() < overlayBlurLockUntil) {
                    return;
                }
                if (overlayInput.dataset.skipBlurSync === '1') {
                    overlayInput.dataset.skipBlurSync = '';
                    return;
                }
                overlayInput.dataset.composing = '';
                applySearchQuery(overlayInput.value);
            });
        }

        if (clearBtn) {
            clearBtn.addEventListener('mousedown', (event) => {
                event.preventDefault();
            });
            clearBtn.addEventListener('click', () => {
                overlayBlurLockUntil = performance.now() + 250;
                if (overlayInput) {
                    overlayInput.dataset.skipBlurSync = '1';
                    overlayInput.dataset.composing = '';
                    overlayInput.value = '';
                }
                applySearchQuery('');
                if (input) input.focus();
                if (overlayInput) {
                    window.setTimeout(() => {
                        overlayInput.dataset.skipBlurSync = '';
                    }, 0);
                }
            });
        }

        chips.forEach((chip) => {
            chip.addEventListener('mousedown', (event) => {
                event.preventDefault();
            });
            chip.addEventListener('click', () => {
                overlayBlurLockUntil = performance.now() + 250;
                if (overlayInput) {
                    overlayInput.dataset.skipBlurSync = '1';
                    overlayInput.dataset.composing = '';
                }
                const nextQuery = chip.getAttribute('data-search-chip') || '';
                applySearchQuery(nextQuery);
                if (input) input.focus();
                if (overlayInput) {
                    window.setTimeout(() => {
                        overlayInput.dataset.skipBlurSync = '';
                    }, 0);
                }
            });
        });
    }

    function filterChampions(query) {
        const normalizedQuery = normalizeSearchText(query);
        if (!normalizedQuery) {
            return allChampions;
        }

        return allChampions.filter((c) => {
            const name = c['英雄名称'] || '';
            const enName = c['英文名'] || '';
            const heroKey = normalizeSearchText(name) || normalizeSearchText(enName);
            const indexedTerms = championSearchIndex.get(heroKey) || new Set();
            const directAliasHit = heroKey === normalizedQuery || indexedTerms.has(normalizedQuery);

            if (directAliasHit) {
                return true;
            }

            if (indexedTerms.size > 0) {
                for (const term of indexedTerms) {
                    if (matchesSearchToken(term, normalizedQuery)) {
                        return true;
                    }
                }
            }

            return matchesSearchToken(normalizeSearchText(name), normalizedQuery) ||
                matchesSearchToken(normalizeSearchText(enName), normalizedQuery);
        });
    }

    function applySearchQuery(rawQuery) {
        const nextQuery = String(rawQuery || '').trim();
        currentSearchQuery = nextQuery;

        if (input && input.value !== rawQuery) {
            input.value = rawQuery;
        }

        if (!nextQuery) {
            hideSearchOverlay();
            onRender(allChampions);
            return;
        }

        const filtered = filterChampions(nextQuery);
        if (filtered.length === 0 && allChampions.length > 0) {
            // 搜索未命中：展示引导面板而不是误导性的“数据准备中”
            showSearchOverlay(nextQuery);
            return;
        }
        hideSearchOverlay();
        onRender(filtered);
    }

    // 判断当前焦点是否在可编辑元素中，避免在 input/textarea 里按 "/" 误触发跳转
    function isEditableTarget(target) {
        if (!target) return false;
        const tag = (target.tagName || '').toLowerCase();
        return tag === 'input' || tag === 'textarea' || target.isContentEditable;
    }

    function setup() {
        if (!input) return;

        // 全局快捷键：按 "/" 或 Ctrl+K 自动聚焦搜索框
        document.addEventListener('keydown', (e) => {
            const isSlash = e.key === '/' && !isEditableTarget(e.target);
            const isCtrlK = (e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K');
            if (isSlash || isCtrlK) {
                e.preventDefault();
                input.focus();
                input.select();
            }
        });

        // input 路径防抖 150ms；IME 组词与 Enter/blur 保持立即执行
        const debouncedApply = debounce((value) => {
            applySearchQuery(value);
            renderSearchAssistPanel(value);
        }, SEARCH_DEBOUNCE_MS);

        input.addEventListener('compositionstart', () => {
            input.dataset.composing = '1';
        });

        input.addEventListener('compositionend', () => {
            input.dataset.composing = '';
            window.setTimeout(() => {
                applySearchQuery(input.value);
                renderSearchAssistPanel(input.value);
            }, 0);
        });

        input.addEventListener('input', (e) => {
            if (input.dataset.composing === '1') return;
            debouncedApply(e.target.value);
        });

        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                debouncedApply.cancel();
                applySearchQuery(e.target.value);
                hideSearchAssistPanel();
            }
        });

        input.addEventListener('blur', () => {
            if (input.dataset.composing === '1') return;
            debouncedApply.cancel();
            applySearchQuery(input.value);
            window.setTimeout(() => {
                if (document.activeElement !== input) {
                    hideSearchAssistPanel();
                }
            }, 180);
        });

        document.addEventListener('click', (e) => {
            const target = e.target;
            const insideSearch = target && (target.closest?.('#searchAssistPanel') || target.closest?.('#searchInput'));
            if (!insideSearch) {
                hideSearchAssistPanel();
            }
        });
    }

    return {
        setup,
        setChampions(champions, aliasRecords) {
            allChampions = champions;
            championCoreIndex = buildChampionCoreIndex(champions);
            searchShortcutCatalog = buildSearchShortcutCatalog(champions, aliasRecords);
        },
        applySearchQuery,
        // 数据刷新后按当前查询重放视图，避免旧结果残留
        reapply() {
            applySearchQuery(currentSearchQuery);
        },
        filterChampions,
        hideSearchAssistPanel,
        hideSearchOverlay,
        get currentQuery() {
            return currentSearchQuery;
        },
    };
}
