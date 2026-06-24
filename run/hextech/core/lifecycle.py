"""桌面/Web/overlay 生命周期协调入口。

现阶段桌面服务编排的真实实现位于 `hextech.display.desktop.service_manager`。
这里保留 core 层稳定导入点，供后续把跨产品生命周期合同继续收口。
"""

from __future__ import annotations

from hextech.display.desktop.service_manager import ManagedService, ServiceManager


__all__ = ["ManagedService", "ServiceManager"]
