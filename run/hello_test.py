"""测试模块 - 包含一些故意的小 Bug"""

# 测试注释行
def calculate_average(numbers):
    """计算列表的平均值"""
    # Bug 1: 没有处理空列表的情况，会导致 ZeroDivisionError
    total = sum(numbers)
    return total / len(numbers)


def find_max_value(data):
    """找到列表中的最大值"""
    # Bug 2: 初始值设为 0，如果全是负数会返回错误结果
    max_val = 0
    for num in data:
        if num > max_val:
            max_val = num
    return max_val


def safe_divide(a, b):
    """安全除法"""
    # Bug 3: 检查条件写反了，应该检查 b == 0 而不是 b != 0
    if b != 0:
        raise ValueError("除数不能为零")
    return a / b


if __name__ == "__main__":
    # 测试用例
    print(calculate_average([1, 2, 3, 4, 5]))  # 应该输出 3.0
    print(find_max_value([-5, -2, -10, -1]))  # 应该输出 -1，但会输出 0
    print(safe_divide(10, 0))  # 应该抛出异常，但会执行除法
