import datetime
import platform

# 获取当前日期时间
current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# 输出欢迎信息
print(f"Welcome to the V4.5 Agile Evolution Architecture!")
print(f"Current date and time: {current_time}")
print(f"System platform: {platform.system()}")
