import os
from datetime import datetime

# 检查 run/ 目录写权限
if not os.access(".", os.W_OK):
    print("[ERROR: RUN-DIR-LOCKED]")
    exit(1)

# 获取当前时间（毫秒级精度）
current_time = datetime.now()
time_str = current_time.strftime('%Y-%m-%d %H:%M:%S') + f'.{current_time.microsecond // 1000:03d}'
print(f"系统时间: {time_str}")
print("[V4.4-STAGING-FLOW-RE-CONFIRMED]")
