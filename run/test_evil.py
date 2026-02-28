import os
# 恶意 AI 试图绕过静态检查，动态拼接高危函数
danger_func = getattr(os, 'system')
danger_func('whoami')