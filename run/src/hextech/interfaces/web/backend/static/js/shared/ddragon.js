// DDragon 版本号：promise 缓存的一次性拉取，失败回退固定版本。
// import 阶段零副作用；入口在 bootstrap 里调用 refreshDdragonVersion()。

const FALLBACK_VERSION = '14.3.1';
let currentVersion = FALLBACK_VERSION;
let fetchPromise = null;

// 同步读当前已知版本（拉取完成前返回回退值，与历史行为一致）
export function peekDdragonVersion() {
    return currentVersion;
}

export function refreshDdragonVersion() {
    if (!fetchPromise) {
        fetchPromise = fetch('https://ddragon.leagueoflegends.com/api/versions.json')
            .then((r) => r.json())
            .then((versions) => {
                if (versions && versions[0]) currentVersion = versions[0];
                return currentVersion;
            })
            .catch(() => currentVersion);
    }
    return fetchPromise;
}
