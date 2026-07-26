// 网络工具：API/WS 地址推导与自动重连 WebSocket。
// 重连采用 1s 起步、15s 封顶的指数退避，连通即复位（替代旧的固定 3s）。

export function apiBase() {
    return window.location.origin;
}

export function wsUrl() {
    const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${scheme}//${window.location.host}`;
}

export function createReconnectingWS({
    url,
    onMessage,
    onOpen,
    onClose,
    baseDelayMs = 1000,
    maxDelayMs = 15000,
}) {
    let socket = null;
    let attempts = 0;
    let reconnectTimer = null;
    let closedByCaller = false;

    function connect() {
        socket = new WebSocket(url);
        socket.onopen = () => {
            attempts = 0;
            if (onOpen) onOpen();
        };
        socket.onclose = () => {
            if (closedByCaller) return;
            if (onClose) onClose();
            const delay = Math.min(maxDelayMs, baseDelayMs * Math.pow(2, Math.min(attempts, 6)));
            attempts += 1;
            reconnectTimer = setTimeout(connect, delay);
        };
        socket.onmessage = (event) => {
            let message = null;
            try {
                message = JSON.parse(event.data);
            } catch (err) {
                console.error('[WS] 解析消息失败:', err);
                return;
            }
            try {
                onMessage(message);
            } catch (err) {
                console.error('[WS] 处理消息失败:', err);
            }
        };
    }

    function close() {
        closedByCaller = true;
        if (reconnectTimer !== null) clearTimeout(reconnectTimer);
        if (socket) {
            socket.onclose = null;
            socket.close();
        }
    }

    connect();
    return {
        close,
        get socket() {
            return socket;
        },
    };
}
