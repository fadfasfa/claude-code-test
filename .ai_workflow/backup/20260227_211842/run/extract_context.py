import urllib.request
import re
import ssl

def run_probe():
    print("🚀 启动降级物理探针，正在强行读取页面底层数据...")
    url = "https://hextech.dtodo.cn/zh-CN/champion-stats/1"
    
    # 绕过环境 SSL 限制
    context = ssl._create_unverified_context()
    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
        }
    )
    
    try:
        # 直接读取原始文本
        html = urllib.request.urlopen(req, context=context, timeout=15).read().decode('utf-8')
        
        # 定位锚点
        matches = list(re.finditer(r'winRate|win_rate', html))
        print(f"✅ 成功获取 HTML (长度 {len(html)} 字节)。总计发现 {len(matches)} 个数据锚点。\n")
        
        # 截取物理快照（使用 repr 保留所有隐藏的转义字符）
        for i, m in enumerate(matches[:5]):
            start = max(0, m.start() - 150)
            end = min(len(html), m.end() + 150)
            print(f"--- 锚点 {i+1} 物理快照 ---")
            print(repr(html[start:end]))
            print("-" * 60)
            
    except Exception as e:
        print(f"❌ 探针请求异常: {e}")

if __name__ == "__main__":
    run_probe()