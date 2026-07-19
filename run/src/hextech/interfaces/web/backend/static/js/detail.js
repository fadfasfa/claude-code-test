// 海克斯详情页前端逻辑。
// 调用方: hextech/display/web 详情页 HTML; 关键依赖: API_BASE、WS_URL、ddragon API。

        const API_BASE = window.location.origin;
        const WS_URL = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}`;
        let ddVersion = '14.3.1';
        window.ddVersion = ddVersion;
        fetch('https://ddragon.leagueoflegends.com/api/versions.json')
            .then(r => r.json())
            .then(v => { if (v && v[0]) { ddVersion = v[0]; window.ddVersion = ddVersion; } })
            .catch(() => { });

        let hextechData = null;
        let currentView = 'all';
        let currentMode = 'comprehensive';
        let activeArray = null;
        let synergyData = null;
        let synergyMeta = null;
        let synergyLoaded = false;
        let augmentIconMap = {};
        let augmentCatalogMap = {};
        let ws = null;
        const DETAIL_LOADING_RETRY_BASE_MS = 500;
        const DETAIL_LOADING_RETRY_MAX_MS = 5000;
        const DETAIL_LOADING_MAX_RETRIES = 12;
        let detailLoadingRetryTimer = null;
        let detailPreloadRequested = false;

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
                    <p class="text-xs text-gray-400 leading-relaxed mb-6">
                        已在其他悬浮窗/标签页中激活了新的枢纽，当前窗口已自动进入魔法休眠状态，以降低系统资源开销。
                    </p>
                    <div class="dormancy-btn-row">
                        <button type="button" id="btnReactivate" class="dormancy-btn">重新激活此窗口</button>
                        <button type="button" id="btnAllowDuo" class="dormancy-btn dormancy-btn-secondary">双开此视口</button>
                    </div>
                </div>
            `;
            document.body.appendChild(overlay);

            if (window.wsInstance) {
                window.wsInstance.onclose = null;
                window.wsInstance.close();
            }
            clearDetailRetry();
        }

        function reactivateTab() {
            const overlay = document.getElementById('dormancyOverlay');
            if (overlay) {
                overlay.remove();
            }
            tabId = Math.random().toString(36).substring(2, 9);
            bc.postMessage({ type: 'new_tab_opened', id: tabId });

            connectWS();
            loadHextechs();
        }

        // 双开模式：把本标签加入白名单广播给其他标签，对方收到后不再对本 tabId 触发休眠
        function allowDuoTab() {
            const overlay = document.getElementById('dormancyOverlay');
            if (overlay) {
                overlay.remove();
            }
            dormancyAllowList.add(tabId);
            bc.postMessage({ type: 'allow_duo', id: tabId });
            connectWS();
            loadHextechs();
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
        bc.postMessage({ type: 'new_tab_opened', id: tabId });

        document.addEventListener('click', (event) => {
            if (event.target && event.target.id === 'btnReactivate') {
                reactivateTab();
            } else if (event.target && event.target.id === 'btnAllowDuo') {
                allowDuoTab();
            }
        });

        const urlParams = new URLSearchParams(window.location.search);
        const hero = urlParams.get('hero');
        const enName = urlParams.get('en');
        const heroId = urlParams.get('id');
        const heroWR = urlParams.get('wr');
        const heroPR = urlParams.get('pr');
        const isAuto = urlParams.get('auto');

        window.enName = enName;
        window.heroId = heroId;
        window.heroName = hero;

        if (hero) document.getElementById('heroName').textContent = hero;
        if (heroWR) document.getElementById('heroWR').textContent = heroWR;
        if (heroPR) document.getElementById('heroPR').textContent = heroPR;

        if (heroId) {
            document.getElementById('heroAvatar').src = `/assets/${heroId}.png`;
        } else if (enName) {
            document.getElementById('heroAvatar').src = `https://ddragon.leagueoflegends.com/cdn/${ddVersion}/img/champion/${enName}.png`;
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

        function setDisplayMode(mode) {
            currentMode = mode;
            document.querySelectorAll('.mode-btn').forEach(btn => {
                btn.className = 'mode-btn hx-tab-idle px-4 py-2 rounded-lg font-medium text-sm';
            });
            const activeBtn = mode === 'comprehensive' ? document.getElementById('btn-comprehensive') : document.getElementById('btn-winrate');
            if (activeBtn) {
                activeBtn.className = 'mode-btn hx-tab-active px-4 py-2 rounded-lg text-sm';
            }
            renderCurrentView();
        }

        function switchView(view) {
            currentView = view;
            document.querySelectorAll('.tab-btn').forEach(btn => {
                btn.className = 'tab-btn hx-tab-idle px-5 py-2 rounded-lg font-medium';
            });
            const activeBtn = document.getElementById(`tab-${view}`);
            if (activeBtn) {
                activeBtn.className = 'tab-btn hx-tab-active px-5 py-2 rounded-lg';
            }
            renderCurrentView();
        }

        function normalizeTierName(tier) {
            const text = String(tier || '').trim();
            if (text.includes('棱彩') || text.includes('彩色') || text === 'Prismatic') return 'Prismatic';
            if (text.includes('黄金') || text.includes('金') || text === 'Gold') return 'Gold';
            return 'Silver';
        }

        function normalizeAugmentKey(value) {
            return String(value || '')
                .toLowerCase()
                .replace(/[\s·_\-()/【】\[\]（）:：'"]/g, '')
                .trim();
        }

        function getTierBarClass(tier) {
            const normalized = normalizeTierName(tier);
            if (normalized === 'Prismatic') return 'hx-tier-prismatic-bar';
            if (normalized === 'Gold') return 'hx-tier-gold-bar';
            return 'hx-tier-silver-bar';
        }

        function toLocalAugmentIconUrl(iconUrl, hextechName) {
            if (iconUrl) {
                return iconUrl;
            }

            return `/assets/${encodeURIComponent(hextechName)}.png`;
        }

        function getAugmentTierClass(tier) {
            const normalized = normalizeTierName(tier);
            if (normalized === 'Prismatic') return 'tier-shell-prismatic';
            if (normalized === 'Gold') return 'tier-shell-gold';
            return 'tier-shell-silver';
        }

        function splitAugmentCandidates(nameText) {
            return String(nameText || '')
                .split(/[，,、\/\|+]/)
                .map(part => part.trim())
                .filter(Boolean);
        }

        function findBaseHexByName(nameText) {
            const baseArray = hextechData ? (hextechData.comprehensive || []) : [];
            const candidates = splitAugmentCandidates(nameText);
            const exactNames = [String(nameText || '').trim(), ...candidates].filter(Boolean);

            for (const exactName of exactNames) {
                const exact = baseArray.find(h => h.海克斯名称 === exactName);
                if (exact) {
                    return exact;
                }
            }

            const baseByKey = new Map();
            baseArray.forEach((hex) => {
                const key = normalizeAugmentKey(hex.海克斯名称);
                if (key && !baseByKey.has(key)) {
                    baseByKey.set(key, hex);
                }
            });
            for (const candidate of candidates) {
                const matched = baseByKey.get(normalizeAugmentKey(candidate));
                if (matched) {
                    return matched;
                }
            }
            return null;
        }

        function findAugmentIconFromMap(nameText) {
            const candidates = splitAugmentCandidates(nameText);
            const entries = Object.entries(augmentIconMap || {});
            const byKey = new Map(entries.map(([name, url]) => [normalizeAugmentKey(name), { name, url }]));
            for (const candidate of candidates) {
                const hit = byKey.get(normalizeAugmentKey(candidate));
                if (hit) {
                    return hit;
                }
            }
            return null;
        }

        function findAugmentCatalogEntry(nameText) {
            const candidates = splitAugmentCandidates(nameText);
            for (const candidate of candidates) {
                const hit = augmentCatalogMap[normalizeAugmentKey(candidate)];
                if (hit) {
                    return hit;
                }
            }
            return null;
        }

        function getArticleNameText(item) {
            if (item && Array.isArray(item.augmentNames) && item.augmentNames.length > 0) {
                return item.augmentNames.join(', ');
            }
            if (item && Array.isArray(item.augment_names) && item.augment_names.length > 0) {
                return item.augment_names.join(', ');
            }
            return item ? (item.name || '') : '';
        }

        function resolveArticleAugment(item) {
            const nameText = getArticleNameText(item);
            const baseHex = findBaseHexByName(nameText);
            if (baseHex) {
                return {
                    name: baseHex.海克斯名称,
                    icon: toLocalAugmentIconUrl(baseHex.icon, baseHex.海克斯名称),
                    tier: baseHex.海克斯阶级,
                    desc: purifyHextechTooltip(baseHex.tooltip_plain),
                    score: baseHex.综合得分 || baseHex.海克斯胜率 || 0
                };
            }

            const catalogEntry = findAugmentCatalogEntry(nameText);
            if (catalogEntry) {
                return {
                    name: catalogEntry.name,
                    icon: catalogEntry.icon || toLocalAugmentIconUrl('', catalogEntry.name),
                    tier: catalogEntry.tier || item.tier,
                    desc: purifyHextechTooltip(catalogEntry.tooltip_plain || catalogEntry.description || ''),
                    score: 0
                };
            }

            const mapped = findAugmentIconFromMap(nameText);
            if (mapped) {
                return {
                    name: mapped.name,
                    icon: mapped.url,
                    tier: item.tier,
                    desc: '无详细描述',
                    score: 0
                };
            }

            const fallbackName = splitAugmentCandidates(nameText)[0] || nameText;
            return {
                name: fallbackName,
                icon: toLocalAugmentIconUrl('', fallbackName),
                tier: item.tier,
                desc: '无详细描述',
                score: 0
            };
        }

        function renderAugmentIcon(iconUrl, hextechName, tier, variant = 'list') {
            const localIconUrl = toLocalAugmentIconUrl(iconUrl, hextechName);
            const tierClass = getAugmentTierClass(tier);
            const safeHextechName = escapeHtml(hextechName);
            const safeTier = escapeHtml(tier || '');
            const safeIconUrl = escapeHtml(localIconUrl);
            const shellClass = variant === 'detail'
                ? `augment-icon-shell augment-icon-shell--detail ${tierClass}`
                : `augment-icon-shell augment-icon-shell--list ${tierClass}`;

            return `
                <div class="${shellClass}">
                    <img loading="lazy" width="46" height="46" src="${safeIconUrl}"
                         alt="${safeHextechName}"
                         decoding="async"
                         data-hextech-name="${safeHextechName}"
                         data-hextech-tier="${safeTier}">
                </div>
            `;
        }

        function isQuestionMarkAugmentName(text) {
            return /^[?？]{3,}$/.test(String(text || '').trim());
        }

        function purifyHextechTooltip(text) {
            if (!text) return '';
            const original = String(text).trim();
            if (isQuestionMarkAugmentName(original)) {
                return original;
            }

            let s = String(text);
            s = s.replace(/\{\{\s*Item_Keyword_OnHit\s*\}\}/gi, '攻击特效');
            s = s.replace(/\{\{.*?\}\}/g, '');
            s = s.replace(/<\/?lol-uikit[^>]*>/gi, '');
            s = s.replace(/%i:[a-zA-Z0-9_]+%/gi, '');

            // 剥离动态追踪面板（包括已造成的伤害等变体）
            s = s.replace(/<br>\s*(已获得的额外属性|已造成的伤害|已造成的额外伤害|已提供的护盾|伤害|治疗)[：:][\s\S]*/g, '');
            s = s.replace(/\s*(已获得的额外属性|已造成的伤害|已造成的额外伤害|已提供的护盾|伤害|治疗)[：:][\s\S]*/g, '');

            // 仅清理 tooltip 内未解析变量留下的 ASCII 问号，不处理真实海克斯名“？？？”。
            s = s.replace(/\?{2,}/g, '');
            s = s.replace(/(?:\s*\?\s*){2,}/g, '');
            s = s.replace(/^[?\s]+|[?\s]+$/g, '');

            s = s.replace(/\n\s*\n/g, '\n');
            return s.trim();
        }

        function createHextechCard(hextech, index) {
            const iconUrl = toLocalAugmentIconUrl(hextech.icon, hextech.海克斯名称);
            const tierClass = getTierBarClass(hextech.海克斯阶级);

            const wrValue = Number(hextech.海克斯胜率) || 0;
            const wr = (wrValue * 100).toFixed(1);
            const pr = (hextech.海克斯出场率 * 100).toFixed(1);

            // 胜率热力图分级：>53% 为热点、<47% 为冷点，居中区段保持中性
            let heatClass = '';
            let trendArrowMarkup = '';
            if (wrValue > 0.53) {
                heatClass = 'heat-hot';
                trendArrowMarkup = '<span class="hextech-card-rate-trend text-emerald-300">▲</span>';
            } else if (wrValue < 0.47) {
                heatClass = 'heat-cold';
                trendArrowMarkup = '<span class="hextech-card-rate-trend text-rose-300">▼</span>';
            }

            const hexInfo = {
                name: hextech.海克斯名称,
                tier: hextech.海克斯阶级,
                desc: purifyHextechTooltip(hextech.tooltip_plain)
            };
            const bindData = encodeURIComponent(JSON.stringify(hexInfo));

                return `
            <div class="hextech-list-card list-row cursor-pointer p-2 rounded border border-[rgba(139,148,158,0.18)] bg-[rgba(19,26,34,0.92)] transition-colors ${heatClass}" data-hex-info="${bindData}">
                <div class="hextech-card-rank text-xs font-bold text-gray-500">${index + 1}</div>

                <div class="hextech-card-icon-group">
                    <div class="w-1.5 h-6 rounded-full ${tierClass}"></div>
                    ${renderAugmentIcon(iconUrl, hextech.海克斯名称, hextech.海克斯阶级, 'list')}
                </div>

                <div class="hextech-card-title">
                    <div class="text-sm font-bold text-gray-200 truncate" title="${escapeHtml(hextech.海克斯名称)}">${escapeHtml(hextech.海克斯名称)}</div>
                </div>

                <div class="hextech-card-rate hextech-card-rate--win">
                    <span class="hextech-card-rate-value">${wr}%</span>${trendArrowMarkup}
                </div>

                <div class="hextech-card-rate hextech-card-rate--pick">
                    <span class="hextech-card-rate-value">${pr}%</span>
                </div>
            </div>
            `;
        }

        function renderCurrentView() {
            if (!hextechData) return;
            const compList = document.getElementById('compList');
            const dataContainer = document.getElementById('dataContainer');
            const noDataTip = document.getElementById('noDataTip');

            let leftData = [];

            const baseArray = currentMode === 'winrate_only' ? hextechData.winrate_only : hextechData.comprehensive;

            if (currentView === 'all') {
                leftData = baseArray || [];
            } else {
                const tierData = hextechData[currentView] || [];
                if (currentMode === 'winrate_only') {
                    leftData = tierData.slice().sort((a, b) => b.海克斯胜率 - a.海克斯胜率);
                } else {
                    leftData = tierData.slice().sort((a, b) => (b.综合得分 || 0) - (a.综合得分 || 0));
                }
            }


            if (leftData.length === 0) {
                dataContainer.classList.add('hidden');
                noDataTip.classList.remove('hidden');
            } else {
                noDataTip.classList.add('hidden');
                dataContainer.classList.remove('hidden');
                compList.innerHTML = leftData.map((h, i) => createHextechCard(h, i)).join('');

                updateFilteredSynergies(currentView);
            }
        }

        function normalizeGradeValue(grade) {
            return String(grade || '未知').replace(/^评分\s*/i, '').trim().toUpperCase() || '未知';
        }

        function getGradeScore(grade) {
            const gradeVal = normalizeGradeValue(grade);
            if (gradeVal.includes('SS')) return 6;
            if (gradeVal.includes('S')) return 5;
            if (gradeVal.includes('A')) return 4;
            if (gradeVal.includes('B')) return 3;
            if (gradeVal.includes('C')) return 2;
            if (gradeVal.includes('D')) return 1;
            return 0;
        }

        function parseLegacySynergyString(raw) {
            const str = String(raw || '');
            const parts = str.split('|').map(s => s.trim());
            if (parts.length < 4) {
                return { original: str, name: '未知联动', tier: '', grade: '未知', tags: [], content: str };
            }

            const nameInfo = parts[0];
            const tierName = parts[1];
            const gradeStr = parts[2];
            const tagGroup = parts[3];
            let upvotes = 0;
            let downvotes = 0;
            let contentStartIndex = 5;
            if (parts.length > 5 && /^\d+$/.test(parts[4]) && /^\d+$/.test(parts[5])) {
                upvotes = parseInt(parts[4] || '0');
                downvotes = parseInt(parts[5] || '0');
                contentStartIndex = 6;
            }

            let author = '佚名';
            let content = '';
            let isOriginal = false;

            if (parts.length > contentStartIndex) {
                const nextPart = parts[contentStartIndex];
                if (nextPart.startsWith('作者：') || nextPart.startsWith('作者:')) {
                    author = nextPart.substring(3).trim();
                    contentStartIndex += 1;
                    if (parts.length > contentStartIndex && (parts[contentStartIndex] === '原创' || parts[contentStartIndex] === '非原创')) {
                        isOriginal = parts[contentStartIndex] === '原创';
                        contentStartIndex += 1;
                    }
                } else if (nextPart === '原创') {
                    isOriginal = true;
                    contentStartIndex += 1;
                    if (parts.length > contentStartIndex && (parts[contentStartIndex].startsWith('作者：') || parts[contentStartIndex].startsWith('作者:'))) {
                        author = parts[contentStartIndex].substring(3).trim();
                        contentStartIndex += 1;
                    }
                } else if (parts.length > contentStartIndex + 1 && (parts[contentStartIndex + 1] === '原创' || parts[contentStartIndex + 1] === '非原创')) {
                    author = nextPart || author;
                    isOriginal = parts[contentStartIndex + 1] === '原创';
                    contentStartIndex += 2;
                }
                content = parts.slice(contentStartIndex).join(' | ');
            }

            return {
                original: str,
                name: nameInfo,
                augmentNames: splitAugmentCandidates(nameInfo),
                tier: tierName,
                grade: normalizeGradeValue(gradeStr),
                tags: tagGroup.split(/\s+/).filter(t => t),
                upvotes: upvotes,
                downvotes: downvotes,
                author: author,
                isOriginal: isOriginal,
                content: content
            };
        }

        function normalizeSynergyArticle(raw) {
            if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
                const augmentNames = Array.isArray(raw.augment_names)
                    ? raw.augment_names
                    : (Array.isArray(raw.augmentNames) ? raw.augmentNames : splitAugmentCandidates(raw.name));
                const nameInfo = String(raw.name || augmentNames.join(', ') || '未知联动');
                return {
                    original: raw,
                    name: nameInfo,
                    augmentNames: augmentNames.filter(Boolean),
                    tier: String(raw.tier || ''),
                    grade: normalizeGradeValue(raw.rating || raw.grade),
                    tags: String(raw.tag || (Array.isArray(raw.tags) ? raw.tags.join(' ') : '') || '强力联动').split(/\s+/).filter(t => t),
                    upvotes: Number(raw.upvotes || 0),
                    downvotes: Number(raw.downvotes || 0),
                    author: String(raw.author || '佚名'),
                    isOriginal: Boolean(raw.is_original || raw.isOriginal),
                    content: String(raw.content || '')
                };
            }
            return parseLegacySynergyString(raw);
        }

        function isKnownSynergyTier(rawTier) {
            const text = String(rawTier || '').trim();
            return Boolean(text && (
                text.includes('棱彩') || text.includes('彩色') || text === 'Prismatic' ||
                text.includes('黄金') || text.includes('金') || text === 'Gold' ||
                text.includes('白银') || text.includes('银') || text === 'Silver'
            ));
        }

        function articleMatchesTier(item, tier) {
            if (tier === 'all') return true;
            if (isKnownSynergyTier(item.tier)) {
                return normalizeTierName(item.tier) === tier;
            }
            const resolvedAugment = resolveArticleAugment(item);
            return resolvedAugment.tier && normalizeTierName(resolvedAugment.tier) === tier;
        }

        function updateFilteredSynergies(tier) {
            const container = document.getElementById('synergyArticleScroll');
            container.dataset.synergyLoaded = synergyLoaded ? '1' : '0';
            container.dataset.synergyStatus = synergyMeta && synergyMeta.status ? synergyMeta.status : 'ok';
            const normalized = (synergyData || []).map(normalizeSynergyArticle).filter(item => item.content);
            if (normalized.length === 0) {
                let message = '暂无联动文章';
                if (synergyMeta && synergyMeta.status === 'quarantined') {
                    message = synergyMeta.message || '联动数据待校准';
                } else if (synergyMeta && synergyMeta.status === 'error') {
                    message = synergyMeta.message || '联动数据读取失败';
                } else if (synergyMeta && synergyMeta.status === 'empty') {
                    message = synergyMeta.message || '暂无联动数据';
                }
                container.innerHTML = `<div class="glass-panel rounded-xl p-4"><div class="text-sm text-gray-400 text-center">${escapeHtml(message)}</div></div>`;
                return;
            }

            const filtered = normalized.filter(item => articleMatchesTier(item, tier));

            if (filtered.length === 0) {
                container.innerHTML = '<div class="glass-panel rounded-xl p-4"><div class="text-sm text-gray-400 text-center">该阶级无联动文章</div></div>';
                return;
            }

            renderSynergyArticles(filtered, container);
        }

        function renderSynergyArticles(articles, container) {
            if (!articles || articles.length === 0) {
                container.innerHTML = '<div class="glass-panel rounded-xl p-4"><div class="text-sm text-gray-400 text-center">暂无联动文章</div></div>';
                return;
            }

            const parsedArticles = articles.map(item => {
                const resolvedAugment = resolveArticleAugment(item);
                const gradeScore = getGradeScore(item.grade);
                return {
                    ...item,
                    gradeScore: gradeScore,
                    fallbackScore: resolvedAugment.score || 0,
                    resolvedAugment: resolvedAugment
                };
            });

            const html = parsedArticles.map(item => {
                if (item.gradeScore === -1) {
                    return `
                        <div class="glass-panel rounded-xl p-4 border border-gray-800 hover:border-blue-500/30 transition-colors">
                            <div class="hextech-article-content text-sm text-gray-300">${escapeHtml(item.content)}</div>
                        </div>
                    `;
                }

                let badgeBg = 'bg-[#8c9ba5]';
                let badgeText = 'text-[#000000]';
                if (item.gradeScore >= 5) {
                    badgeBg = 'bg-[#ebd55b]';
                    badgeText = 'text-[#000000]';
                } else if (item.gradeScore === 4) {
                    badgeBg = 'bg-[#9a73d1]';
                    badgeText = 'text-[#ffffff]';
                } else if (item.gradeScore === 3) {
                    badgeBg = 'bg-[#4b7cf3]';
                    badgeText = 'text-[#ffffff]';
                }

                const resolvedAugment = item.resolvedAugment || resolveArticleAugment(item);
                const iconUrl = resolvedAugment.icon;
                const safeName = escapeHtml(item.name);
                const displayTier = item.tier || resolvedAugment.tier;
                const safeTier = escapeHtml(displayTier);
                const safeGrade = escapeHtml(item.grade);
                const safeAuthor = escapeHtml(item.author);

                const getTagStyle = (tag) => {
                    if (tag.includes('强力联动')) return 'border border-[#2bd5c2]/40 text-[#2bd5c2] bg-[#2bd5c2]/10';
                    if (tag.includes('娱乐')) return 'border border-[#9a73d1]/40 text-[#9a73d1] bg-[#9a73d1]/10';
                    if (tag.includes('陷阱')) return 'border border-[#e28544]/40 text-[#e28544] bg-[#e28544]/10';
                    return 'border border-gray-500/40 text-gray-400 bg-gray-500/10';
                };

                const hexInfo = {
                    name: resolvedAugment.name || item.name,
                    tier: resolvedAugment.tier || item.tier,
                    desc: resolvedAugment.desc || '无详细描述'
                };
                const bindData = encodeURIComponent(JSON.stringify(hexInfo));

                return `
<div class="hextech-card p-[1px] hx-surface-card mb-4" style="clip-path: polygon(0 0, calc(100% - 14px) 0, 100% 14px, 100% 100%, 14px 100%, 0 calc(100% - 14px));">
    <div class="hextech-article-inner bg-[#060b10]" style="clip-path: polygon(0 0, calc(100% - 13px) 0, 100% 13px, 100% 100%, 13px 100%, 0 calc(100% - 13px));">

        <div class="cursor-pointer" data-hex-info="${bindData}">
            ${renderAugmentIcon(iconUrl, resolvedAugment.name || item.name, displayTier, 'detail')}
        </div>

        <div class="hextech-article-body flex flex-col flex-grow">
            <div class="hextech-article-head">

                <div class="hextech-article-title-block flex flex-col">
                    <div class="flex items-center gap-2.5 flex-wrap">
                        <span class="text-white text-[16px] font-bold tracking-wide leading-none">${safeName}</span>
                        <div class="${badgeBg} px-1.5 h-[20px] rounded flex items-center shadow-sm">
                            <span class="${badgeText} text-[11px] font-black">${safeGrade}</span>
                        </div>
                        ${item.tags.map(tag => `<div class="${getTagStyle(tag)} px-1.5 h-[20px] rounded flex items-center text-[10px] font-bold">${escapeHtml(tag)}</div>`).join('')}
                    </div>
                    <div class="text-[#8c9ba5] text-[11px] mt-1.5 leading-none">
                        ${safeTier}
                    </div>
                </div>

                <div class="hextech-article-meta flex items-center gap-2 mt-0">
                    <div class="flex items-center bg-[#1e2333] border border-[#2a3040] rounded-full overflow-hidden h-[24px]">
                        <div class="flex items-center gap-1.5 px-3 h-full border-r border-[#2a3040]">
                            <svg class="w-3.5 h-3.5 text-[#507c59] fill-current" viewBox="0 0 16 16"><path d="M3 1.5A.5.5 0 0 1 3.5 1h9a.5.5 0 0 1 .4.82l-2 2.68 2 2.68a.5.5 0 0 1-.4.82h-9v6.5a.5.5 0 0 1-1 0v-13z"/></svg>
                            <span class="text-[#8c9ba5] text-[12px] font-medium pt-0.5">${item.upvotes}</span>
                        </div>
                        <div class="flex items-center gap-1.5 px-3 h-full">
                            <svg class="w-3.5 h-3.5 text-[#c29845] fill-current" viewBox="0 0 16 16"><path d="M8 13.25 2.75 5.5h10.5L8 13.25z"/></svg>
                            <span class="text-[#8c9ba5] text-[12px] font-medium pt-0.5">${item.downvotes}</span>
                        </div>
                    </div>
                    ${item.author ? `<div class="text-[#8c9ba5] text-[12px] flex flex-wrap items-center justify-end gap-1.5 min-w-0 text-right">作者: ${safeAuthor} ${item.isOriginal ? '<span class="bg-[#ebd55b] text-black px-1 py-[1px] rounded-[3px] text-[10px] font-black tracking-wide leading-none pt-[3px]">原创</span>' : ''}</div>` : ''}
                </div>

            </div>

            <div class="hextech-article-content text-[#b3b9c6] text-[14px] mt-3">
                ${escapeHtml(item.content)}
            </div>

        </div>
    </div>
</div>
                `;
            }).join('');

            container.innerHTML = html;
        }

        async function loadSynergies() {
            try {
                const synergyRes = await fetch(`${API_BASE}/api/synergies/${encodeURIComponent(heroId || hero)}`);
                const synergyResult = await synergyRes.json();
                synergyMeta = synergyResult && typeof synergyResult === 'object' ? synergyResult : null;
                synergyData = Array.isArray(synergyResult.synergy_items) && synergyResult.synergy_items.length > 0
                    ? synergyResult.synergy_items
                    : (synergyResult.synergies || []);
                synergyLoaded = true;
                updateFilteredSynergies(currentView);
            } catch (err) {
                console.error(err);
                synergyMeta = { status: 'error', message: '联动数据读取失败' };
                synergyData = [];
                synergyLoaded = true;
                updateFilteredSynergies(currentView);
            }
        }

        async function loadAugmentIconMap() {
            try {
                const [iconResponse, manifestResponse] = await Promise.all([
                    fetch(`${API_BASE}/api/augment_icon_map`).catch(() => null),
                    fetch('/catalog/Augment_Icon_Manifest.json', { cache: 'no-store' }).catch(() => null)
                ]);

                if (iconResponse && iconResponse.ok) {
                    const payload = await iconResponse.json();
                    augmentIconMap = payload && typeof payload === 'object' ? payload : {};
                }

                if (manifestResponse && manifestResponse.ok) {
                    const manifest = await manifestResponse.json();
                    if (Array.isArray(manifest)) {
                        const catalog = {};
                        manifest.forEach((item) => {
                            if (!item || !item.name) return;
                            const icon = item.filename
                                ? `/assets/${encodeURIComponent(item.filename)}`
                                : (item.icon_url || augmentIconMap[item.name] || '');
                            const entry = {
                                name: item.name,
                                icon,
                                tier: item.tier || '',
                                tooltip_plain: item.tooltip_plain || '',
                                description: item.description || ''
                            };
                            catalog[normalizeAugmentKey(item.name)] = entry;
                        });
                        augmentCatalogMap = catalog;
                    }
                }
            } catch (_) {
                augmentIconMap = {};
                augmentCatalogMap = {};
            }
        }

        function detailRetryDelayMs(retryCount) {
            return Math.min(
                DETAIL_LOADING_RETRY_MAX_MS,
                DETAIL_LOADING_RETRY_BASE_MS * Math.pow(2, Math.min(retryCount, 4))
            );
        }

        function describeLoadingStatus(payload) {
            const startup = payload && payload.startup_status ? payload.startup_status : {};
            const preload = payload && payload.preload_status ? payload.preload_status : {};
            const messages = [
                preload.last_error,
                preload.error,
                preload.reason,
                startup.last_error,
                startup.hextech_warning,
                startup.bundle_manifest && startup.bundle_manifest.warning,
            ].filter(Boolean);
            return messages.length > 0 ? String(messages[0]) : '详情页已打开，海克斯数据会在后台补齐后可用';
        }

        function scheduleDetailRetry(retryCount) {
            if (detailLoadingRetryTimer) {
                return;
            }
            if (retryCount >= DETAIL_LOADING_MAX_RETRIES) {
                document.querySelector('#noDataTip .text-sm').textContent = '数据准备时间较长，请稍后刷新页面';
                return;
            }
            detailLoadingRetryTimer = window.setTimeout(() => {
                detailLoadingRetryTimer = null;
                loadHextechs(retryCount + 1);
            }, detailRetryDelayMs(retryCount));
        }

        function clearDetailRetry() {
            if (!detailLoadingRetryTimer) {
                return;
            }
            window.clearTimeout(detailLoadingRetryTimer);
            detailLoadingRetryTimer = null;
        }

        function requestDetailPreload() {
            if (detailPreloadRequested || !hero) {
                return;
            }
            detailPreloadRequested = true;
            fetch(`${API_BASE}/api/champion/${encodeURIComponent(hero)}/preload`, {
                method: 'POST',
                credentials: 'same-origin',
            }).catch(() => {});
        }

        async function loadHextechs(retryCount = 0) {
            if (!hero) {
                clearDetailRetry();
                document.getElementById('skeletonContainer').classList.add('hidden');
                const spinner = document.getElementById('loadingSpinner');
                spinner.classList.remove('hidden');
                spinner.innerHTML = '<div class="text-red-400">错误：未获取到英雄名称</div>';
                return;
            }
            try {
                const hextechRes = await fetch(`${API_BASE}/api/champion/${encodeURIComponent(hero)}/hextechs`);
                hextechData = await hextechRes.json();
                activeArray = hextechData.comprehensive || [];
                synergyData = [];
                synergyMeta = null;
                synergyLoaded = false;

                document.getElementById('loadingSpinner').classList.add('hidden');
                document.getElementById('skeletonContainer').classList.add('hidden');
                renderCurrentView();
                loadAugmentIconMap().then(() => {
                    if (hextechData) {
                        renderCurrentView();
                    }
                }).catch(() => {});
                if (hextechData.loading && activeArray.length === 0) {
                    document.getElementById('noDataTip').classList.remove('hidden');
                    document.querySelector('#noDataTip .text-xl').textContent = '数据准备中';
                    document.querySelector('#noDataTip .text-sm').textContent = describeLoadingStatus(hextechData);
                    requestDetailPreload();
                    scheduleDetailRetry(retryCount);
                    return;
                }
                clearDetailRetry();
                loadSynergies();
            } catch (err) {
                console.error(err);
                clearDetailRetry();
                document.getElementById('loadingSpinner').classList.add('hidden');
                document.getElementById('skeletonContainer').classList.add('hidden');
                document.getElementById('noDataTip').classList.remove('hidden');
                document.querySelector('#noDataTip .text-xl').textContent = '数据加载失败';
            }
        }

        function connectWS() {
            ws = new WebSocket(WS_URL + '/ws');
            window.wsInstance = ws;
            const status = document.getElementById('wsStatus');
            ws.onopen = () => { status.innerHTML = '<span class="w-2 h-2 bg-green-500 rounded-full animate-pulse"></span><span class="text-green-400">已连接</span>'; };
            ws.onclose = () => { status.innerHTML = '<span class="w-2 h-2 bg-red-500 rounded-full"></span><span class="text-red-400">已断开</span>'; setTimeout(connectWS, 3000); };
            ws.onmessage = (e) => {
                try {
                    const msg = JSON.parse(e.data);
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
                } catch (err) {
                    console.error(err);
                }
            };
        }

        let detailBootstrapped = false;
        function bootstrapDetailPage() {
            if (detailBootstrapped) return;
            detailBootstrapped = true;
            loadHextechs();
            connectWS();
        }

        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', bootstrapDetailPage, { once: true });
        } else {
            bootstrapDetailPage();
        }

        window.addEventListener('DOMContentLoaded', () => {
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

            const containers = [
                document.getElementById('compList'),
                document.getElementById('synergyArticleScroll')
            ].filter(Boolean);

            containers.forEach(container => {
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
        });

        function escapeHtml(text) {
            const map = {
                '&': '&amp;',
                '<': '&lt;',
                '>': '&gt;',
                '"': '&quot;',
                "'": '&#039;'
            };
            return String(text).replace(/[&<>"']/g, m => map[m]);
        }
