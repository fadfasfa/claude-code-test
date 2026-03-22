import sys
import re

file_path = r'c:\Users\apple\claudecode\run\static\detail.html'

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# Chunk 1: background variables
content = re.sub(
    r"\s*let iconBgClass = 'bg-\[\#181a26\]';.*?(?=\s*const wr =)",
    "",
    content,
    flags=re.DOTALL
)

# Chunk 2: raw img tag to renderAugmentIcon('list')
content = re.sub(
    r"<img src=\"\$\{iconUrl\}\"\s*alt=\"\$\{hextech\.海克斯名称\}\"\s*loading=\"lazy\"\s*decoding=\"async\"\s*data-hextech-tier=\"\$\{hextech\.海克斯阶级\}\"\s*data-hextech-name=\"\$\{hextech\.海克斯名称\}\"\s*onerror=\"this\.onerror=null; this\.src=createPlaceholder\(this\.dataset\.hextechName \|\| this\.alt \|\| '\?'\);\"\s*class=\"w-10 h-10 rounded-lg border border-gray-600 object-cover \$\{iconBgClass\} flex-shrink-0 shadow-md\">",
    r"${renderAugmentIcon(iconUrl, hextech.海克斯名称, hextech.海克斯阶级, 'list')}",
    content,
    flags=re.DOTALL
)

# Chunk 3: synergy right side background classes
content = re.sub(
    r"\s*// 不同评阶图标的发光效果与内部填充底色\s*let iconBorderColor = 'border-gray-500';\s*let iconBgClass = 'bg-\[\#0a0a0f\]';\s*if \(item\.tier\.includes\('棱彩'\) \|\| item\.tier === 'Prismatic'\) \{\s*iconBorderColor = 'border-\[\#2bd5c2\]';\s*iconBgClass = 'bg-gradient-to-br from-\[\#2bd5c2\]/30 to-\[\#0a0a0f\]';\s*\}\s*else if \(item\.tier\.includes\('金'\) \|\| item\.tier === 'Gold'\) \{\s*iconBorderColor = 'border-\[\#d4af37\]';\s*iconBgClass = 'bg-gradient-to-br from-\[\#d4af37\]/30 to-\[\#0a0a0f\]';\s*\}\s*else if \(item\.tier\.includes\('银'\) \|\| item\.tier === 'Silver'\) \{\s*iconBorderColor = 'border-\[\#8c9ba5\]';\s*iconBgClass = 'bg-gradient-to-br from-\[\#8c9ba5\]/30 to-\[\#0a0a0f\]';\s*\}",
    "",
    content,
    flags=re.DOTALL
)

# Chunk 4: raw synergy img tag to renderAugmentIcon('detail')
content = re.sub(
    r"<div class=\"w-14 h-14 flex-shrink-0 border-2 \$\{iconBorderColor\} p-\[1\.5px\] bg-\[\#0a0a0f\] rounded-lg relative\">\s*<img src=\"\$\{iconUrl\}\"\s*data-hextech-name=\"\$\{item\.name\}\"\s*data-hextech-tier=\"\$\{item\.tier\}\"\s*decoding=\"async\"\s*class=\"w-full h-full object-cover rounded-\[5px\] \$\{iconBgClass\}\"\s*onerror=\"this\.onerror=null; this\.src=createPlaceholder\(this\.dataset\.hextechName \|\| this\.alt \|\| '\?'\);\">\s*</div>",
    r"${renderAugmentIcon(iconUrl, item.name, item.tier, 'detail')}",
    content,
    flags=re.DOTALL
)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print('Success')
