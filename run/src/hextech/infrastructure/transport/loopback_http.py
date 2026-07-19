"""仅供本机控制面使用的 HTTP Server。

标准库 ``HTTPServer.server_bind`` 会对绑定地址执行 ``getfqdn``，Windows 上可能
在 UI 启动关键路径触发缓慢的反向 DNS。这里保留 ``TCPServer`` 的绑定语义，直接
使用已经限定为 loopback 的地址作为 server name。
"""

from __future__ import annotations

from http.server import ThreadingHTTPServer
from socketserver import TCPServer


class LoopbackThreadingHTTPServer(ThreadingHTTPServer):
    """不解析 FQDN、请求线程不阻塞进程退出的 loopback HTTP Server。"""

    daemon_threads = True

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


__all__ = ["LoopbackThreadingHTTPServer"]
