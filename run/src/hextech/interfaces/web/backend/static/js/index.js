// 海克斯首页前端逻辑。
// 调用方: hextech/display/web 首页 HTML; 关键依赖: API_BASE、WS_URL、searchOverlay。

        const API_BASE = window.location.origin;
        const WS_URL = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}`;
        const PRELOAD_HOVER_DELAY_MS = 500;
        const PRELOAD_HEADERS = {};

        let ddVersion = '14.3.1';
        let currentSearchQuery = '';
        const searchOverlay = document.getElementById('searchOverlay');
        const searchAssistPanel = document.getElementById('searchAssistPanel');
        let searchOverlayBlurLockUntil = 0;
        let searchShortcutCatalog = [];
        let championSearchIndex = new Map();
        let championAliasRecords = [];
        let championCoreIndex = new Map();

        fetch('https://ddragon.leagueoflegends.com/api/versions.json')
            .then(r => r.json())
            .then(v => { if (v && v[0]) ddVersion = v[0]; })
            .catch(() => {});

        let allChampions = [];
        let activeChampionIds = new Set();
        let ws = null;

        // 视觉样式仍由 Web 负责，但分档只消费 generation 的权威 `英雄评级`。
        // 阈值唯一维护在 Python 的 champion_tier.py，避免两端随时间漂移。
        const TIERS = [
            { id: 'T1', name: '夯', enName: 'God Tier', cssClass: 'hx-tier-op' },
            { id: 'T2', name: '顶级', enName: 'Top', cssClass: 'hx-tier-s' },
            { id: 'T3', name: '人上人', enName: 'Elite', cssClass: 'hx-tier-a' },
            { id: 'T4', name: 'npc', enName: 'Average', cssClass: 'hx-tier-npc' },
            { id: 'T5', name: '拉', enName: 'Trash', cssClass: 'hx-tier-trash' }
        ];
        const TIER_BY_ID = new Map(TIERS.map(tier => [tier.id, tier]));

        const CHAMPION_PINYIN = {
            '亚索': 'Yasuo', '永恩': 'Yone', '阿狸': 'Ahri', '阿卡丽': 'Akali',
            '卡莎': 'Kaisa', '伊泽瑞尔': 'Ezreal', '卢锡安': 'Lucian', '薇恩': 'Vayne',
            '金克丝': 'Jinx', '凯特琳': 'Caitlyn', '艾希': 'Ashe', '烬': 'Jhin',
            '拉克丝': 'Lux', '盲僧': 'LeeSin', '李青': 'LeeSin',
            '劫': 'Zed', '泰隆': 'Talon', '卡特琳娜': 'Katarina',
            '盖伦': 'Garen', '德莱厄斯': 'Darius', '诺手': 'Darius', '腕豪': 'Sett',
            '提莫': 'Teemo', '崔丝塔娜': 'Tristana', '小炮': 'Tristana',
            '维迦': 'Veigar', '小法师': 'Veigar', '吉格斯': 'Ziggs', '爆破鬼才': 'Ziggs',
            '凯南': 'Kennen', '慎': 'Shen', '暮光之眼': 'Shen',
            '阿木木': 'Amumu', '茂凯': 'Maokai', '大树': 'Maokai',
            '艾尼维亚': 'Anivia', '冰鸟': 'Anivia', '沃利贝尔': 'Volibear', '狗熊': 'Volibear',
            '古拉加斯': 'Gragas', '酒桶': 'Gragas', '特朗德尔': 'Trundle', '巨魔之王': 'Trundle',
            '沃里克': 'Warwick', '狼人': 'Warwick',
            '布里茨': 'Blitzcrank', '蒸汽机器人': 'Blitzcrank', '瑟提': 'Sett',
            '萨勒芬妮': 'Seraphine', '娑娜': 'Sona', '琴女': 'Sona',
            '卡尔玛': 'Karma', '天启者': 'Karma', '迦娜': 'Janna', '风女': 'Janna',
            '璐璐': 'Lulu', '基兰': 'Zilean', '时光守护者': 'Zilean',
            '黑默丁格': 'Heimerdinger', '大头': 'Heimerdinger',
            '卡萨丁': 'Kassadin', '马尔扎哈': 'Malzahar', '先知': 'Malzahar',
            '科加斯': 'Chogath', '大虫子': 'Chogath', '雷克顿': 'Renekton', '鳄鱼': 'Renekton',
            '内瑟斯': 'Nasus', '狗头': 'Nasus', '瑟庄妮': 'Sejuani', '猪妹': 'Sejuani',
            '布隆': 'Braum', '弗雷尔卓德之心': 'Braum', '丽桑卓': 'Lissandra', '冰裔': 'Lissandra',
            '艾翁': 'Ivern', '翠神': 'Ivern', '妮蔻': 'Neeko', '万花通灵': 'Neeko',
            '佐伊': 'Zoe', '暮光星灵': 'Zoe', '瑞兹': 'Ryze', '符文法师': 'Ryze',
            '卡莉丝塔': 'Kalista', '复仇之矛': 'Kalista', '莎弥拉': 'Samira', '沙漠玫瑰': 'Samira',
            '厄斐琉斯': 'Aphelios', '残月之肃': 'Aphelios', '赛娜': 'Senna', '涤魂圣枪': 'Senna',
            '奇亚娜': 'Qiyana', '元素女皇': 'Qiyana', '悠米': 'Yuumi', '魔法猫咪': 'Yuumi',
            '潘森': 'Pantheon', '不屈之枪': 'Pantheon',
            '佛耶戈': 'Viego', '破败之王': 'Viego', '格温': 'Gwen', '灵罗娃娃': 'Gwen',
            '阿克尚': 'Akshan', '影哨': 'Akshan', '薇古丝': 'Vex', '愁云使者': 'Vex',
            '泽丽': 'Zeri', '霓虹之女': 'Zeri', '卑尔维斯': 'Belveth', '女皇终结者': 'Belveth',
            '纳亚菲利': 'Naafiri', '百裂冥犬': 'Naafiri', '奎桑提': 'KSante', '纳祖芒': 'KSante',
            '奥瑞利安索尔': 'AurelionSol', '铸星龙王': 'AurelionSol', '龙王': 'AurelionSol',
            '蝎子': 'Skarner', '斯卡纳': 'Skarner',
        };

        let indexBootstrapped = false;
        function bootstrapIndexPage() {
            if (indexBootstrapped) return;
            indexBootstrapped = true;
            loadChampions();
            connectWS();
            setupSearch();
        }

        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', bootstrapIndexPage, { once: true });
        } else {
            bootstrapIndexPage();
        }

        async function loadChampions() {
            try {
                const [championRes, aliasRes] = await Promise.all([
                    fetch(`${API_BASE}/api/champions`),
                    fetch(`${API_BASE}/api/champion_aliases`).catch(() => null),
                ]);
                const res = championRes;
                if (!res.ok) {
                    const failure = await res.json().catch(() => ({}));
                    if (res.status === 503 && failure.error === 'snapshot_unavailable') {
                        throw new Error('snapshot_unavailable');
                    }
                    throw new Error(`HTTP ${res.status}`);
                }
                allChampions = await res.json();
                championCoreIndex = buildChampionCoreIndex(allChampions);
                championAliasRecords = aliasRes && aliasRes.ok ? await aliasRes.json() : [];
                if (!Array.isArray(championAliasRecords) || championAliasRecords.length === 0) {
                    championAliasRecords = await loadAliasRecordsFromStaticFiles();
                }
                if (!Array.isArray(championAliasRecords) || championAliasRecords.length === 0) {
                    championAliasRecords = buildFallbackAliasRecords(allChampions);
                }
                searchShortcutCatalog = buildSearchShortcutCatalog(allChampions, championAliasRecords);
                console.log('[INFO] 加载英雄数据成功，共', allChampions.length, '个英雄');
                renderTiers(allChampions);
            } catch (err) {
                console.error('[ERROR] 加载英雄数据失败:', err);
                const unavailable = err && err.message === 'snapshot_unavailable';
                document.getElementById('tierContainer').innerHTML = `
                    <div class="text-center py-20 ${unavailable ? 'text-slate-300' : 'text-red-400'}">
                        <div class="text-4xl mb-4">${unavailable ? '等待' : '警告'}</div>
                        <div class="font-bold text-xl">${unavailable ? '统计快照准备中' : '数据加载失败'}</div>
                        <div class="text-sm mt-2">${unavailable ? 'DataService 正在生成可用数据，请稍后刷新' : '请确保后端服务已启动'}</div>
                    </div>
                `;
            }
        }

        function renderTiers(champions) {
            const container = document.getElementById('tierContainer');
            if (!container) return;

            if (champions.length === 0) {
                container.innerHTML = `
                    <div class="text-center py-20 text-slate-300">
                        <div class="font-bold text-xl">数据准备中</div>
                        <div class="text-sm mt-2 text-slate-400">后台正在加载英雄数据，稍后刷新即可显示完整列表</div>
                    </div>
                `;
                hideSearchAssistPanel();
                return;
            }

            hideSearchOverlay();

            const sorted = [...champions].sort((a, b) => b['综合分数'] - a['综合分数']);

            const tierGroups = {};
            TIERS.forEach(t => tierGroups[t.id] = []);

            sorted.forEach(champ => {
                const tier = TIER_BY_ID.get(String(champ['英雄评级'] || '').toUpperCase()) || TIER_BY_ID.get('T3');
                tierGroups[tier.id].push(champ);
            });

            container.innerHTML = '';
            TIERS.forEach(tier => {
                const champs = tierGroups[tier.id];
                if (champs.length === 0) return;

                const grayscaleClass = '';
                const opacityClass = '';

                const champHTML = champs.map((c, i) => createChampionCard(c, i)).join('');

                container.innerHTML += `
                    <div class="hx-tier-row hx-tier-group-shell rounded-2xl overflow-hidden ${opacityClass} ${grayscaleClass}">
                        <div class="hx-tier-label shrink-0 ${tier.cssClass} flex flex-col items-center justify-center font-bold text-xl shadow-[2px_0_10px_rgba(0,0,0,0.3)] z-10">
                            <span>${tier.name}</span>
                            <span class="text-[9px] opacity-80 mt-1 uppercase tracking-wider">${tier.enName}</span>
                        </div>
                        <div class="hx-tier-champions flex-1 flex flex-wrap">
                            ${champHTML}
                        </div>
                    </div>
                `;
            });
            bindChampionPreloads();
        }

        function escapeHtml(value) {
            return String(value)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }

        function normalizeSearchText(value) {
            return String(value || '')
                .toLowerCase()
                .replace(/[\s·_\-()/【】\[\]（）]/g, '')
                .trim();
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
                    heroId
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

        async function loadAliasRecordsFromStaticFiles() {
            const aliasIndex = await loadJsonFromCandidates([
                `${API_BASE}/api/champion_aliases`,
                '/catalog/Champion_Alias_Index.json'
            ]);

            if (Array.isArray(aliasIndex)) {
                return aliasIndex;
            }

            const coreData = await loadJsonFromCandidates([
                '/catalog/Champion_Core_Data.json'
            ]);

            if (!coreData || typeof coreData !== 'object') {
                return [];
            }

            return Object.entries(coreData).map(([heroId, entry]) => ({
                title: entry.name || '',
                heroName: entry.title || '',
                enName: entry.en_name || '',
                heroId: String(entry.id || entry.hero_id || heroId || ''),
                aliases: Array.isArray(entry.aliases) ? entry.aliases : []
            }));
        }

        function buildFallbackAliasRecords(champions) {
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
                    aliases
                };
            });
        }

        function buildSearchShortcutCatalog(champions, aliasRecords = []) {
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
                            heroId
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
                            heroId
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
                            heroId: canonicalHeroId
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
                        heroId: canonicalHeroId
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
                        heroId
                    });
                }
            });

            return catalog;
        }

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

        function getSearchSuggestions(query, limit = 12) {
            const normalizedQuery = normalizeSearchText(query);
            const source = searchShortcutCatalog.length ? searchShortcutCatalog : [];
            const ranked = source
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
            if (!searchAssistPanel) return;

            const suggestions = getSearchSuggestions(query, 10);
            const normalizedQuery = normalizeSearchText(query);
            if (!normalizedQuery) {
                hideSearchAssistPanel();
                return;
            }
            const title = normalizedQuery ? '快捷匹配' : '热门快捷搜索';
            const subtitle = normalizedQuery
                ? `找到 ${suggestions.length} 个可直接点选的结果`
                : '点击即可直接切换搜索';

            if (suggestions.length === 0) {
                searchAssistPanel.innerHTML = `
                    <div class="search-assist-header">
                        <div>
                            <div class="search-assist-title">${title}</div>
                            <div class="search-assist-subtitle">${subtitle}</div>
                        </div>
                        <button type="button" class="search-assist-tag" data-search-close>收起</button>
                    </div>
                    <div class="search-assist-list">
                        <div class="px-4 py-6 text-sm text-gray-400">没有匹配到快捷词，可以继续输入。</div>
                    </div>
                `;
                searchAssistPanel.classList.remove('hidden');
                bindSearchAssistPanel();
                return;
            }

            searchAssistPanel.innerHTML = `
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
            searchAssistPanel.classList.remove('hidden');
            bindSearchAssistPanel();
        }

        function hideSearchAssistPanel() {
            if (!searchAssistPanel) return;
            searchAssistPanel.classList.remove('search-assist-open');
            searchAssistPanel.classList.add('hidden');
            searchAssistPanel.innerHTML = '';
        }

        function bindSearchAssistPanel() {
            if (!searchAssistPanel) return;
            const closeBtn = searchAssistPanel.querySelector('[data-search-close]');
            const suggestionButtons = searchAssistPanel.querySelectorAll('[data-search-apply]');

            if (closeBtn) {
                closeBtn.addEventListener('click', () => {
                    hideSearchAssistPanel();
                });
            }

            suggestionButtons.forEach((button) => {
                button.addEventListener('click', () => {
                    const nextQuery = button.getAttribute('data-search-apply') || '';
                    applySearchQuery(nextQuery);
                    const input = document.getElementById('searchInput');
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
            if (!searchOverlay) return;
            searchOverlay.innerHTML = renderNoResultsPanel(query);
            searchOverlay.classList.remove('hidden');
            document.body.style.overflow = 'hidden';
            bindNoResultsSearchPanel();
        }

        function hideSearchOverlay() {
            if (!searchOverlay) return;
            searchOverlay.classList.add('hidden');
            searchOverlay.innerHTML = '';
            document.body.style.overflow = '';
        }

        function bindNoResultsSearchPanel() {
            const input = document.getElementById('emptySearchInput');
            const clearBtn = searchOverlay?.querySelector('[data-search-clear]');
            const chips = searchOverlay ? searchOverlay.querySelectorAll('[data-search-chip]') : [];

            if (input) {
                input.focus();
                input.setSelectionRange(input.value.length, input.value.length);
                const assist = document.getElementById('searchAssistPanel');
                if (assist) assist.classList.remove('search-assist-open');
                input.addEventListener('compositionstart', () => {
                    input.dataset.composing = '1';
                    const assist = document.getElementById('searchAssistPanel');
                    if (assist) assist.classList.remove('search-assist-open');
                });
                input.addEventListener('compositionend', () => {
                    input.dataset.composing = '';
                    const assist = document.getElementById('searchAssistPanel');
                    if (assist) assist.classList.remove('search-assist-open');
                    applySearchQuery(input.value);
                });
                input.addEventListener('input', () => {
                    if (input.dataset.composing === '1') return;
                    const assist = document.getElementById('searchAssistPanel');
                    if (assist) assist.classList.remove('search-assist-open');
                    applySearchQuery(input.value);
                });
                input.addEventListener('keydown', (e) => {
                    if (e.key === 'Enter') {
                        e.preventDefault();
                        const assist = document.getElementById('searchAssistPanel');
                        const rawQuery = String(e.target.value || '').trim();
                        const isImeConfirm = e.isComposing || input.dataset.composing === '1';
                        input.dataset.composing = '';
                        if (isImeConfirm) {
                            if (assist) assist.classList.remove('search-assist-open');
                            applySearchQuery(rawQuery);
                            return;
                        }
                        applySearchQuery(rawQuery);
                        if (assist && rawQuery.length >= 2 && filterChampions(rawQuery).length === 0) {
                            assist.classList.add('search-assist-open');
                        } else if (assist) {
                            assist.classList.remove('search-assist-open');
                        }
                    }
                });
                input.addEventListener('blur', () => {
                    if (performance.now() < searchOverlayBlurLockUntil) {
                        return;
                    }
                    if (input.dataset.skipBlurSync === '1') {
                        input.dataset.skipBlurSync = '';
                        return;
                    }
                    input.dataset.composing = '';
                    const assist = document.getElementById('searchAssistPanel');
                    if (assist) assist.classList.remove('search-assist-open');
                    applySearchQuery(input.value);
                });
            }

            if (clearBtn) {
                clearBtn.addEventListener('mousedown', (event) => {
                    event.preventDefault();
                });
                clearBtn.addEventListener('click', () => {
                    searchOverlayBlurLockUntil = performance.now() + 250;
                    if (input) {
                        input.dataset.skipBlurSync = '1';
                        input.dataset.composing = '';
                        input.value = '';
                    }
                    applySearchQuery('');
                    const topInput = document.getElementById('searchInput');
                    if (topInput) {
                        topInput.focus();
                    }
                    if (input) {
                        window.setTimeout(() => {
                            input.dataset.skipBlurSync = '';
                        }, 0);
                    }
                });
            }

            chips.forEach((chip) => {
                chip.addEventListener('mousedown', (event) => {
                    event.preventDefault();
                });
                chip.addEventListener('click', () => {
                    searchOverlayBlurLockUntil = performance.now() + 250;
                    if (input) {
                        input.dataset.skipBlurSync = '1';
                        input.dataset.composing = '';
                    }
                    const nextQuery = chip.getAttribute('data-search-chip') || '';
                    applySearchQuery(nextQuery);
                    const topInput = document.getElementById('searchInput');
                    if (topInput) {
                        topInput.focus();
                    }
                    if (input) {
                        window.setTimeout(() => {
                            input.dataset.skipBlurSync = '';
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

            return allChampions.filter(c => {
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

            const topInput = document.getElementById('searchInput');
            if (topInput && topInput.value !== rawQuery) {
                topInput.value = rawQuery;
            }

            if (!nextQuery) {
                hideSearchOverlay();
                renderTiers(allChampions);
                return;
            }

            const filtered = filterChampions(nextQuery);
            renderTiers(filtered);
        }

        function createChampionCard(champion, index) {
            const heroId = champion['英雄 ID'] || '';
            const enName = champion['英文名'] || CHAMPION_PINYIN[champion['英雄名称']] || '';
            const wr = formatPercent(champion['英雄胜率']);
            const heroName = String(champion['英雄名称'] || '');

            const detailUrl = `detail.html?hero=${encodeURIComponent(heroName)}&wr=${encodeURIComponent(wr)}&pr=${encodeURIComponent(formatPercent(champion['英雄出场率']))}&id=${encodeURIComponent(heroId)}&en=${encodeURIComponent(enName)}`;

            const isHighlighted = activeChampionIds.has(String(heroId)) ? 'hx-hero-avatar-highlight' : '';

            let avatarUrl;
            if (heroId) {
                avatarUrl = `/assets/champions/${heroId}.png`;
            } else if (enName) {
                avatarUrl = `https://ddragon.leagueoflegends.com/cdn/${ddVersion}/img/champion/${enName}.png`;
            } else {
                avatarUrl = '';
            }

            const safeHeroName = escapeHtml(heroName);
            const safeDetailUrl = escapeHtml(detailUrl);
            const safeAvatarUrl = escapeHtml(avatarUrl);
            const safeHeroNameAttr = escapeHtml(heroName);
            const safeWr = escapeHtml(wr);
            const avatarHiddenStyle = avatarUrl ? '' : 'visibility:hidden;';

            return `
                <a
                    href="${safeDetailUrl}"
                    class="hx-hero-avatar relative rounded-lg overflow-hidden cursor-pointer block flex-shrink-0 ${isHighlighted}"
                    style="animation-delay: ${index * 30}ms"
                    title="${safeHeroName} (${safeWr})"
                    data-hero-name="${safeHeroNameAttr}"
                    data-hero-id="${escapeHtml(String(heroId || ''))}"
                    data-en-name="${escapeHtml(String(enName || ''))}"
                >
                    <img loading="lazy" width="60" height="60"
                        src="${safeAvatarUrl}"
                        alt="${safeHeroNameAttr}"
                        class="w-full h-full object-cover bg-gray-800"
                        style="${avatarHiddenStyle}"
                        data-hero-name="${safeHeroNameAttr}"
                        onerror="this.removeAttribute('src'); this.style.visibility='hidden';"
                    />
                    <div class="absolute bottom-0 inset-x-0 bg-black/80 text-center text-[10px] font-bold text-yellow-400 py-0.5 backdrop-blur-sm">
                        ${safeWr}
                    </div>
                </a>
            `;
        }

        function formatPercent(num) {
            return (num * 100).toFixed(1) + '%';
        }

        function getPreloadHeaders() {
            return PRELOAD_HEADERS;
        }

        function scheduleChampionPreload(anchor) {
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
                    await fetch(`${API_BASE}/api/champion/${encodeURIComponent(heroName)}/preload`, {
                        method: 'POST',
                        headers: getPreloadHeaders(),
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

        function bindChampionPreloads() {
            document.querySelectorAll('.hx-hero-avatar[data-hero-name]').forEach(anchor => {
                if (anchor.dataset.preloadBound === 'true') return;
                anchor.dataset.preloadBound = 'true';
                anchor.addEventListener('mouseenter', () => scheduleChampionPreload(anchor), { passive: true });
                anchor.addEventListener('focus', () => scheduleChampionPreload(anchor), { passive: true });
            });
        }

        // 右上角短暂气泡，告知玩家备战席或数据已自动刷新；同时只挂一个 DOM 节点
        let _updateToastTimer = null;
        function showUpdateToast(text) {
            let toast = document.getElementById('hxUpdateToast');
            if (!toast) {
                toast = document.createElement('div');
                toast.id = 'hxUpdateToast';
                toast.className = 'hx-update-toast';
                document.body.appendChild(toast);
            }
            toast.textContent = text;
            requestAnimationFrame(() => toast.classList.add('is-visible'));
            if (_updateToastTimer) clearTimeout(_updateToastTimer);
            _updateToastTimer = setTimeout(() => toast.classList.remove('is-visible'), 1500);
        }

        // 首启引导气泡：用 localStorage 判定首次，仅在 WS 第一次连通时弹出，5s 自动消失
        function showOnboardingToastOnce() {
            try {
                if (localStorage.getItem('hextech_onboarded') === '1') return;
            } catch (_e) {
                // 隐私模式下 localStorage 不可用，干脆跳过引导，避免重复打扰
                return;
            }
            const toast = document.createElement('div');
            toast.className = 'hx-onboarding-toast';
            toast.innerHTML = `
                <button type="button" class="close" aria-label="关闭">✕</button>
                <div class="title">海克斯核心共鸣成功</div>
                <div class="body">已自动锁定本地备战席。可按 <kbd class="hx-search-hint">/</kbd> 或 <kbd class="hx-search-hint">Ctrl+K</kbd> 唤起搜索框。</div>
            `;
            document.body.appendChild(toast);
            requestAnimationFrame(() => toast.classList.add('is-visible'));
            const dismiss = () => {
                toast.classList.remove('is-visible');
                setTimeout(() => toast.remove(), 320);
                try { localStorage.setItem('hextech_onboarded', '1'); } catch (_e) {}
            };
            toast.querySelector('.close').addEventListener('click', dismiss);
            setTimeout(dismiss, 5000);
        }

        function connectWS() {
            ws = new WebSocket(WS_URL + '/ws');
            const status = document.getElementById('wsStatus');

            ws.onopen = () => {
                status.innerHTML = '<span class="w-2 h-2 bg-green-500 rounded-full animate-pulse"></span><span class="text-green-400">已连接</span>';
                // 首次握手成功展示一次新手引导气泡，之后不再打扰
                showOnboardingToastOnce();
            };

            ws.onclose = () => {
                status.innerHTML = '<span class="w-2 h-2 bg-red-500 rounded-full"></span><span class="text-red-400">已断开</span>';
                setTimeout(connectWS, 3000);
            };

            ws.onmessage = (e) => {
                try {
                    const message = JSON.parse(e.data);
                    console.log('[WS] 收到消息:', message);

                    if (message.type === 'champion_update') {
                        activeChampionIds = new Set(message.champion_ids.map(String));
                        renderTiers(allChampions);
                        showUpdateToast('备战席已更新');
                    } else if (message.type === 'data_updated') {
                        console.log('[WS] 数据已更新，重新加载...');
                        loadChampions();
                        const degraded = Array.isArray(message.degraded_sources) ? message.degraded_sources : [];
                        showUpdateToast(degraded.length ? `数据已刷新 · 沿用 ${degraded.join('/')}` : '数据已刷新');
                    } else if (message.type === 'local_player_locked') {
                        const heroName = message.hero_name;
                        const championId = message.champion_id;
                        const detailFirst = message.detail_first ? '&detailFirst=1' : '';
                        const heroData = allChampions.find(c => c['英雄名称'] === heroName);
                        if (heroData) {
                            const wr = formatPercent(heroData['英雄胜率']);
                            const pr = formatPercent(heroData['英雄出场率']);
                            const enName = heroData['英文名'] || CHAMPION_PINYIN[heroName] || '';
                            const heroId = heroData['英雄 ID'] || '';

                            console.log('[WS] 本地玩家锁定:', heroName, '-> 直接跳转详情页');
                            window.location.href = `detail.html?hero=${encodeURIComponent(heroName)}&wr=${encodeURIComponent(wr)}&pr=${encodeURIComponent(pr)}&id=${encodeURIComponent(heroId)}&en=${encodeURIComponent(enName)}&auto=1${detailFirst}`;
                        } else if (heroName) {
                            window.location.href = `detail.html?hero=${encodeURIComponent(heroName)}&id=${encodeURIComponent(championId)}&auto=1${detailFirst}`;
                        }
                    }
                } catch (err) {
                    console.error('[WS] 解析消息失败:', err);
                }
            };
        }

        // 判断当前焦点是否在可编辑元素中，避免在 input/textarea 里按 "/" 误触发跳转
        function _isEditableTarget(target) {
            if (!target) return false;
            const tag = (target.tagName || '').toLowerCase();
            return tag === 'input' || tag === 'textarea' || target.isContentEditable;
        }

        function setupSearch() {
            const input = document.getElementById('searchInput');
            if (!input) return;

            // 全局快捷键：按 "/" 或 Ctrl+K 自动聚焦搜索框
            document.addEventListener('keydown', (e) => {
                const isSlash = e.key === '/' && !_isEditableTarget(e.target);
                const isCtrlK = (e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K');
                if (isSlash || isCtrlK) {
                    e.preventDefault();
                    input.focus();
                    input.select();
                }
            });

            input.addEventListener('compositionstart', () => {
                input.dataset.composing = '1';
            });

            input.addEventListener('compositionend', () => {
                input.dataset.composing = '';
                window.setTimeout(() => applySearchQuery(input.value), 0);
            });

            input.addEventListener('input', (e) => {
                if (input.dataset.composing === '1') return;
                applySearchQuery(e.target.value);
            });

            input.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    applySearchQuery(e.target.value);
                    if (searchAssistPanel && !searchAssistPanel.classList.contains('hidden')) {
                        hideSearchAssistPanel();
                    }
                }
            });

            input.addEventListener('blur', () => {
                if (input.dataset.composing === '1') return;
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
                if (!insideSearch && searchAssistPanel) {
                    hideSearchAssistPanel();
                }
            });
        }
