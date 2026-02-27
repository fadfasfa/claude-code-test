import json, sys, os

CONTRACT = ".ai_workflow/current_contract.json"

def toggle(target: str = None):
    if not os.path.exists(CONTRACT):
        print(f"错误：找不到 {CONTRACT}")
        return

    with open(CONTRACT, "r", encoding="utf-16") as f:
        data = json.load(f)

    # 确保 JSON 结构存在
    if "task_input_from_claude" not in data:
        data["task_input_from_claude"] = {}

    current = data["task_input_from_claude"].get("executor_node", "QWEN_API")

    if target:
        new_node = "CLAUDE_API" if "claude" in target.lower() else "QWEN_API"
    else:
        new_node = "QWEN_API" if current == "CLAUDE_API" else "CLAUDE_API"

    data["task_input_from_claude"]["executor_node"] = new_node

    with open(CONTRACT, "w", encoding="utf-16") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    # 使用 GBK 兼容的字符
    status = "切换到" if current != new_node else "已是"
    print(f"[OK] 执行者身份：{current} -> {new_node}")

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else None
    toggle(target)
